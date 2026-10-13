"""Places calls: a LiveKit room with the voice server dispatched into it.

The voice server registers as the agent named AGENT_NAME and joins only the rooms
it is dispatched to. The dispatch metadata tells it which way the call goes and,
for an AI-initiated call, why: the plan, the Bee inbox and the conversation ids,
and the call prep note written before the phone rang (voice/prep.py).

Ringing the phone is a separate step. Until VoIP push is set up (it needs the paid
Apple Developer account), an app that is open hears about a new call from the
token server, which streams workspace/state/outbound.jsonl. This module is the
only writer of that file.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from livekit import api

from beelink.planner import OUTBOUND_LOG, Plan

AGENT_NAME = "bee-call"
RING_SECONDS = 60  # how long the voice server waits for the phone to answer (arbitrary)


def call_metadata(direction: str, plan: Plan | None = None, prep: dict[str, Any] | None = None) -> str:
    data: dict[str, Any] = {"direction": direction}
    if plan is not None:
        data.update({"inbox": str(plan.inbox_dir), "plan": str(plan.path) if plan.path else None,
                     "conversation_ids": plan.conversation_ids, "reason": plan.reason, "topic": plan.topic,
                     "kind": plan.kind})
    if prep is not None:
        data["prep"] = prep
    return json.dumps(data, ensure_ascii=False)


def room_with_agent(room: str, metadata: str, ring_seconds: int = RING_SECONDS) -> api.CreateRoomRequest:
    # The room outlives an unanswered ring a little, then closes by itself.
    return api.CreateRoomRequest(name=room, empty_timeout=ring_seconds + 30, departure_timeout=20,
                                 agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=metadata)])


async def place_call(plan: Plan, workspace: Path, lk: api.LiveKitAPI | None = None,
                     prep: dict[str, Any] | None = None) -> str:
    """Opens the room for an AI-initiated call and records it for the ring. Returns the room name.
    `prep` is the call prep note (voice/prep.py Prep.to_meta()), made before this call."""
    room = f"call-{datetime.now():%Y%m%d-%H%M%S}"
    own = lk is None
    lk = lk or api.LiveKitAPI()  # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET from the environment
    try:
        await lk.room.create_room(room_with_agent(room, call_metadata("outbound", plan, prep)))
    finally:
        if own:
            await lk.aclose()
    log = workspace / OUTBOUND_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    placed = datetime.now()
    with log.open("a") as f:
        # expires_at: after this the voice server has given up, so an app must not try to answer.
        f.write(json.dumps({"at": placed.isoformat(timespec="seconds"), "room": room,
                            "expires_at": (placed + timedelta(seconds=RING_SECONDS)).isoformat(timespec="seconds"),
                            "topic": plan.topic, "kind": plan.kind, "reason": plan.reason,
                            "plan": str(plan.path) if plan.path else None}, ensure_ascii=False) + "\n")
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
