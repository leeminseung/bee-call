"""Watches Bee in real time and notices when something new is worth a look.

`bee stream` delivers each event at most once, cannot replay, and has no fact
events, so after every burst of events and on every (re)connect the watcher
also asks `bee changed --cursor <last>` and keeps the cursor in
workspace/state/bee_cursor.json. Records made during our own AI calls are
skipped. Each new batch is saved raw under workspace/inbox/<time>/.

It never calls anyone by itself: it hands a Trigger to a callback, and the
planner decides whether the AI should call. The cursor is saved only after the
callback returns, so a batch whose handling failed is looked at again on the
next catch-up. A new Bee event restarts the quiet period before a catch-up (a
conversation's events at most until LONGEST_WAIT_S has passed since the first
of them), but never cancels a catch-up that has already started.

Run on its own to watch and print triggers:
    uv run python -m beelink.watcher
"""

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from beelink import cli
from beelink.calls import OurCalls

log = logging.getLogger("beelink.watcher")

STREAM_TYPES = ("new-conversation,update-conversation,update-conversation-summary,journal-created,journal-text,"
                "journal-updated")
# Events that mean a conversation or voice note has settled enough to look at.
SETTLED_EVENTS = {"update-conversation-summary", "update-conversation", "journal-created"}
# A voice memo still coming in: its text in pieces, then Bee's reply to it. Each starts the quiet period over, so a
# memo is looked at once it is finished (2026-10-17).
MEMO_PIECES = {"journal-text", "journal-updated"}
# A conversation Bee keeps updating starts the quiet period over and over: its changes are looked at once this long
# has passed since the first event of the burst (review of 2026-10-17, finding 6). A memo's pieces are not held to
# it, since a memo ends. Arbitrary.
LONGEST_WAIT_S = 180.0


