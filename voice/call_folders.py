"""Reading a person's call folders: <workspace>/<date>/<time>/ (voice/recorder.py writes them).

Each call's events.jsonl is appended to while the call runs, and also after it (Bee writes tried again), and other
logs of one JSON object per line (state/outbound.jsonl) are appended to by other processes, so a reader can meet a
last line still being written. read_events() is the one way to read such a file: it keeps every line that parses
and skips the rest, so a half-written line never stops a reader (server review finding 13). call_folders() lists
the call folders.
"""

import json
from pathlib import Path
from typing import Any

EVENTS = "events.jsonl"


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
