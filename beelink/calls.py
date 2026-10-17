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


# Each call record's window, by its path, with the record's size and change time when it was read: a call's record
# is read again only when it changed, so a Bee read during a call does not read every call the person ever had
# (review of the user-noticed fixes, 4).
_WINDOWS: dict[Path, tuple[tuple[int, int], tuple[float, float] | None]] = {}


def our_call_windows(workspace: Path) -> list[tuple[float, float]]:
    windows = []
    for folder in call_folders(workspace):
        path = folder / EVENTS
        try:
            stat = path.stat()
        except OSError:
            continue
        seen = (stat.st_size, stat.st_mtime_ns)
        cached = _WINDOWS.get(path)
        if cached is None or cached[0] != seen:
            cached = (seen, call_window(path))
            _WINDOWS[path] = cached
        if cached[1] is not None:
            windows.append(cached[1])
    return windows


def call_window(path: Path) -> tuple[float, float] | None:
    """One call's time window, from its events.jsonl; None for a record without a start."""
    events = read_events(path)  # the call in progress may have a line still being written
    if not events:
        return None
    first = events[0]
    if first.get("type") != "call_started" or "started_at" not in first:
        return None
    try:
        start = parse_time(str(first["started_at"])).timestamp()
    except ValueError:
        return None
    # The call ends at call_ended; lines after it (Bee write results) come later and are not the call.
    ended = [e.get("t", 0.0) for e in events if e.get("type") == "call_ended"]
    end_t = ended[0] if ended else events[-1].get("t", 0.0)
    return start, start + float(end_t or 0.0)


def overlaps_our_call(start_ms: int | None, end_ms: int | None, windows: list[tuple[float, float]]) -> bool:
    if start_ms is None:
        return False
    start = start_ms / 1000.0
    end = (end_ms / 1000.0) if end_ms else start
    return any(start <= w_end + MARGIN_SECONDS and end >= w_start - MARGIN_SECONDS for w_start, w_end in windows)


def _span(item: Any) -> tuple[float, float] | None:
    """A Bee conversation's start and end in epoch seconds (its start again when it is still recording), or None."""
    if not isinstance(item, dict):
        return None
    start, end = item.get("start_time"), item.get("end_time")
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        return None
    end = end if isinstance(end, (int, float)) and not isinstance(end, bool) and end else start
    return start / 1000.0, end / 1000.0


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
    """What Bee recorded during our own calls, which is never read as new context (BC-060), and what our calls
    wrote to Bee."""
    windows: list[tuple[float, float]]
    workspace: Path | None = None

    @classmethod
    def of(cls, workspace: Path) -> "OurCalls":
        return cls(our_call_windows(workspace), workspace)

    def write_folders(self) -> list[Path]:
        """Where our Bee writes are kept, oldest first: each call's folder, then the meta agent's (an organize run the
        user said yes to in a call, meta/agent.py)."""
        return [*call_folders(self.workspace), self.workspace / "meta"] if self.workspace is not None else []

    def written_facts(self) -> set[int]:
        """The facts our calls wrote on the user's yes (made, confirmed or corrected, and not taken back), and those an
        organize run added: the user's own, not something Bee made of a call."""
        if self.workspace is None:
            return set()
        from beelink.writer import BeeWrites, the_fact

        written: set[int] = set()
        for folder in self.write_folders():
            if not (folder / "bee-writes.jsonl").exists():
                continue
            for write in BeeWrites(folder).all():
                if write.status != "ok":
                    continue
                fact_id = the_fact(write.response).get("id") if write.kind == "fact_create" else write.item.get("fact_id")
                if isinstance(fact_id, int) and write.kind in ("fact_create", "fact_confirm", "fact_update"):
                    written.add(fact_id)
        return written

    def kept_facts(self, items: Any) -> list[Any]:
        """Confirmed facts without those Bee made of our calls (as others()), but with the ones our calls wrote."""
        written = self.written_facts()
        return [item for item in items or [] if (isinstance(item, dict) and item.get("id") in written)
                or not self.derived(item)]

    def at(self, seconds: float) -> bool:
        """A moment (epoch seconds) inside one of our calls, or a minute before or after it: a line Bee heard then
        is the user talking to us (a conversation Bee keeps recording runs through our calls)."""
        return any(start - MARGIN_SECONDS <= seconds <= end + MARGIN_SECONDS for start, end in self.windows)

    def conversation(self, item: Any) -> bool:
        """A Bee conversation that overlaps one of our calls."""
        return isinstance(item, dict) and overlaps_our_call(item.get("start_time"), item.get("end_time"), self.windows)

    def within(self, item: Any) -> bool:
        """A Bee conversation that lies within one of our calls (a minute's margin each side): Bee's recording of the
        call itself. A longer recording that runs through a call is not (crosses): the Bee read tools keep it and
        leave out only its lines said during the call (beelink/toolkit.py)."""
        span = _span(item)
        return span is not None and any(w_start - MARGIN_SECONDS <= span[0] and span[1] <= w_end + MARGIN_SECONDS
                                        for w_start, w_end in self.windows)

    def crosses(self, item: Any) -> bool:
        """A Bee conversation that overlaps one of our calls but is more than Bee's recording of it."""
        return self.conversation(item) and not self.within(item)

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