@dataclass
class Trigger:
    reason: str
    inbox_dir: Path
    conversation_ids: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def parse_stream_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """`bee stream --json` prints one JSON object per event: {"event": name, "data": {...}}."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    name = obj.get("event") or obj.get("type")
    if not name:
        return None
    data = obj.get("data")
    return name, data if isinstance(data, dict) else {}


class BeeWatcher:
    def __init__(self, workspace: Path, on_trigger: Callable[[Trigger], Awaitable[None]],
                 debounce: float = 20.0, longest_wait: float = LONGEST_WAIT_S) -> None:
        self.workspace = workspace
        self.on_trigger = on_trigger
        self.debounce = debounce
        self.longest_wait = longest_wait
        self._burst_started = 0.0  # when the first event of the quiet period's burst came (the loop's clock)
        self._cursor_file = workspace / "state" / "bee_cursor.json"
        self._timer: asyncio.Task | None = None  # the quiet period before a catch-up; the only thing ever cancelled
        self._reason = "event"
        self._lock = asyncio.Lock()  # one catch-up at a time

    # ---------- cursor ----------

    def _load_cursor(self) -> str | None:
        if self._cursor_file.exists():
            return json.loads(self._cursor_file.read_text()).get("cursor")
        return None

    def _save_cursor(self, cursor: str | None) -> None:
        if not cursor:
            return
        self._cursor_file.parent.mkdir(parents=True, exist_ok=True)
        # A new file put in place, as meta/session.py saves its cursor: a save cut off halfway leaves the last one.
        tmp = self._cursor_file.with_name(f".{self._cursor_file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"cursor": cursor, "saved_at": datetime.now().isoformat()}))
        tmp.replace(self._cursor_file)

    # ---------- catching up ----------

    async def catch_up(self, reason: str) -> Trigger | None:
        payload = await cli.changed(self._load_cursor())
        next_cursor = payload.get("meta", {}).get("next_cursor")

        # What Bee made of our own calls is left out, also from the batch saved for the meta agent (BC-060).
        ours = OurCalls.of(self.workspace)
        conversations = ours.conversations(payload.get("conversations"))
        kept = {
            "conversations": conversations,
            "facts": ours.others(payload.get("facts")),
            "todos": ours.others(payload.get("todos")),
            "journals": ours.voice_notes(payload.get("journals")),
            "dailies": payload.get("dailies") or [],
        }
        counts = {k: len(v) for k, v in kept.items()}
        if not any(counts.values()):
            self._save_cursor(next_cursor)
            return None

        inbox = self.workspace / "inbox" / datetime.now().strftime("%Y-%m-%d-%H%M%S")
        inbox.mkdir(parents=True, exist_ok=True)
        left_out = {k: len(payload.get(k) or []) - len(v) for k, v in kept.items()}
        (inbox / "changed.json").write_text(json.dumps({**payload, **kept, "left_out_as_our_calls": left_out},
                                                       ensure_ascii=False, indent=1))
        trigger = Trigger(reason=reason, inbox_dir=inbox,
                          conversation_ids=[c["id"] for c in conversations if "id" in c], counts=counts)
        await self.on_trigger(trigger)  # if this raises, the cursor stays and the batch comes back next time
        self._save_cursor(next_cursor)
        return trigger

    def on_event(self, name: str) -> None:
        """One event from `bee stream`: a settled change starts the quiet period before a catch-up, and a later one
        starts it over, as a piece of a voice memo does (keeping the reason it was started for); a catch-up already
        running (placing a call) is never cancelled."""
        if name in SETTLED_EVENTS:
            self._schedule(name, held=True)
        elif name in MEMO_PIECES:
            pending = self._timer is not None and not self._timer.done()
            self._schedule(self._reason if pending else name, held=False)

    def _schedule(self, reason: str, held: bool) -> None:
        """Starts the quiet period, or starts it over; `held`: never past the longest wait of the burst."""
        self._reason = reason
        now = asyncio.get_running_loop().time()
        if self._timer and not self._timer.done():
            self._timer.cancel()  # still in the quiet period: start it over
        else:
            self._burst_started = now
        wait = self.debounce
        if held:
            wait = min(wait, max(0.0, self._burst_started + self.longest_wait - now))
        self._timer = asyncio.create_task(self._after_quiet(wait))

    async def _after_quiet(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self._timer = None  # past the quiet period: a new event schedules another catch-up instead of cancelling this one
        async with self._lock:
            try:
                await self.catch_up(self._reason)
            except Exception:
                log.exception("catch-up failed; the same Bee changes will be looked at again")

    # ---------- streaming ----------

    async def run(self) -> None:
        backoff = 2.0
        while True:
            # Own process group: the bee CLI is a node wrapper around a native binary,
            # and both must go when the watcher stops.
            try:
                proc = await asyncio.create_subprocess_exec(
                    cli.bee_bin(), "stream", "--json", "--types", STREAM_TYPES,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=cli.run_env(),
                    start_new_session=True,
                )
            except (OSError, cli.BeeError) as exc:  # a stream that cannot start is retried like a dropped one
                log.warning("bee stream could not start: %s; retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            try:
                async with self._lock:
                    await self.catch_up("connected")
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    parsed = parse_stream_line(raw.decode(errors="replace"))
                    if parsed is None:
                        continue
                    name, _ = parsed
                    backoff = 2.0
                    self.on_event(name)
            except Exception as exc:  # keep watching through transient failures
                log.warning("bee stream error: %s", exc)
            finally:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            log.info("bee stream ended; reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _print_trigger(trigger: Trigger) -> None:
    print(f"trigger: {trigger.reason} counts={trigger.counts} saved={trigger.inbox_dir}")


async def _main() -> None:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        from voice.people import People, dev_person
        people, person = People(), dev_person()
        if not person or not people.settings(person).bee:
            raise SystemExit("set BEE_CALL_DEV_PERSON to a person who connected Bee: the watcher reads their Bee "
                             "and keeps its cursor and batches in their folder")
        cli.use_bee(people.bee_dir(person))
        await BeeWatcher(people.workspace(person), _print_trigger).run()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
