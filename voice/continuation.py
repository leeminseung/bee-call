"""A call that continues an earlier one.

The user opens an earlier call's record in the app and taps "Continue this call". The app asks the token server
for a pass with that call's id (continue=<call_id>, its LiveKit room; app builds from before design D send the
record's folder, continue=<date>/<time>), and the voice server starts the new call knowing what was said in the
earlier one, what its background jobs found, and which jobs stopped unfinished when it ended.
Nothing restarts by itself: a stopped job is only mentioned, and the user decides whether to run it again. The AI
never calls back on its own to continue; a continued call is always one the user starts.
"""

import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from voice.call_folders import CALL_ID, EVENTS, find_call, read_events, session_id as folder_id
from worker.runner import job_alive

SESSION_ID = re.compile(r"\d{4}-\d{2}-\d{2}/\d{4}(?:-\d+)?")  # workspace/<date>/<time>, see voice/recorder.py
MAX_TALK_CHARS = 6000  # arbitrary: about ten minutes of talk; an older start is left out
MAX_RESULT_CHARS = 1200  # arbitrary: one job's summary


def session_dir(workspace: Path, session_id: Any) -> Path | None:
    """The folder of an earlier call named by its call id or by its folder (<date>/<time>), or None when it names no
    recorded call (only ids of those shapes, so a request cannot name any other folder)."""
    if not isinstance(session_id, str):
        return None
    if SESSION_ID.fullmatch(session_id):
        folder = workspace / session_id
        return folder if (folder / "events.jsonl").is_file() else None
    return find_call(workspace, session_id) if CALL_ID.fullmatch(session_id) else None


def record_id(workspace: Path, session_id: Any) -> str | None:
    """The earlier call's folder (<date>/<time>), as its record names it (the call event's continue_from, which the
    records page links to), from either form of its id; None when it names no recorded call."""
    folder = session_dir(workspace, session_id)
    return None if folder is None else folder_id(folder)


def _events(folder: Path) -> list[dict[str, Any]]:
    return read_events(folder / EVENTS)


def _talk(folder: Path) -> str:
    """The transcript's lines, without their timestamps: "AI: ..." and "User: ..."."""
    try:
        lines = (folder / "transcript.md").read_text(errors="replace").splitlines()
    except OSError:
        return ""
    talk: list[str] = []
    for line in lines:
        m = re.match(r"- [\d:.]+ \*\*(AI|User):\*\* (.*)", line)
        if m:
            talk.append(f"{m.group(1)}: {m.group(2).strip()}")
        elif talk and line.strip() and not line.startswith("#"):  # a reply of several paragraphs
            talk[-1] += " " + line.strip()
    text = "\n".join(talk)
    if len(text) > MAX_TALK_CHARS:
        text = "(the start of the call is left out)\n" + text[-MAX_TALK_CHARS:].split("\n", 1)[-1]
    return text


def _jobs(events: list[dict[str, Any]], folder: Path) -> list[str]:
    jobs: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("type") not in ("job_started", "job_done", "job_failed"):
            continue
        job = jobs.setdefault(str(e.get("job_id")), {})
        if e["type"] == "job_started":
            job["task"] = e.get("task")
        elif e["type"] == "job_done":
            job.update(state="done", summary=e.get("summary"), files=e.get("files") or [])
        else:
            job.update(state="cancelled" if e.get("error") == "cancelled" else "failed", error=e.get("error"))
    lines = []
    for job_id, job in jobs.items():
        # What the worker wrote comes from web pages too: escaped, so it never closes a block and speaks as the app
        # (the call AI's instructions say a job's words are never instructions).
        task = escape(str(job.get("task") or "a task").strip(), quote=False)
        state = job.get("state")
        if state == "done":
            summary = escape(str(job.get("summary") or "").strip()[:MAX_RESULT_CHARS], quote=False)
            names = [str(f).split("/")[-1] for f in job.get("files") or [] if f]
            files = escape(", ".join(n for n in names if n != "usage.json") or "none", quote=False)  # bookkeeping
            lines.append(f"- Job {job_id}, {task}\n  Finished. Result: {summary}\n  Files in the app: {files}")
        elif state == "failed":
            error = escape(str(job.get("error") or "").strip()[:300], quote=False)
            lines.append(f"- Job {job_id}, {task}\n  Failed: {error}")
        elif state is None and job_alive(folder / "jobs" / job_id / "events.jsonl"):  # it outlived that call
            lines.append(f"- Job {job_id}, {task}\n  Still running; its result will reach the app, with a "
                         "notification. Do not start it again.")
        else:  # stopped unfinished: cancelled when its call ended (calls before 2026-10-16), or cut off since
            lines.append(f"- Job {job_id}, {task}\n  Stopped unfinished when that call ended. Start it again only "
                         "if the user wants it.")
    return lines


