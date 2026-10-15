"""Time windows of our own calls, read from the session folders.

Bee also records our calls (the wearable hears the user talking to us). For those windows our own record is the
source, so what Bee made of them is never read as new context (BC-060): its conversations that overlap one of our
calls, the facts and todos it made from them, and a voice note recorded during one. OurCalls holds that rule for
every reader: the watcher's batches (beelink/watcher.py), a call's Bee inputs (beelink/context.py) and the reads of
the call AI, the prep and the meta agent (beelink/reads.py).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beelink.clock import parse_time
from voice.call_folders import EVENTS, call_folders, read_events

MARGIN_SECONDS = 60.0
# Bee derives facts and todos from a conversation some time after it ends. What it made from one of our calls
# up to this long after the call is taken as made from that call (a guess: Bee does not say which conversation a
# fact came from). A fact from another conversation in that time is left out too, the cost of the rule.
DERIVED_AFTER_SECONDS = 600.0


def our_call_windows(workspace: Path) -> list[tuple[float, float]]:
    windows = []
    for folder in call_folders(workspace):
        events = read_events(folder / EVENTS)  # the call in progress may have a line still being written
        if not events:
            continue
        first = events[0]
        if first.get("type") != "call_started" or "started_at" not in first:
            continue
        try:
            start = parse_time(str(first["started_at"])).timestamp()
        except ValueError:
            continue
        # The call ends at call_ended; lines after it (Bee write results) come later and are not the call.
        ended = [e.get("t", 0.0) for e in events if e.get("type") == "call_ended"]
        end_t = ended[0] if ended else events[-1].get("t", 0.0)
        windows.append((start, start + float(end_t or 0.0)))
    return windows


def overlaps_our_call(start_ms: int | None, end_ms: int | None, windows: list[tuple[float, float]]) -> bool:
    if start_ms is None:
        return False
    start = start_ms / 1000.0
    end = (end_ms / 1000.0) if end_ms else start
    return any(start <= w_end + MARGIN_SECONDS and end >= w_start - MARGIN_SECONDS for w_start, w_end in windows)


def _seconds(value: Any) -> float | None:
    """A Bee time as epoch seconds: milliseconds, or an ISO string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000.0
    if isinstance(value, str):
        try:
            return parse_time(value).timestamp()
        except ValueError:
            return None
    return None


@dataclass
class OurCalls:
    """What Bee recorded during our own calls, which is never read as new context (BC-060)."""
    windows: list[tuple[float, float]]

    @classmethod
    def of(cls, workspace: Path) -> "OurCalls":
        return cls(our_call_windows(workspace))

    def conversation(self, item: Any) -> bool:
        """A Bee conversation that overlaps one of our calls."""
        return isinstance(item, dict) and overlaps_our_call(item.get("start_time"), item.get("end_time"), self.windows)

    def derived(self, item: Any) -> bool:
        """A fact, todo or note Bee made during one of our calls, or soon after it."""
        made = _seconds(item.get("created_at")) if isinstance(item, dict) else None
        return made is not None and any(start - MARGIN_SECONDS <= made <= end + DERIVED_AFTER_SECONDS
                                        for start, end in self.windows)

    def conversations(self, items: Any) -> list[Any]:
        return [item for item in items or [] if not self.conversation(item)]

    def during(self, item: Any) -> bool:
        """Something made during one of our calls."""
        made = _seconds(item.get("created_at")) if isinstance(item, dict) else None
        return made is not None and any(start - MARGIN_SECONDS <= made <= end + MARGIN_SECONDS
                                        for start, end in self.windows)

    def others(self, items: Any) -> list[Any]:
        """Facts or todos, without those Bee made of our calls (items with no time are kept)."""
        return [item for item in items or [] if not self.derived(item)]

    def voice_notes(self, items: Any) -> list[Any]:
        """Voice notes (Bee journals), without one recorded during our call. A voice note is the user's own words,
        not something Bee makes of a conversation, so one recorded in the minutes after a call is kept."""
        return [item for item in items or [] if not self.during(item)]
