"""Time windows of our own AI calls, read from the session folders.

Bee also records our AI calls (the watch hears the user talking to us). For
those windows our own recording is the better source, so Bee records that
overlap one of our calls are skipped.
"""

import json
from datetime import datetime
from pathlib import Path

MARGIN_SECONDS = 60.0


def our_call_windows(workspace: Path) -> list[tuple[float, float]]:
    windows = []
    for events in workspace.glob("*/*/events.jsonl"):
        lines = events.read_text().splitlines()
        if not lines:
            continue
        first = json.loads(lines[0])
        if first.get("type") != "call_started" or "started_at" not in first:
            continue
        start = datetime.fromisoformat(first["started_at"]).timestamp()
        # The call ends at call_ended; lines after it (Bee write results) come later and are not the call.
        events_seen = [json.loads(line) for line in lines if line.strip()]
        ended = [e.get("t", 0.0) for e in events_seen if e.get("type") == "call_ended"]
        end_t = ended[0] if ended else events_seen[-1].get("t", 0.0)
        windows.append((start, start + float(end_t or 0.0)))
    return windows


def overlaps_our_call(start_ms: int | None, end_ms: int | None, windows: list[tuple[float, float]]) -> bool:
    if start_ms is None:
        return False
    start = start_ms / 1000.0
    end = (end_ms / 1000.0) if end_ms else start
    return any(start <= w_end + MARGIN_SECONDS and end >= w_start - MARGIN_SECONDS for w_start, w_end in windows)
