"""Reading a person's call folders: <workspace>/<date>/<time>/ (voice/recorder.py writes them).

Each call's events.jsonl is appended to while the call runs, and also after it (Bee writes tried again), so a
reader can meet a last line still being written. read_events() is the one way to read it: it keeps every line that
parses and skips the rest, so a half-written line never stops a reader (server review finding 13).
"""

import json
from pathlib import Path
from typing import Any

EVENTS = "events.jsonl"


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every whole event in an events.jsonl, in order; [] when it cannot be read."""
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
