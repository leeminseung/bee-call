"""Hands out LiveKit room tokens to the iPhone app and test clients, and tells an open app about AI calls.

Run: uv run python -m voice.token_server

GET /token?identity=matt                 a token for a new call the user starts; the voice server is
                                         dispatched into the room when the app joins
GET /token?identity=matt&room=R&answer=1 a token to answer the AI's call in room R (already dispatched);
                                         410 if the room is gone (the voice server gave up after RING_SECONDS)
GET /ring                                Server-Sent Events: one "ring" event per call the AI places
                                         (calls placed in the last RING_SECONDS, then new ones). Until VoIP
                                         push is set up, this is how an open app learns the AI is calling.

Listens on all interfaces so an iPhone on the same Wi-Fi can reach it.
Development only: anyone on the network can get a token.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

from beelink.planner import OUTBOUND_LOG
from voice.dispatch import AGENT_NAME, RING_SECONDS, call_metadata

load_dotenv(".env.local")
WORKSPACE = Path("workspace")
KEEPALIVE_SECONDS = 15.0


def env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is missing from .env.local")
    return value


def make_token(identity: str, room: str, answer: bool, key: str, secret: str) -> str:
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_ttl(timedelta(hours=2))
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                     can_publish_data=True))
    )
    if not answer:
        # A call the user starts: dispatch the voice server when the app's join creates the room.
        token = token.with_room_config(api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=call_metadata("inbound"))]))
    return token.to_jwt()


async def livekit_room_exists(app: web.Application, room: str) -> bool:
    lk = api.LiveKitAPI(app["url"], app["key"], app["secret"])
    try:
        found = await lk.room.list_rooms(api.ListRoomsRequest(names=[room]))
    finally:
        await lk.aclose()
    return any(r.name == room for r in found.rooms)


async def token(request: web.Request) -> web.Response:
    identity = request.query.get("identity", "user")
    answer = request.query.get("answer") == "1"
    if answer and not request.query.get("room"):
        raise web.HTTPBadRequest(text="answer=1 needs the room of the AI's call\n")
    room = request.query.get("room") or f"call-{time.strftime('%Y%m%d-%H%M%S')}"
    if answer and not await request.app["room_exists"](request.app, room):
        # Joining would create an empty room with no voice server in it.
        raise web.HTTPGone(text="that call has ended\n")
    jwt = make_token(identity, room, answer, request.app["key"], request.app["secret"])
    return web.json_response({"url": request.app["url"], "token": jwt, "room": room, "identity": identity})


def recent_calls(log: Path, now: datetime) -> tuple[list[str], int]:
    """Lines for calls placed in the last RING_SECONDS, and the file offset to follow from."""
    if not log.exists():
        return [], 0
    data = log.read_bytes()
    end = data.rfind(b"\n") + 1
    lines = []
    for line in data[:end].decode(errors="replace").splitlines():
        try:
            placed = datetime.fromisoformat(json.loads(line)["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if (now - placed).total_seconds() < RING_SECONDS:
            lines.append(line)
    return lines, end


async def ring(request: web.Request) -> web.StreamResponse:
    log = request.app["workspace"] / OUTBOUND_LOG
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    lines, pos = recent_calls(log, datetime.now())
    last_write = time.monotonic()
    try:
        while True:
            for line in lines:
                await resp.write(f"event: ring\ndata: {line}\n\n".encode())
                last_write = time.monotonic()
            lines = []
            await asyncio.sleep(0.5)
            if log.exists() and log.stat().st_size > pos:
                with log.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1  # only whole lines
                pos += end
                lines = [l for l in chunk[:end].decode(errors="replace").splitlines() if l.strip()]
            if time.monotonic() - last_write > KEEPALIVE_SECONDS:
                await resp.write(b": keepalive\n\n")
                last_write = time.monotonic()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


def make_app(url: str, key: str, secret: str, workspace: Path = WORKSPACE, room_exists=livekit_room_exists) -> web.Application:
    app = web.Application()
    app["url"], app["key"], app["secret"], app["workspace"] = url, key, secret, workspace
    app["room_exists"] = room_exists
    app.router.add_get("/token", token)
    app.router.add_get("/ring", ring)
    return app


if __name__ == "__main__":
    import logging

    # One line per request, so a phone that cannot reach the server is easy to tell apart.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # The default route can be a VPN, so list the Wi-Fi addresses a phone can actually reach.
    from voice.session_server import lan_addresses

    port = int(os.environ.get("TOKEN_SERVER_PORT", "8787"))
    app = make_app(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    print(f"token server on port {port}. Token URL for the iPhone app (same Wi-Fi):", flush=True)
    for iface, ip in lan_addresses() or [("this Mac only", "127.0.0.1")]:
        print(f"  http://{ip}:{port}/token  ({iface})", flush=True)
    web.run_app(app, host="0.0.0.0", port=port, print=None)
