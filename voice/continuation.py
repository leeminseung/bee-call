"""A call that continues an earlier one.

The user opens an earlier call's record in the app and taps "Continue this call". The app asks the token server
for a pass with that record's id (continue=<date>/<time>), and the voice server starts the new call knowing what
was said in the earlier one, what its background jobs found, and which jobs stopped unfinished when it ended.
Nothing restarts by itself: a stopped job is only mentioned, and the user decides whether to run it again. The AI
never calls back on its own to continue; a continued call is always one the user starts.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_ID = re.compile(r"\d{4}-\d{2}-\d{2}/\d{4}(?:-\d+)?")  # workspace/<date>/<time>, see voice/recorder.py
MAX_TALK_CHARS = 6000  # arbitrary: about ten minutes of talk; an older start is left out
MAX_RESULT_CHARS = 1200  # arbitrary: one job's summary


def session_dir(workspace: Path, session_id: Any) -> Path | None:
    """The folder of an earlier call, or None when the id is not one (only ids of the recorder's shape, so a
    request cannot name any other folder)."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    folder = workspace / session_id
    return folder if (folder / "events.jsonl").is_file() else None


def _events(folder: Path) -> list[dict[str, Any]]:
    events = []
    for line in (folder / "events.jsonl").read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


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
        task = str(job.get("task") or "a task").strip()
        state = job.get("state")
        if state == "done":
            summary = str(job.get("summary") or "").strip()[:MAX_RESULT_CHARS]
            names = [str(f).split("/")[-1] for f in job.get("files") or [] if f]
            files = ", ".join(n for n in names if n != "usage.json") or "none"  # usage.json is bookkeeping
            lines.append(f"- Job {job_id}, {task}\n  Finished. Result: {summary}\n  Files in the app: {files}")
        elif state == "failed":
            lines.append(f"- Job {job_id}, {task}\n  Failed: {str(job.get('error') or '').strip()[:300]}")
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
