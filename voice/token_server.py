"""Hands out LiveKit room tokens to the iPhone app and test clients, and tells an open app about AI calls.

Run: uv run python -m voice.token_server

GET /token?identity=matt                 a token for a new call the user starts; the voice server is
                                         dispatched into the room when the app joins
GET /token?identity=matt&room=R&answer=1 a token to answer the AI's call in room R (already dispatched);
                                         410 if the room is gone (the voice server gave up after RING_SECONDS)
GET /ring                                Server-Sent Events: one "ring" event per call the AI places
                                         (calls placed in the last RING_SECONDS, then new ones): how an open
                                         app learns the AI is calling without VoIP push.
POST /voip  {"token": hex, "environment": "development"|"production"}
                                         the app's VoIP push token, so an AI call rings a closed app too
                                         (voice/voip_push.py); 204, or 400 for something else
GET /token?identity=matt&continue=D/T    a new call that continues the earlier call D/T (voice/continuation.py)
POST /push  {"token": hex, "environment": ...}
                                         the app's token for plain notifications: the offers that ask
                                         before an AI call (voice/offers.py)
GET /offers                              offers still waiting for an answer or a call
POST /offers/<id>  {"choice": "now"|"later"|"decline"}
                                         the user's answer; voice/outbound.py places the call. 404 unknown,
                                         409 already answered or closed

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
from voice import voip_push
from voice.continuation import session_dir
from meta.session import add_event
from voice.offers import OfferStore
from voice.dispatch import AGENT_NAME, RING_SECONDS, call_metadata

load_dotenv(".env.local")
WORKSPACE = Path("workspace")
KEEPALIVE_SECONDS = 15.0


def env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is missing from .env.local")
    return value


def make_token(identity: str, room: str, answer: bool, key: str, secret: str,
               continue_from: str | None = None) -> str:
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
            agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME,
                                          metadata=call_metadata("inbound", continue_from=continue_from))]))
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
    continue_from = request.query.get("continue")  # "Continue this call" in an earlier call's record
    if continue_from is not None and (answer or session_dir(request.app["workspace"], continue_from) is None):
        raise web.HTTPBadRequest(text="continue= needs the id of an earlier call, like 2026-10-14/0055\n")
    if answer and not await request.app["room_exists"](request.app, room):
        # Joining would create an empty room with no voice server in it.
        raise web.HTTPGone(text="that call has ended\n")
    jwt = make_token(identity, room, answer, request.app["key"], request.app["secret"], continue_from)
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


async def voip_token(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        voip_push.save_device_token(request.app["workspace"], str(body["token"]), str(body["environment"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a VoIP token: {exc}")
    print(f"VoIP push token saved ({body['environment']})", flush=True)
    return web.Response(status=204)


async def alert_token(request: web.Request) -> web.Response:
    """The app's token for plain notifications (the offers that ask before a call)."""
    try:
        body = await request.json()
        voip_push.save_device_token(request.app["workspace"], str(body["token"]), str(body["environment"]), "alert")
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a notification token: {exc}")
    print(f"notification token saved ({body['environment']})", flush=True)
    return web.Response(status=204)


async def list_offers(request: web.Request) -> web.Response:
    """The offers still waiting for an answer or a call, for the app's first screen."""
    store = OfferStore(request.app["workspace"])
    return web.json_response({"offers": [o.view() for o in store.open(datetime.now())]})


async def answer_offer(request: web.Request) -> web.Response:
    """A notification button or the app: {"choice": "now" | "later" | "decline"}. voice/outbound.py places the call."""
    try:
        body = await request.json()
        choice = str(body["choice"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='expected {"choice": "now" | "later" | "decline"}\n')
    store = OfferStore(request.app["workspace"])
    try:
        offer = store.answer(request.match_info["offer_id"], choice, datetime.now())
    except KeyError:
        raise web.HTTPNotFound(text="no such offer\n")
    except ValueError as exc:
        raise web.HTTPConflict(text=f"{exc}\n")
    print(f"offer {offer.id}: {choice} -> {offer.state}", flush=True)
    add_event(request.app["workspace"], "offer_answered", offer=offer.id, choice=choice, topic=offer.topic)
    return web.json_response({"offer": offer.view()})


def make_app(url: str, key: str, secret: str, workspace: Path = WORKSPACE, room_exists=livekit_room_exists) -> web.Application:
    app = web.Application()
    app["url"], app["key"], app["secret"], app["workspace"] = url, key, secret, workspace
    app["room_exists"] = room_exists
    app.router.add_get("/token", token)
    app.router.add_get("/ring", ring)
    app.router.add_post("/voip", voip_token)
    app.router.add_post("/push", alert_token)
    app.router.add_get("/offers", list_offers)
    app.router.add_post("/offers/{offer_id}", answer_offer)
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
