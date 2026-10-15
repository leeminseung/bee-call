"""Reading a person's call folders: <workspace>/<date>/<time>/ (voice/recorder.py writes them).

Each call's events.jsonl is appended to while the call runs, and also after it (Bee writes tried again), and other
logs of one JSON object per line (state/outbound.jsonl) are appended to by other processes, so a reader can meet a
last line still being written. read_events() is the one way to read such a file: it keeps every line that parses
and skips the rest, so a half-written line never stops a reader (server review finding 13). call_folders() lists
the call folders, call_ids() names them as the app does (contract/app-contract.md, section 1), and resolve_inside()
is the one rule for which of a call's files a web server may serve (both servers use it).
"""

import json
import os
import re
from pathlib import Path
from typing import Any

EVENTS = "events.jsonl"
# A call's private parts: raw call audio, raw Bee data, and everything the call AI's model was given (which holds Bee
# data): the model log of every call and the Claude adapter's history (BC-095). No web server ever serves them
# (resolve_inside) and the meta agent never reads them (meta/files.py). The one list (server review 19).
PRIVATE = ("audio", "inputs", "model-log.jsonl", "claude-history.json")
# A call's id: its LiveKit room name, the same string as `room` in GET /token and in a ring (contract section 1).
CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def call_folders(workspace: Path) -> list[Path]:
    """The person's call folders that have a record, oldest first (by their <date>/<time> names)."""
    return sorted(path.parent for path in workspace.glob(f"*/*/{EVENTS}"))


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every whole JSON object in a file of one per line (a call's events.jsonl, the outbound log), in order;
    [] when it cannot be read."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line still being written, or a broken one
        if isinstance(event, dict):
            events.append(event)
    return events


def first_event(path: Path) -> dict[str, Any] | None:
    """The first line of a file of one JSON object per line (a record's call_started), without reading the rest."""
    try:
        with path.open("rb") as f:
            line = f.readline()
    except OSError:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def session_id(folder: Path) -> str:
    """A call folder's id by its place: <date>/<time>, as voice/continuation.py and the records server name it."""
    return f"{folder.parent.name}/{folder.name}"


def call_ids(workspace: Path) -> list[tuple[str, Path]]:
    """Each call record with its id, oldest first (contract section 1): the LiveKit room of its call_started line,
    and for a later record in the same room (a voice server restarted during the call) <room>.2, then .3. A record
    without a usable room is named by its folder (rec-<date>-<time>)."""
    named: list[tuple[str, Path]] = []
    seen: dict[str, int] = {}
    for folder in call_folders(workspace):
        room = (first_event(folder / EVENTS) or {}).get("room")
        if not isinstance(room, str) or not CALL_ID.fullmatch(room) or len(room) > 120:
            room = f"rec-{folder.parent.name}-{folder.name}"
        seen[room] = seen.get(room, 0) + 1
        named.append((room if seen[room] == 1 else f"{room}.{seen[room]}", folder))
    return named


def find_call(workspace: Path, call_id: str) -> Path | None:
    """The folder of the record with this call id, or None."""
    return next((folder for found, folder in call_ids(workspace) if found == call_id), None)


# A served call file shows as text when it is text, and nothing served can run script on the server's origin: job files
# are made from outside content (a sandboxed response, no type sniffing).
PLAIN_TEXT = (".md", ".txt", ".log", ".csv", ".tsv", ".jsonl")
SERVED_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


def served_headers(target: Path) -> dict[str, str]:
    """The headers a call file is served with, by both web servers."""
    headers = dict(SERVED_HEADERS)
    if target.suffix.lower() in PLAIN_TEXT:
        headers["Content-Type"] = "text/plain; charset=utf-8"
    return headers


class NotServed(Exception):
    """A path no web server serves: a call's private part, or one that leaves its folder (403)."""


def resolve_inside(folder: Path, rel: str) -> Path:
    """The real path of `rel` inside a call's folder, for a web server to serve. NotServed for a private part (any
    spelling of its name, or a link to it) or a path that leaves the folder; FileNotFoundError for anything else
    that is not a file there."""
    parts = rel.split("/")
    if (rel.startswith("/") or "\\" in rel or "\x00" in rel
            or any(part in ("", ".", "..") for part in parts)):
        raise NotServed("path not allowed")
    if parts[0].casefold() in PRIVATE:  # a call's private parts are never served (BC-095)
        raise NotServed(f"{parts[0]} is never served")
    base = folder.resolve()
    try:
        target = (base / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        raise FileNotFoundError(rel) from None
    if target == base or not target.is_relative_to(base):
        raise NotServed("path leaves the call's folder")
    first = base / target.relative_to(base).parts[0]  # catches a link into audio/ and AUDIO/ on macOS
    for name in PRIVATE:
        private = base / name
        if first.name.casefold() == name or (private.exists() and os.path.samefile(first, private)):
            raise NotServed(f"{name} is never served")
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target