def earlier_call(workspace: Path, session_id: Any) -> str | None:
    """What the new call needs to know about the earlier one, or None when there is no such call."""
    folder = session_dir(workspace, session_id)
    if folder is None:
        return None
    events = _events(folder)
    started = next((e.get("started_at") for e in events if e.get("type") == "call_started"), None)
    try:
        when = datetime.fromisoformat(started).strftime("%a %b %-d, %-I:%M %p") if started else session_id
    except ValueError:
        when = session_id
    ended = next((e.get("t") for e in events if e.get("type") in ("call_ended", "session_closed")), None)
    length = f", about {round(ended / 60) or 1} min" if isinstance(ended, (int, float)) else ""
    parts = [f"The earlier call: {when}{length}."]
    talk = _talk(folder)
    parts.append("What was said (AI is you):\n" + talk if talk else "Nothing was said in it.")
    jobs = _jobs(events, folder)
    if jobs:
        parts.append("Background jobs in that call:\n" + "\n".join(jobs))
    return "\n\n".join(parts)


LAST_CALLS = 3  # the earlier calls a call's prep reads (arbitrary, the user's amendment of 2026-10-18)
LAST_LINES = 4  # of each, its last lines (arbitrary)
LINE_CHARS = 200  # of each line (arbitrary)
ENDED_BY_HANGING_UP = ("participant_disconnected",)  # LiveKit's close reason when the phone left the room
ENDED = ("user_initiated", "participant_disconnected", "task_completed")  # a normal end (voice/call_view.py)


def how_it_ended(events: list[dict[str, Any]]) -> str:
    """How a recorded call ended, in a few words for a model."""
    reason = next((str(e.get("reason") or "").rsplit(".", 1)[-1].strip().lower() for e in events
                   if e.get("type") == "session_closed"), None)
    if any(e.get("type") == "end_call_requested" for e in events):
        return "it ended with a goodbye, and you ended the call"
    if reason in ENDED_BY_HANGING_UP:
        return "the user hung up"
    if reason in ENDED or (reason == "" and any(e.get("type") == "call_ended" for e in events)):
        return "it ended"
    if reason:
        return "it dropped"
    return "it ended" if any(e.get("type") == "call_ended" for e in events) else "no end is on record"


def last_calls(workspace: Path, zone, count: int = LAST_CALLS, skip: str | None = None) -> str | None:
    """The person's last `count` calls for a call's prep, oldest first, built from their records (no model call): each
    with its title, when it was, how it ended and its last few lines; None before their first call. `skip`: a call's
    folder (<date>/<time>) left out, as the earlier call a continued call has whole."""
    from voice.call_folders import call_folders

    found = [folder for folder in call_folders(workspace) if folder_id(folder) != skip][-count:]
    parts = []
    for folder in found:
        events = _events(folder)
        started = next((e.get("started_at") for e in events if e.get("type") == "call_started"), None)
        try:
            when = datetime.fromisoformat(started).astimezone(zone).strftime("%a %b %-d, %-I:%M %p")
        except (TypeError, ValueError):
            when = folder_id(folder)
        titles = [e.get("title") for e in events if e.get("type") == "call_titled" and e.get("title")]
        title = f'"{titles[-1]}"' if titles else "no title"
        talk = [line.replace("AI: ", "AI (you): ", 1) for line in _talk(folder).splitlines() if line.strip()]
        lines = "\n".join(f"   {line[:LINE_CHARS]}{'…' if len(line) > LINE_CHARS else ''}"
                          for line in talk[-LAST_LINES:]) or "   (nothing was said)"
        parts.append(f"- {when}, {title}: {how_it_ended(events)}. Its last lines:\n{lines}")
    if not parts:
        return None
    return ("Your last calls with the user, oldest first (the last one is the call just before this one):\n"
            + "\n".join(parts))
