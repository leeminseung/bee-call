"""Places calls: a LiveKit room with the voice server dispatched into it.

The voice server registers as the agent named AGENT_NAME and joins only the rooms
it is dispatched to. The dispatch metadata tells it which way the call goes and,
for an AI-initiated call, why: the plan, the Bee inbox and the conversation ids,
and the call prep note written before the phone rang (voice/prep.py).
AGENT_NAME is LIVEKIT_AGENT_NAME, bee-call when unset. Every machine that runs a
voice server needs its own name (bee-call on the server, bee-call-dev on a Mac):
LiveKit shares the calls for one name among all the workers registered under it.

Ringing the phone is a separate step. The ring line goes into
workspace/state/outbound.jsonl, which the token server streams to an open app, and
to Apple's VoIP push for a closed or locked one (voice/voip_push.py, once its key is
set up and the app has registered). This module is the only writer of that file.
"""

import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from livekit import api

from beelink.planner import OUTBOUND_LOG, Plan
from voice import voip_push
from voice.people import Person

log = logging.getLogger("bee-call.dispatch")

# The entry points import this module before they load .env.local, where a Mac sets LIVEKIT_AGENT_NAME.
load_dotenv(".env.local")


def agent_name(env: Mapping[str, str] = os.environ) -> str:
    return env.get("LIVEKIT_AGENT_NAME", "").strip() or "bee-call"


AGENT_NAME = agent_name()
RING_SECONDS = 60  # how long the voice server waits for the phone to answer (arbitrary)


def room_name(person: str, now: datetime | None = None) -> str:
    """A call's room carries its person, so the token server can tell whose call a room is (answer=1)."""
    return f"call-{person}-{(now or datetime.now()):%Y%m%d-%H%M%S}"


def call_metadata(direction: str, plan: Plan | None = None, prep: dict[str, Any] | None = None,
                  continue_from: str | None = None, agreed: str | None = None, person: str | None = None) -> str:
    data: dict[str, Any] = {"direction": direction}
    if person:  # whose call: the voice server works in this person's folder (voice/people.py)
        data["person"] = person
    if continue_from:  # the earlier call this one continues (voice/continuation.py)
        data["continue_from"] = continue_from
    if agreed:  # how the user agreed to an AI call (voice/offers.py)
        data["agreed"] = agreed
    if plan is not None:
        data.update({"inbox": str(plan.inbox_dir) if plan.inbox_dir else None,
                     "plan": str(plan.path) if plan.path else None,
                     "conversation_ids": plan.conversation_ids, "reason": plan.reason, "topic": plan.topic,
                     "kind": plan.kind})
    if prep is not None:
        data["prep"] = prep
    return json.dumps(data, ensure_ascii=False)


def room_with_agent(room: str, metadata: str, ring_seconds: int = RING_SECONDS) -> api.CreateRoomRequest:
    # The room outlives an unanswered ring a little, then closes by itself.
    return api.CreateRoomRequest(name=room, empty_timeout=ring_seconds + 30, departure_timeout=20,
                                 agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=metadata)])


async def place_call(plan: Plan, person: Person, lk: api.LiveKitAPI | None = None,
                     prep: dict[str, Any] | None = None, *, continue_from: str | None = None,
                     agreed: str | None = None) -> str:
    """Opens the room for an AI-initiated call to `person` and records it for the ring. Returns the room name.
    `prep` is the call prep note (voice/prep.py Prep.to_meta()), made before this call; `agreed` says how the
    user agreed to it (voice/offers.py), and `continue_from` the earlier call it continues."""
    room = room_name(person.id)
    own = lk is None
    lk = lk or api.LiveKitAPI()  # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET from the environment
    try:
        await lk.room.create_room(room_with_agent(room, call_metadata("outbound", plan, prep, continue_from,
                                                                      agreed, person.id)))
    finally:
        if own:
            await lk.aclose()
    path = person.workspace / OUTBOUND_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    placed = datetime.now()
    # expires_at: after this the voice server has given up, so an app must not try to answer. It is this Mac's
    # local time; expires_epoch is the same moment for a phone in another time zone.
    expires = placed + timedelta(seconds=RING_SECONDS)
    line = {"at": placed.isoformat(timespec="seconds"), "room": room,
            "expires_at": expires.isoformat(timespec="seconds"), "expires_epoch": int(expires.timestamp()),
            "topic": plan.topic, "kind": plan.kind, "reason": plan.reason,
            "plan": str(plan.path) if plan.path else None}
    with path.open("a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    try:
        pushed = await voip_push.ring(line, person)
        if pushed:
            log.info("VoIP push sent for room %s (apns-id %s)", room, pushed)
    except Exception as exc:  # an open app still hears the ring stream
        log.warning("VoIP push for room %s failed: %s", room, exc)
    return room


async def open_inbound_room(room: str, lk: api.LiveKitAPI | None = None) -> None:
    """For test callers that join with their own token: the voice server is dispatched as it is for the app."""
    own = lk is None
    lk = lk or api.LiveKitAPI()
    try:
        await lk.room.create_room(room_with_agent(room, call_metadata("inbound")))
    finally:
        if own:
            await lk.aclose()
