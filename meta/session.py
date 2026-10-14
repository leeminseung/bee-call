"""The meta agent's own record: what woke it, what it looked at, what it decided and why, in plain files under
workspace/meta/ (design: the meta agent proposes the user's next step and acts only after a yes).

  events.jsonl   the inbox. Any process appends one line per event (a Bee change, a call that ended, the user's
                 answer to an offer, a booked call coming up); the meta agent takes what is new since its cursor.
  session.jsonl  the meta session: one entry per time the agent thought, with the events, every tool it used
                 and the agent's own words, shown as they are in the app's timeline.
  state.md       the agent's working notes, rewritten by the agent itself: open threads, the next call's agenda,
                 what the user turned down, what it learned the user prefers.

Appending one short line is atomic on POSIX, so writers need no lock; the cursor is the inbox's byte offset.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

META_DIR = Path("meta")  # under the workspace
DAILY_NOTIFICATIONS = 4  # at most this many notifications a day (agreed with Matt, 2026-10-14)
QUIET_FROM, QUIET_UNTIL = time(23, 0), time(8, 0)  # no notifications in between (agreed with Matt, 2026-10-14)
STATE_TEMPLATE = """# State

## Open threads

## Agenda for the next call

## Turned down

## What the user prefers
"""


def add_event(workspace: Path, kind: str, now: datetime | None = None, **data: Any) -> None:
    """For any process: tell the meta agent something happened."""
    folder = workspace / META_DIR
    folder.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": (now or datetime.now()).isoformat(timespec="seconds"), "kind": kind, **data},
                      ensure_ascii=False, default=str)
    with open(folder / "events.jsonl", "a") as f:
        f.write(line + "\n")


def meta_notes(workspace: Path) -> str | None:
    """The meta agent's notes for a call's prep, or None before it has written any."""
    try:
        text = (workspace / META_DIR / "state.md").read_text().strip()
    except OSError:
        return None
    return text if text and text != STATE_TEMPLATE.strip() else None


def quiet(moment: datetime) -> bool:
    clock = moment.time()
    return clock >= QUIET_FROM or clock < QUIET_UNTIL


def quiet_ends(moment: datetime) -> datetime:
    """When notifications may go out again after `moment`."""
    if not quiet(moment):
        return moment
    day = moment.date() if moment.time() < QUIET_UNTIL else moment.date() + timedelta(days=1)
    return datetime.combine(day, QUIET_UNTIL)


@dataclass
class MetaSession:
    workspace: Path

    @property
    def dir(self) -> Path:
        return self.workspace / META_DIR

    def take_events(self) -> list[dict[str, Any]]:
        """The events added since the last call, oldest first; the cursor moves past them."""
        inbox, cursor = self.dir / "events.jsonl", self.dir / "events.cursor"
        if not inbox.exists():
            return []
        try:
            start = int(cursor.read_text())
        except (OSError, ValueError):
            start = 0
        with open(inbox, "rb") as f:
            f.seek(start)
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1]  # a line still being written waits for the next look
        events = []
        for line in complete.decode(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        if complete:
            cursor.write_text(str(start + len(complete)))
        return events

    def log(self, entry: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "session.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = (self.dir / "session.jsonl").read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def recent_text(self, limit_chars: int = 6000) -> str:
        """The latest entries, newest last, as the agent reads them back: when, why, and what it decided."""
        parts: list[str] = []
        used = 0
        for entry in reversed(self.entries()):
            events = "; ".join(f"{e.get('kind')}" + (f" ({e.get('summary')})" if e.get("summary") else "")
                               for e in entry.get("events", []))
            actions = "; ".join(f"{a.get('tool')}: {a.get('result')}" for a in entry.get("actions", []))
            text = f"[{entry.get('at')}] woke for: {events or '-'}\nDecided: {entry.get('decision', '').strip()}" + \
                (f"\nDid: {actions}" if actions else "")
            if used + len(text) > limit_chars:
                break
            parts.append(text)
            used += len(text)
        return "\n\n".join(reversed(parts)) or "Nothing yet: this is the first time you look."

    def state(self) -> str:
        try:
            return (self.dir / "state.md").read_text()
        except OSError:
            return STATE_TEMPLATE

    def write_state(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".state.{os.getpid()}.tmp"
        tmp.write_text(text.strip() + "\n")
        tmp.replace(self.dir / "state.md")
