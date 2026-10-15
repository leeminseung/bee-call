"""A call in text: the voice server's own call (voice/agent.py run_call) with the person's lines typed, and no LiveKit
media, speech recognition or voice. For tests from outside, and a quick look at a call on a Mac.

  uv run python -m voice.text_call <person> "Hello" "Tell me about my day."        a call the person starts
  uv run python -m voice.text_call <person> --continue <call id> "Where were we?"  one that continues an earlier call
  uv run python -m voice.text_call <person> --answer <room> "Hello"                answers the AI's call ringing there
  --wait <seconds>  waits that long after the last line before hanging up (for background jobs to report)

What runs is what a call runs: the person's settings and the day's budget, the call AI of COORDINATOR_LLM (echo and
fake need no network), Bee through BEE_BIN when the person uses Bee, WORKER's background jobs, the call's record in
the person's folder, Bee writes, the meta agent's call_ended, and the title after the call. The person's lines go in
as their turns, one after another, each after the AI's answer to the one before; then they hang up. Each of the AI's
replies is printed as it is added to the conversation; at the end, the call's id and its record folder, as JSON on
the last line. What does not run: LiveKit (no room, no app, no captions), ringing, speech recognition and the voice.

--answer takes the room an agreed AI call was placed in (state/offers: the offer's room; state/outbound.jsonl: the
ring line's), and answers it as the app would, with what the voice server is told when LiveKit dispatches it.
"""

import argparse
import asyncio
import contextvars
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from livekit import rtc
from livekit.agents import AgentSession

from voice import agent
from voice.call_folders import find_call
from voice.dispatch import call_metadata, room_name
from voice.offers import OfferStore, agreed, plan_of
from voice.people import People


class TextRoom:
    """What a call uses of its LiveKit room: its name, the voice server's attributes and the room's events."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handlers: dict[str, list[Callable]] = {}
        self.local_participant = SimpleNamespace(attributes={}, set_attributes=self._set_attributes)

    async def _set_attributes(self, attributes: dict[str, str]) -> None:
        self.local_participant.attributes.update(attributes)

    def on(self, event: str, callback: Callable | None = None):
        if callback is None:
            def register(handler):
                self.handlers.setdefault(event, []).append(handler)
                return handler
            return register
        self.handlers.setdefault(event, []).append(callback)
        return callback

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(*args)


class TextJob:
    """What a call uses of LiveKit's job context, for one call in text: the caller is in the room from the start."""

    def __init__(self, room: str, meta: dict[str, Any]) -> None:
        self.room = TextRoom(room)
        self.job = SimpleNamespace(metadata=json.dumps(meta), room=SimpleNamespace(name=room))
        self.proc = SimpleNamespace(userdata={})
        self.caller = SimpleNamespace(identity=meta.get("person") or "caller", kind=0, disconnect_reason=None)
        self.shutdown_callbacks: list[Callable] = []
        self.shutdown_reason: str | None = None
        self.session: AgentSession | None = None

    async def connect(self) -> None:
        pass

    async def wait_for_participant(self):
        return self.caller

    def init_recording(self, options) -> None:
        pass

    def is_fake_job(self) -> bool:
        return False

    def add_shutdown_callback(self, callback: Callable) -> None:
        self.shutdown_callbacks.append(callback)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reason = reason

    def delete_room(self, room_name: str | None = None):
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    async def end(self) -> None:
        """The job ends as LiveKit ends it: each shutdown callback runs as a task of its own."""
        for callback in self.shutdown_callbacks:
            await asyncio.get_running_loop().create_task(callback(), context=contextvars.Context())


class TextSession(AgentSession):
    """LiveKit's AgentSession without a room: text in, text out. Its voice "starts" as each reply is added, so the
    call goes on as it does when the AI's voice is heard."""

    async def start(self, *args: Any, room: Any = None, **kwargs: Any) -> Any:
        return await super().start(*args, **kwargs)


async def no_ringing(room: Any) -> None:
    await asyncio.Event().wait()  # until the AI picks up


def meta_for(people: People, person: str, answer: str | None, continue_from: str | None) -> tuple[str, dict]:
    """The call's room and the dispatch metadata the voice server would be given."""
    if answer is None:
        meta: dict[str, Any] = {"direction": "inbound", "person": person}
        if continue_from:
            meta["continue_from"] = continue_from
        return room_name(person, people.person(person).now()), meta
    offer = next((o for o in OfferStore(people.workspace(person)).all() if o.room == answer), None)
    if offer is None:
        raise SystemExit(f"no AI call of {person}'s was placed in room {answer}")
    return answer, json.loads(call_metadata("outbound", plan_of(offer), None, offer.continue_from, agreed(offer),
                                            person))


async def text_call(people: People, person: str, lines: list[str], *, answer: str | None = None,
                    continue_from: str | None = None, wait_s: float = 0.0,
                    say: Callable[[str], None] = print) -> dict[str, Any]:
    """Runs one call in text; returns {"call_id", "record"} (record None for a call that was not answered)."""
    room, meta = meta_for(people, person, answer, continue_from)
    job = TextJob(room, meta)
    closed = asyncio.Event()

    def make_session(ctx, language, voice, rec, call_model) -> AgentSession:
        session = TextSession(llm=agent.make_llm(rec, language, call_model))
        session.on("close", lambda ev: closed.set())

        def added(ev) -> None:
            item = ev.item
            if getattr(item, "role", None) == "assistant" and (item.text_content or "").strip():
                say(f"AI: {item.text_content.strip()}")
                session.emit("agent_state_changed", SimpleNamespace(old_state="thinking", new_state="speaking"))

        session.on("conversation_item_added", added)
        job.session = session
        return session

    agent.make_session, agent.ring_back = make_session, no_ringing
    await agent.run_call(job, people)
    session = job.session
    if session is not None and not closed.is_set():
        for line in lines:
            if closed.is_set():  # the AI ended the call
                break
            say(f"{person}: {line}")
            await session.run(user_input=line)
        if wait_s:
            await asyncio.sleep(wait_s)
        if not closed.is_set():
            job.caller.disconnect_reason = rtc.DisconnectReason.CLIENT_INITIATED
            await session.aclose()  # the person hangs up
    await job.end()
    folder = find_call(people.workspace(person), room)
    return {"call_id": room, "record": str(folder) if folder else None}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m voice.text_call", description=__doc__.splitlines()[0])
    parser.add_argument("person", help="the person whose call it is (an approved id)")
    parser.add_argument("lines", nargs="*", help="what the person says, one turn each")
    parser.add_argument("--answer", metavar="ROOM", help="answer the AI's call ringing in this room")
    parser.add_argument("--continue", dest="continue_from", metavar="CALL_ID", help="continue this earlier call")
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to wait after the last line")
    args = parser.parse_args(argv)
    people = People()
    if not people.known(args.person):
        raise SystemExit(f"{args.person} is not an approved person")
    found = asyncio.run(text_call(people, args.person, args.lines, answer=args.answer,
                                  continue_from=args.continue_from, wait_s=args.wait))
    print(json.dumps(found), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
