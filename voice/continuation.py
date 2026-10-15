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


def _jobs(events: list[dict[str, Any]]) -> list[str]:
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
        else:  # cancelled when the call ended, or still running then
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
    jobs = _jobs(events)
    if jobs:
        parts.append("Background jobs in that call:\n" + "\n".join(jobs))
    return "\n\n".join(parts)
