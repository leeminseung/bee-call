"""The one place that turns a person's call records into what the app draws (contract/app-contract.md, sections 1
and 2): the call list (GET /calls) and one call as its thread (GET /calls/{call_id}).

It only reads: a call's folder (events.jsonl, bee-writes.jsonl, its jobs/), the ring log (state/outbound.jsonl) and
the voice server's ring verdicts (call_missed, call_declined in meta/events.jsonl). What a folder's files say is kept
per folder until one of its files changes (Calls); liveness and missed rings are worked out against the clocks it is
given on every build. Every rule of the contract's sections 1 and 2 is here, and older records are read here and
nowhere else: bee_write_queued and bee_write_done, records without bee-writes.jsonl, without user_transcript or
without a "call" event, times without an offset, and prep reads stored as "tool {json}" text.

Two clocks: `now` is the person's clock (Person.now(), which a test clock can move), against which rings expire;
`wall` is this machine's (time.time()), against which a record's file shows life.
"""

import base64
import binascii
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from beelink.planner import OFFERS_DIR, OUTBOUND_LOG
from voice.audio_tags import strip_tags
from voice.call_folders import CALL_ID, EVENTS, call_ids, read_events, session_id
from voice.offers import topic_line
from worker.chart import svg_title
from worker.runner import USAGE

LIVE_S = 120  # no new line or file refresh for this long, and no end: the call no longer reads as live
RECENT_S = 600  # an ended call is still announced by /changes on connect for this long (contract section 3)
NEWEST_DAYS = 2  # the day folders whose records /changes looks at between rebuilds: today's, and yesterday's after 0:00
# The retry of failed Bee writes after a call ends within 5 s (voice/agent.py BEE_RETRY_S): a retry still applying
# after this long was cut off by a stopped process.
RETRY_SETTLES_S = 30
DEFAULT_LIMIT, MAX_LIMIT = 20, 50
TITLE_CHARS = 60  # the person's first sentence as a title
JOB_TITLE_CHARS = 40  # a job's title cut from its task, for jobs started before delegate took a title
WRITES = "bee-writes.jsonl"  # beelink/writer.py
KINDS = ("todo_create", "todo_accept", "fact_confirm", "fact_update")
ENDED = ("user_initiated", "participant_disconnected", "task_completed")  # LiveKit's close reasons for a normal end
BOOKKEEPING = ("request.md", "events.jsonl", "usage.json")  # a job folder's own files, never listed
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
TABLES = (".csv", ".tsv")
KIND_ORDER = {"chart": 0, "report": 1, "table": 2, "other": 3}
SENTENCE_END = re.compile(r"[.!?。！？](?=\s|$)")
CLOCK_ONLY = re.compile(r"(\d{1,2}):(\d{2})")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
LAST = math.inf  # sorts after every line of the record

# Bee's ids in the prep note ("(conversation 10785664)", "todo suggestion 11628862"), which the AI needs and the
# person does not (moved from web/app.js withoutIds). ASCII rules, as in JavaScript: an id next to Hangul still ends.
ID_WORDS = r"convs?|conversations?|todos?|todo suggestions?|suggestions?|facts?|unconfirmed facts?|ids?"
ID_GROUP = re.compile(rf"\s*\((?:(?:{ID_WORDS})\.?\s*)?#?\d{{4,}}(?:\s*(?:,|and|&)\s*#?\d{{4,}})*\)", re.I | re.A)
ID_AFTER_WORD = re.compile(rf"\b({ID_WORDS})\s+#?\d{{4,}}\b", re.I | re.A)
BARE_ID = re.compile(r"(^|\s)#\d{4,}\b", re.A)
SPACE_BEFORE_MARK = re.compile(r"[ \t]+([,.;:])")
SPACES = re.compile(r"[ \t]{2,}")


class BadRequest(ValueError):
    """A list request the server answers with 400: a limit outside 1-50, or a cursor it did not give out."""


# ---------- what a folder's files say ----------

@dataclass
class JobFolder:
    names: set[str]  # the files in jobs/<NN>/
    chart_titles: dict[str, str]  # chart file -> its title, kept in the chart's SVG


@dataclass
class Record:
    """One call folder as read: its numbered lines, the Bee writer's record, its job folders, its file's time."""
    folder: Path
    lines: list[tuple[int, dict[str, Any]]]
    writes: list[dict[str, Any]] | None  # None: the call has no bee-writes.jsonl
    jobs: dict[str, JobFolder]
    mtime: float  # events.jsonl's last change or refresh (voice/recorder.py refreshes it while the call runs)


def numbered_lines(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """(line number from 1, object) for each whole line; a last line without its newline is still being written."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    found = []
    for n, raw in enumerate(data[:data.rfind(b"\n") + 1].split(b"\n")[:-1], start=1):
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append((n, event))
    return found


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _stamp(folder: Path) -> tuple:
    jobs = folder / "jobs"
    try:
        job_dirs = sorted(p for p in jobs.iterdir() if p.is_dir())
    except OSError:
        job_dirs = []
    return (_stat(folder / EVENTS), _stat(folder / WRITES),
            tuple((p.name, _stat(p), _stat(p / "events.jsonl")) for p in job_dirs))


def read_record(folder: Path) -> Record:
    jobs = {}
    try:
        job_dirs = sorted(p for p in (folder / "jobs").iterdir() if p.is_dir())
    except OSError:
        job_dirs = []
    for job in job_dirs:
        try:
            names = {p.name for p in job.iterdir() if p.is_file() and not p.name.startswith(".")}
        except OSError:
            names = set()
        titles = {}
        for name in names:
            if name.lower().endswith(".svg"):  # a chart the worker drew keeps its title in its SVG (worker/chart.py)
                try:
                    with (job / name).open(errors="replace") as f:
                        title = svg_title(f.read(4096))
                except OSError:
                    title = None
                if title:
                    titles[name] = title
        jobs[job.name] = JobFolder(names, titles)
    writes = read_events(folder / WRITES) if (folder / WRITES).exists() else None
    try:
        mtime = (folder / EVENTS).stat().st_mtime
    except OSError:
        mtime = 0.0
    return Record(folder, numbered_lines(folder / EVENTS), writes, jobs, mtime)


class Calls:
    """One person's call records, each read again only when one of its files changed, and what each says."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._records: dict[Path, tuple[tuple, Record]] = {}
        self._calls: dict[tuple[Path, str], tuple[Record, "Call"]] = {}

    def record(self, folder: Path) -> Record:
        stamp = _stamp(folder)
        cached = self._records.get(folder)
        if cached is None or cached[0] != stamp:
            cached = (stamp, read_record(folder))
            self._records[folder] = cached
        return cached[1]

    def call(self, record: Record, zone: tzinfo) -> "Call":
        """What the record says, on the clock of `zone`."""
        key = (record.folder, str(zone))
        cached = self._calls.get(key)
        if cached is None or cached[0] is not record:
            cached = (record, read_call(record, zone))
            self._calls[key] = cached
        return cached[1]

    def all(self) -> list[tuple[str, Record]]:
        """(call id, record) for every record, oldest first."""
        found = call_ids(self.workspace)
        alive = {folder for _, folder in found}
        for gone in [f for f in self._records if f not in alive]:
            del self._records[gone]
        self._calls = {key: value for key, value in self._calls.items() if key[0] in alive}
        return [(call_id, self.record(folder)) for call_id, folder in found]


# ---------- times ----------

def _time(value: Any, zone: tzinfo) -> datetime | None:
    """A stored time on the person's clock; one stored without an offset is taken as theirs."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def _iso(moment: datetime, zone: tzinfo) -> str:
    return moment.astimezone(zone).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _said(value: Any) -> str | None:
    """What the AI said as the person reads it: without audio tags such as [laughs] (voice/audio_tags.py)."""
    return _text(strip_tags(value)) if isinstance(value, str) else None


# ---------- what a record says ----------

@dataclass
class Piece:
    """One final transcript of the person, or one spoken reply of the AI."""
    speaker: str  # person or ai
    line: int
    t: float  # where it sits: the AI's where its voice started, the person's where the transcript was final
    text: str
    interrupted: bool
    caption: bool  # one caption line on the phone (contract section 3)


@dataclass
class Job:
    id: str
    line: int
    started_t: float
    title: str
    step: str | None = None
    end: str | None = None  # done or failed
    end_t: float = 0.0
    end_line: float = LAST
    summary: str | None = None
    files: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Write:
    id: str
    kind: str
    text: str | None
    at: datetime | None  # when it began (its first line in bee-writes.jsonl)
    t: float  # where it sits
    line: float
    state: str  # written, undone, unknown, failed, or applying while it is being made
    ok: bool = False  # the writer's latest status is "ok": the one an undo in the call may take
    retried_at: float | None = None  # applying again after it failed: when the retry after the call began (wall)


@dataclass
class Call:
    """Everything one record says, before any clock is asked."""
    record: Record
    started: datetime
    room: str | None
    direction: str | None = None  # the "call" event's: inbound or outbound
    has_call_event: bool = False
    topic: str | None = None
    reason: str | None = None
    kind: str | None = None  # an AI call's kind: "conversation", "voice note" or none
    continue_from: str | None = None
    language: str | None = None  # the call's language, from the coordinator event
    close_reason: str | None = None  # LiveKit's, as user_initiated
    close_t: float | None = None
    ended_t: float | None = None
    last_t: float = 0.0
    title: str | None = None  # the after-call title (call_titled)
    why: dict[str, Any] | None = None
    prep: dict[str, Any] | None = None
    pieces: list[Piece] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)

    def when(self, t: float) -> datetime:
        return self.started + timedelta(seconds=t)

    def end_t(self) -> float:
        """Where the call ended: its session's close, else its record's, else its last line."""
        for t in (self.close_t, self.ended_t):
            if t is not None:
                return t
        return self.last_t


def _close_reason(value: Any) -> str:
    """LiveKit's close reason as recorded ("CloseReason.USER_INITIATED", or its value)."""
    return str(value or "").rsplit(".", 1)[-1].strip().lower()


def read_call(record: Record, zone: tzinfo) -> Call:
    started, room = None, None
    lines = record.lines
    first = lines[0][1] if lines else {}
    if first.get("type") == "call_started":
        started, room = _time(first.get("started_at"), zone), first.get("room")
    if started is None:  # the folder's own name, <date>/<HHMM>
        name = record.folder.name.split("-")[0]
        try:
            started = datetime.strptime(f"{record.folder.parent.name} {name}", "%Y-%m-%d %H%M").replace(tzinfo=zone)
        except ValueError:
            started = datetime.fromtimestamp(record.mtime, zone)
    call = Call(record, started, room if isinstance(room, str) else None)
    jobs: dict[str, Job] = {}
    has_transcripts = any(e.get("type") == "user_transcript" for _, e in lines)
    t = 0.0
    for n, e in lines:
        t = _number(e.get("t")) if _number(e.get("t")) is not None else t
        kind = e.get("type")
        if kind != "call_titled":  # written after the call, at its last time: never what the call lasted
            call.last_t = max(call.last_t, t)
        if kind == "call":
            call.has_call_event = True
            call.direction = e.get("direction") if isinstance(e.get("direction"), str) else None
            call.topic, call.reason = _text(e.get("topic")), _text(e.get("reason"))
            call.kind = e.get("kind") if isinstance(e.get("kind"), str) else None
            call.continue_from = _text(e.get("continue_from"))
        elif kind == "coordinator" and isinstance(e.get("language"), str):
            call.language = e["language"]
        elif kind == "why_calling" and call.why is None:
            call.why = e
        elif kind == "call_prep" and call.prep is None:
            call.prep = e
        elif kind == "session_closed" and call.close_t is None:
            call.close_t, call.close_reason = t, _close_reason(e.get("reason"))
        elif kind == "call_ended" and call.ended_t is None:
            call.ended_t = t
        elif kind == "call_titled" and _text(e.get("title")):
            call.title = _text(e.get("title"))
        elif kind == "user_transcript" and _text(e.get("text")):
            call.pieces.append(Piece("person", n, t, _text(e.get("text")), False, True))
        elif kind == "message" and e.get("role") == "user" and not has_transcripts and _text(e.get("text")):
            call.pieces.append(Piece("person", n, t, _text(e.get("text")), False, False))
        elif kind == "message" and e.get("role") == "assistant" and _said(e.get("text")):
            metrics = e.get("metrics") if isinstance(e.get("metrics"), dict) else {}
            began, stopped = _number(metrics.get("started_speaking_at")), _number(metrics.get("stopped_speaking_at"))
            spoke = stopped - began if began is not None and stopped is not None else None
            at = t - spoke if spoke is not None and 0 <= spoke <= t else t
            call.pieces.append(Piece("ai", n, at, _said(e.get("text")), bool(e.get("interrupted")), True))
        elif kind in ("job_started", "job_progress", "job_done", "job_failed") and \
                ITEM_ID.fullmatch(f"job-{e.get('job_id')}"):
            job_id = str(e["job_id"])
            job = jobs.get(job_id)
            if job is None:
                job = jobs[job_id] = Job(job_id, n, t, "")
            if kind == "job_started":
                job.line, job.started_t = n, t
                job.title = _text(e.get("title")) or _short(_text(e.get("task")) or f"Job {job_id}")
            elif kind == "job_progress":
                job.step = _text(e.get("text")) or job.step
            elif job.end is None:
                job.end, job.end_t, job.end_line = ("done" if kind == "job_done" else "failed"), t, n
                if kind == "job_done":
                    job.summary = _text(e.get("summary"))
                    job.files = [f for f in e.get("files") or [] if isinstance(f, str)]
                else:
                    job.error = str(e.get("error") or "")
    for job in jobs.values():
        if not job.title:
            job.title = f"Job {job.id}"
    call.jobs = list(jobs.values())
    call.writes = _writes_from_writer(record, call, zone) if record.writes is not None else _writes_from_events(call)
    return call


def _short(task: str) -> str:
    """A job's title from its task, for jobs started before delegate took a title: at most JOB_TITLE_CHARS."""
    task = " ".join(task.split())
    if len(task) <= JOB_TITLE_CHARS:
        return task
    cut = task[:JOB_TITLE_CHARS - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space >= JOB_TITLE_CHARS // 2 else cut).rstrip(" ,.;:") + "…"


def _writes_from_writer(record: Record, call: Call, zone: tzinfo) -> list[Write]:
    """The writes as the Bee writer recorded each step (beelink/writer.py): a write's state is its latest line."""
    events = {}  # key -> where its bee_write event sits
    for n, e in record.lines:
        if e.get("type") == "bee_write" and isinstance(e.get("key"), str) and e["key"] not in events:
            events[e["key"]] = (_number(e.get("t")) or 0.0, n)
    found: dict[str, Write] = {}
    for line in record.writes or []:
        key = line.get("key")
        if not isinstance(key, str) or not ITEM_ID.fullmatch(f"write-{key}"):
            continue
        if key not in found:
            item = line.get("item")
            if not isinstance(item, dict) or item.get("kind") not in KINDS:
                continue
            at = _time(line.get("at"), zone)
            t, n = events.get(key, (((at - call.started).total_seconds() if at else call.last_t), LAST))
            found[key] = Write(f"write-{key}", item["kind"], _text(item.get("text")), at, t, n, "applying")
        write = found[key]
        status = line.get("status")
        retried = _time(line.get("at"), zone) if status == "applying" and write.state == "failed" else None
        write.ok = status == "ok"
        write.state = {"ok": "written", "undo_failed": "written", "undone": "undone", "failed": "failed",
                       "applying": "applying"}.get(status, "unknown")
        write.retried_at = retried.timestamp() if retried else None
    return list(found.values())


def _writes_from_events(call: Call) -> list[Write]:
    """Writes as the call's events tell them, for records without bee-writes.jsonl: the bee_write events of a write
    and its undo (by key, or without keys by kind and text), and before those the writes queued for after the call
    (bee_write_queued, bee_write_done)."""
    writes: list[Write] = []
    by_key: dict[str, Write] = {}
    queued: list[tuple[Write, str]] = []
    for n, e in call.record.lines:
        kind, t = e.get("type"), _number(e.get("t")) or 0.0
        if kind not in ("bee_write", "bee_write_undone", "bee_write_queued", "bee_write_done"):
            continue
        item = e.get("item") if isinstance(e.get("item"), dict) else {}
        write_kind = e.get("kind") if isinstance(e.get("kind"), str) else item.get("kind")
        text = _text(e.get("text")) or _text(item.get("text"))
        key = e.get("key") if isinstance(e.get("key"), str) and e.get("key") else None
        if kind == "bee_write":
            state = "written" if e.get("result") == "ok" else "unknown" if e.get("may_have_gone_in") else "failed"
            earlier = by_key.get(key) if key else (
                next((w for w in writes if w.state == "failed" and w.kind == write_kind and w.text == text), None)
                if e.get("retried") else None)
            if earlier is None:
                earlier = Write(f"write-{key}" if key else f"write-e{n}", str(write_kind), text, call.when(t), t, n,
                                state)
                writes.append(earlier)
                if key:
                    by_key[key] = earlier
            earlier.state, earlier.ok = state, state == "written"
        elif kind == "bee_write_undone":
            if key:
                undone = by_key.get(key)
            else:
                undone = next((w for w in reversed(writes) if w.state == "written"
                               and (not write_kind or w.kind == write_kind) and (not text or w.text == text)), None)
            if undone is not None:
                undone.state, undone.ok = "undone", False
        elif kind == "bee_write_queued":  # not in Bee until its result after the call says so
            ids = f"{item.get('fact_id', '')}|{item.get('suggestion_id', '')}"
            write = Write(f"write-q{sum(w.id.startswith('write-q') for w in writes) + 1}", str(write_kind), text,
                          call.when(t), t, n, "failed")
            queued.append((write, f"{write_kind}|{text or ''}|{ids}"))
            writes.append(write)
        else:  # bee_write_done: the oldest queued write it names still without a result, as the page matched them
            ids = f"{item.get('fact_id', '')}|{item.get('suggestion_id', '')}"
            waiting = [(w, k) for w, k in queued if w.kind == write_kind]
            match = next((w for w, k in waiting if k == f"{write_kind}|{text or ''}|{ids}"), None) or (
                next((w for w, _ in waiting if w.text == text), None) if text else None) or (
                waiting[0][0] if waiting else None)
            if match is not None:
                queued = [(w, k) for w, k in queued if w is not match]
                match.state = "failed" if e.get("ok") is False else "written"
                match.ok = match.state == "written"
    return [w for w in writes if w.kind in KINDS]


# ---------- the clock's part ----------

def state_of(call: Call, wall: float) -> str:
    """live, ended or dropped (the table in contract section 1)."""
    if call.close_reason is not None:
        return "ended" if call.close_reason in ENDED else "dropped"
    if call.ended_t is not None:
        return "dropped"  # its session never closed normally
    return "live" if wall - call.record.mtime <= LIVE_S else "dropped"


# ---------- the thread ----------

def _url(call_id: str, path: str) -> str:
    return f"/calls/{call_id}/files/{path}"


def _files(call_id: str, call: Call, job: Job) -> list[dict[str, Any]]:
    """What a finished job made: charts, the report, tables, others, by name within a kind (contract 2.4)."""
    folder = call.record.jobs.get(job.id) or JobFolder(set(), {})
    listed = {}
    for path in job.files:
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "jobs" or parts[1] != job.id:
            continue
        name = parts[2]
        if name in BOOKKEEPING or USAGE.fullmatch(name) or not FILE_NAME.fullmatch(name) or name not in folder.names:
            continue
        listed[name] = path
    out = []
    for name, path in listed.items():
        stem, suffix = (name.rsplit(".", 1)[0], "." + name.rsplit(".", 1)[1].lower()) if "." in name else (name, "")
        if suffix in IMAGES and f"{stem}.svg" in listed:
            continue  # the picture of a chart the worker drew: the chart's image, not a file of its own
        title, image = None, None
        if suffix == ".svg" and f"{stem}.png" in folder.names:
            kind, title, image = "chart", folder.chart_titles.get(name), _url(call_id, f"jobs/{job.id}/{stem}.png")
        elif suffix in IMAGES:
            kind, title, image = "chart", folder.chart_titles.get(name), _url(call_id, path)
        elif name.lower() == "report.md":
            kind = "report"
        elif suffix in TABLES:
            kind = "table"
        else:
            kind = "other"
        out.append({"name": name, "kind": kind, "url": _url(call_id, path), "title": title, "image_url": image})
    return sorted(out, key=lambda f: (KIND_ORDER[f["kind"]], f["name"]))


def _job_item(call_id: str, call: Call, job: Job, state: str, zone: tzinfo) -> tuple[dict[str, Any], tuple]:
    """The job's card and where it sits: a finished job where it finished, a running one last."""
    started = call.when(job.started_t)
    if job.end is None and state == "live":
        item = {"type": "job", "id": f"job-{job.id}", "at": _iso(started, zone), "title": job.title,
                "state": "running", "started_at": _iso(started, zone), "duration_s": None, "step": job.step,
                "summary": None, "files": [], "failure": None}
        return item, (LAST, job.started_t, job.line)
    if job.end is None:  # the call ended or dropped without the job's end: it did not outlive the call
        end_t, end_line, outcome, failure = call.end_t(), LAST, "failed", "call_ended"
    else:
        end_t, end_line, outcome = job.end_t, job.end_line, job.end
        failure = None if outcome == "done" else ("call_ended" if job.error == "cancelled" else "error")
    item = {"type": "job", "id": f"job-{job.id}", "at": _iso(call.when(end_t), zone), "title": job.title,
            "state": outcome, "started_at": _iso(started, zone),
            "duration_s": max(0, round(end_t - job.started_t)), "step": None,
            "summary": job.summary if outcome == "done" else None,
            "files": _files(call_id, call, job) if outcome == "done" else [], "failure": failure}
    return item, (end_t, end_line)


def items_of(call_id: str, call: Call, state: str, zone: tzinfo,
             wall: float) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The thread's items in the order to draw (contract 2.4, 2.5), and how many caption lines they hold."""
    placed: list[tuple[tuple, Any]] = [((p.t, p.line), p) for p in call.pieces]
    docked = []
    for job in call.jobs:
        item, where = _job_item(call_id, call, job, state, zone)
        (docked if item["state"] == "running" else placed).append((where, item))
    live = state == "live"
    last_ok = next((w for w in reversed(call.writes) if w.ok), None) if live else None
    for write in call.writes:
        if write.state == "applying" and live:
            continue  # still being made: it shows once Bee answered
        shown = write.state
        if shown == "applying":  # left by a stopped process, unless it is the retry after the call, still running
            retrying = write.retried_at is not None and wall - write.retried_at < RETRY_SETTLES_S
            shown = "failed" if retrying else "unknown"
        item = {"type": "bee_write", "id": write.id, "at": _iso(write.at or call.when(write.t), zone),
                "kind": write.kind, "text": write.text, "state": shown,
                "will_retry": live and write.state == "failed", "undoable": write is last_ok}
        placed.append(((write.t, write.line), item))
    placed.sort(key=lambda entry: entry[0])
    items: list[dict[str, Any]] = []
    run: dict[str, Any] | None = None
    captions = {"person": 0, "ai": 0}
    for _, thing in placed:
        if isinstance(thing, Piece):
            captions[thing.speaker] += thing.caption
            if run is not None and run["speaker"] == thing.speaker:
                run["text"] += " " + thing.text
                run["interrupted"] = thing.interrupted
                continue
            run = {"type": "speech", "id": f"speech-{thing.line}", "at": _iso(call.when(thing.t), zone),
                   "speaker": thing.speaker, "text": thing.text, "interrupted": thing.interrupted}
            items.append(run)
        else:
            run = None
            items.append(thing)
    items += [item for _, item in sorted(docked, key=lambda entry: entry[0])]
    return items, captions


# ---------- the call's summary ----------

def started_by(call: Call, rang: dict[str, dict[str, Any]]) -> str:
    if call.has_call_event:
        return "ai" if call.direction == "outbound" else "person"
    if call.room in rang:
        return "ai"
    return "ai" if call.why and call.why.get("conversation_id") else "person"  # records from before the call event


def first_sentence(text: str) -> str:
    """The person's first sentence as a title, at most TITLE_CHARS."""
    found = SENTENCE_END.search(text)
    sentence = " ".join((text[:found.end()] if found else text).split())
    if len(sentence) <= TITLE_CHARS:
        return sentence
    cut = sentence[:TITLE_CHARS - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space >= TITLE_CHARS // 2 else cut).rstrip(" ,.;:") + "…"


def _continues(value: str | None, ids: dict[str, str], known: set[str]) -> str | None:
    """The earlier call's id, from a continue_from that names it by id or by its folder (<date>/<time>)."""
    if not value:
        return None
    if value in known:
        return value
    return ids.get(value)


@dataclass
class Context:
    """What a build of the list or a view knows beyond one record."""
    zone: tzinfo
    language: str
    wall: float
    rang: dict[str, dict[str, Any]]  # room -> its ring line (state/outbound.jsonl)
    by_folder: dict[str, str]  # <date>/<time> -> call id
    ids: set[str]


def summary(call_id: str, call: Call, ctx: Context) -> tuple[dict[str, Any], list[dict[str, Any]], dict]:
    """The call summary (section 1), with its items and caption counts (section 2)."""
    state = state_of(call, ctx.wall)
    items, captions = items_of(call_id, call, state, ctx.zone, ctx.wall)
    by = started_by(call, ctx.rang)
    ring = ctx.rang.get(call.room or "", {})
    language = call.language or ctx.language
    topic = topic_line(call.topic or _text(ring.get("topic")), call.kind or ring.get("kind"), language) \
        if by == "ai" else None
    person = next((i["text"] for i in items if i["type"] == "speech" and i["speaker"] == "person"), None)
    if call.title:
        title, source = call.title, "summary"
    elif topic:
        title, source = topic, "topic"
    elif person:
        title, source = first_sentence(person), "first_sentence"
    else:
        title, source = None, "none"
    kinds = {f["kind"] for i in items if i["type"] == "job" and i["state"] == "done" for f in i["files"]}
    entry = {
        "call_id": call_id, "started_at": _iso(call.started, ctx.zone), "started_by": by, "topic": topic,
        "title": title, "title_source": source, "state": state,
        "duration_s": None if state == "live" else max(0, round(call.end_t())),
        "continues": _continues(call.continue_from, ctx.by_folder, ctx.ids),
        "marks": {"report": "report" in kinds, "table": "table" in kinds, "chart": "chart" in kinds,
                  "bee_writes": sum(1 for i in items if i["type"] == "bee_write" and i["state"] == "written")},
    }
    return entry, items, captions


def why_calling(call: Call, ctx: Context) -> dict[str, Any] | None:
    """Why the AI called (2.2): the call's reason, and the Bee conversation behind it."""
    reason = call.reason or _text(ctx.rang.get(call.room or "", {}).get("reason"))
    why = call.why or {}
    quotes = []
    for u in why.get("utterances") or []:
        if not isinstance(u, dict) or not _text(u.get("text")):
            continue
        quotes.append({"speaker": "person" if u.get("speaker") == "me" else "other", "text": _text(u.get("text")),
                       "at": _quote_time(u.get("at"), call.started, ctx.zone)})
    conversation = None
    if why.get("conversation_id") or quotes or _text(why.get("summary")):
        conversation = {"summary": _text(why.get("summary")), "quotes": quotes}
    return {"reason": reason, "conversation": conversation}


def _quote_time(value: Any, started: datetime, zone: tzinfo) -> str | None:
    """A quote's time; older records kept only "HH:MM", the last such time before the call."""
    if not isinstance(value, str):
        return None
    clock = CLOCK_ONLY.fullmatch(value.strip())
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            return None
        moment = started.astimezone(zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return _iso(moment - timedelta(days=1) if moment > started else moment, zone)
    moment = _time(value, zone)
    return _iso(moment, zone) if moment else None


def without_ids(text: str) -> str:
    """The prep note without Bee's ids (web/app.js withoutIds, moved here)."""
    text = ID_GROUP.sub("", text)
    text = ID_AFTER_WORD.sub(r"\1", text)
    text = BARE_ID.sub(r"\1", text)
    text = SPACE_BEFORE_MARK.sub(r"\1", text)
    return SPACES.sub(" ", text).strip()


def prep_card(call: Call, by: str, zone: tzinfo) -> dict[str, Any] | None:
    """What the AI looked at before it picked up (2.3)."""
    prep = call.prep
    if prep is None:
        return None
    note = without_ids(prep["note"]) if isinstance(prep.get("note"), str) else ""
    reads = prep.get("reads") if isinstance(prep.get("reads"), list) else []
    why = call.why or {}
    behind = why.get("conversation_id")
    entries: list[dict[str, Any]] = []
    if reads and all(isinstance(r, dict) for r in reads):
        for read in reads:
            entries.append(dict(read))
    else:  # stored as "tool {json}" text, without the conversation behind an AI call
        if by == "ai" and behind:
            entries.append({"type": "conversation", "id": behind, "summary": why.get("summary"),
                            "at": why.get("started_at")})
        for read in reads:
            if not isinstance(read, str):
                continue
            name, _, rest = read.partition(" ")
            try:
                args = json.loads(rest) if rest else {}
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            if name == "search_bee":
                entries.append({"type": "search", "query": args.get("query")})
            elif name == "bee_day":
                entries.append({"type": "day", "date": args.get("date")})
            elif name == "bee_conversation":
                same = args.get("conversation_id") == behind and behind
                entries.append({"type": "conversation", "id": args.get("conversation_id"),
                                "summary": why.get("summary") if same else None,
                                "at": why.get("started_at") if same else None})
    looked_up, seen = [], set()
    for entry in entries:
        kind = entry.get("type")
        if kind == "conversation":
            at = _time(entry.get("at"), zone)
            shown = {"type": "conversation", "summary": _text(entry.get("summary")), "at": _iso(at, zone) if at else None}
            named = entry.get("id")  # Bee's id: a number, or a string in a record that kept it so
            key = ("conversation", named if isinstance(named, (int, str)) else (shown["summary"], shown["at"]))
        elif kind == "day" and isinstance(entry.get("date"), str) and DAY.fullmatch(entry["date"]):
            shown, key = {"type": "day", "date": entry["date"]}, ("day", entry["date"])
        elif kind == "search" and _text(entry.get("query")):
            shown, key = {"type": "search", "query": _text(entry.get("query"))}, ("search", _text(entry.get("query")))
        else:
            continue
        if key not in seen:
            seen.add(key)
            looked_up.append(shown)
    bee = prep.get("bee")
    from_bee = bee if isinstance(bee, bool) else bool(
        reads or call.why or (call.record.folder / "inputs").is_dir())
    return {"note": note or None, "looked_up": looked_up, "from_bee": from_bee}


# ---------- versions and cursors ----------

def version_of(body: dict[str, Any]) -> str:
    """16 hex characters that change whenever the body does (also the ETag)."""
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cursor(started: str, call_id: str) -> str:
    return base64.urlsafe_b64encode(f"{started}|{call_id}".encode()).rstrip(b"=").decode()


def _read_cursor(before: str) -> tuple[float, str]:
    if not isinstance(before, str) or not 0 < len(before) <= 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", before):
        raise BadRequest("before is not a cursor this server gave out")
    try:
        raw = base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)).decode()
        started, call_id = raw.split("|", 1)
        moment = datetime.fromisoformat(started)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadRequest("before is not a cursor this server gave out") from None
    if moment.tzinfo is None or not CALL_ID.fullmatch(call_id):
        raise BadRequest("before is not a cursor this server gave out")
    return moment.timestamp(), call_id


# ---------- the list and the view ----------

def _rings(workspace: Path, zone: tzinfo) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The ring lines by room (the first for a room), and the voice server's verdict for each unanswered one (the
    first verdict for a room is its verdict; a decline after it changes nothing)."""
    rang: dict[str, dict[str, Any]] = {}
    for line in read_events(workspace / OUTBOUND_LOG):
        room = line.get("room")
        if isinstance(room, str) and CALL_ID.fullmatch(room) and room not in rang and _time(line.get("at"), zone):
            rang[room] = line
    verdicts: dict[str, str] = {}
    for event in read_events(workspace / "meta" / "events.jsonl"):
        room = event.get("room")
        if event.get("kind") in ("call_missed", "call_declined") and isinstance(room, str):
            verdicts.setdefault(room, "missed" if event["kind"] == "call_missed" else "declined")
    return rang, verdicts


def _unanswered(rang: dict[str, dict[str, Any]], verdicts: dict[str, str], recorded: set[str], now: datetime,
                zone: tzinfo, language: str) -> list[dict[str, Any]]:
    """The AI's calls that rang without being answered: missed or declined (section 1)."""
    out = []
    for room, line in rang.items():
        if room in recorded:
            continue
        at = _time(line["at"], zone)
        expires = _time(line.get("expires_at"), zone) or at + timedelta(seconds=60)
        state = verdicts.get(room) or ("missed" if now >= expires else None)
        if state is None:
            continue  # still ringing: it shows once answered, missed or declined
        topic = topic_line(_text(line.get("topic")), line.get("kind"), language)
        out.append({"call_id": room, "started_at": _iso(at, zone), "started_by": "ai", "topic": topic,
                    "title": topic, "title_source": "topic", "state": state, "duration_s": None, "continues": None,
                    "marks": {"report": False, "table": False, "chart": False, "bee_writes": 0}, "results": []})
    return out


def _context(workspace: Path, records: list[tuple[str, Record]], zone: tzinfo, language: str,
             wall: float) -> tuple[Context, dict[str, str]]:
    rang, verdicts = _rings(workspace, zone)
    ctx = Context(zone, language, wall, rang, {session_id(r.folder): call_id for call_id, r in records},
                  {call_id for call_id, _ in records})
    return ctx, verdicts


def call_list(workspace: Path, *, zone: tzinfo, language: str, now: datetime, wall: float | None = None,
              limit: int = DEFAULT_LIMIT, before: str | None = None, cache: Calls | None = None) -> dict[str, Any]:
    """GET /calls: every call, newest first, a page at a time (section 1). BadRequest for a bad limit or cursor."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise BadRequest(f"limit must be a whole number from 1 to {MAX_LIMIT}")
    after = _read_cursor(before) if before is not None else None
    cache = cache or Calls(workspace)
    records = cache.all()
    ctx, verdicts = _context(workspace, records, zone, language, time.time() if wall is None else wall)
    calls = {call_id: cache.call(record, zone) for call_id, record in records}
    rooms = {call.room for call in calls.values() if call.room}
    entries: list[tuple[tuple[float, str], Any]] = [((c.started.timestamp(), call_id), call_id)
                                                     for call_id, c in calls.items()]
    for ring in _unanswered(ctx.rang, verdicts, rooms, now, zone, language):
        entries.append(((datetime.fromisoformat(ring["started_at"]).timestamp(), ring["call_id"]), ring))
    entries.sort(key=lambda entry: entry[0], reverse=True)
    if after is not None:
        entries = [entry for entry in entries if entry[0] < after]
    page, more = entries[:limit], len(entries) > limit
    out = []
    for _, found in page:
        if isinstance(found, dict):
            out.append(found)
            continue
        entry, items, _ = summary(found, calls[found], ctx)
        entry["results"] = [i for i in items if i["type"] in ("job", "bee_write")]
        out.append(entry)
    body: dict[str, Any] = {"calls": out, "more": more,
                            "before": _cursor(out[-1]["started_at"], out[-1]["call_id"]) if more and out else None}
    body["version"] = version_of(body)
    return body


def call_view(workspace: Path, call_id: str, *, zone: tzinfo, language: str, now: datetime,
              wall: float | None = None, cache: Calls | None = None) -> dict[str, Any] | None:
    """GET /calls/{call_id}: one call as its thread (section 2); None for a call with no record (404)."""
    cache = cache or Calls(workspace)
    records = cache.all()
    record = next((r for found, r in records if found == call_id), None)
    if record is None:
        return None
    ctx, _ = _context(workspace, records, zone, language, time.time() if wall is None else wall)
    call = cache.call(record, zone)
    entry, items, captions = summary(call_id, call, ctx)
    body: dict[str, Any] = {"call": entry, "why_calling": why_calling(call, ctx) if entry["started_by"] == "ai" else None,
                            "prep": prep_card(call, entry["started_by"], zone), "items": items,
                            "captions_included": captions}
    body["version"] = version_of(body)
    return body


# ---------- what /changes watches (contract section 3) ----------

def watched_calls(workspace: Path, *, zone: tzinfo, now: datetime, wall: float | None = None,
                  cache: Calls | None = None) -> list[tuple[str, Path]]:
    """The calls /changes announces on connect and rebuilds: those live, those that ended in the last RECENT_S (a
    title and Bee retries arrive after the end), and those whose record changed in that time."""
    cache = cache or Calls(workspace)
    wall = time.time() if wall is None else wall
    found = []
    for call_id, record in cache.all():
        call = cache.call(record, zone)
        if state_of(call, wall) == "live" or wall - record.mtime <= RECENT_S \
                or (now - call.when(call.end_t())).total_seconds() <= RECENT_S:
            found.append((call_id, record.folder))
    return found


def files_stamp(workspace: Path, folders: list[Path]) -> tuple:
    """What /changes looks at between its rebuilds: new call folders, the records of the newest days (a new call's
    record appears and gets its first line after its folder is made), the ring log and its verdicts, the offers, and
    the files of the calls it watches. Equal stamps: nothing a body shows changed but by the clock."""
    try:
        days = sorted(p for p in workspace.iterdir() if DAY.fullmatch(p.name))
    except OSError:
        days = []
    newest = []
    for day in days[-NEWEST_DAYS:]:
        try:
            newest += [(day.name, p.name, _stat(p / EVENTS)) for p in sorted(day.iterdir())]
        except OSError:
            continue
    return (tuple((p.name, _stat(p)) for p in days), tuple(newest), _stat(workspace / OUTBOUND_LOG),
            _stat(workspace / "meta" / "events.jsonl"), _stat(workspace / OFFERS_DIR),
            tuple(_stamp(folder) for folder in folders))
