"""Hands out LiveKit room tokens to the iPhone app, and everything else the app asks this server for.

Every request except signing in carries the app's sign-in token (Authorization: Bearer <token>) and works only
on that person's own folder (voice/people.py). On a Mac, BEE_CALL_DEV_PERSON lets requests made on the machine
itself in without a token (curl, test callers); never set it on a server.

Run: uv run python -m voice.token_server

Signing in (Sign in with Apple, voice/apple_signin.py):
GET  /auth/nonce                         {"nonce"}: a one-time value the app hashes into Apple's sign-in request
POST /auth/apple {"identity_token", "nonce"}
                                         200 {"token", "person"}; 403 {"waiting": CODE} until Matt approves that
                                         sign-in (uv run python -m voice.people approve CODE --as ID)
POST /auth/signout                       ends this sign-in
GET  /me                                 who is signed in, their settings and today's estimated spend
PUT  /settings {"language": "ko", ...}   the settings the app may change (voice/people.py APP_SETTINGS)

Calls:
GET  /token                              a token for a new call the user starts; the voice server is dispatched
                                         into the room when the app joins. 429 once today's budget is spent
GET  /token?room=R&answer=1              a token to answer the AI's call in room R (already dispatched); 410 if
                                         the room is gone (the voice server gave up after RING_SECONDS)
GET  /token?continue=D/T                 a new call that continues the earlier call D/T (voice/continuation.py)
GET  /ring                               Server-Sent Events: one "ring" event per call the AI places for this
                                         person (calls placed in the last RING_SECONDS, then new ones): how an
                                         open app learns the AI is calling without VoIP push
POST /voip  {"token": hex, "environment": "development"|"production"}
                                         the app's VoIP push token, so an AI call rings a closed app too
POST /push  {"token": hex, "environment": ...}
                                         the app's token for plain notifications (the offers that ask first)
GET  /offers                             offers still waiting for an answer or a call
POST /offers/<id>  {"choice": "now"|"later"|"decline"}
                                         the user's answer; voice/outbound.py places the call. 404 unknown,
                                         409 already answered or closed

Bee, for the people who use it:
POST /bee/connect                        {"link"}: the approval link the app opens. The server waits for the
                                         approval in the background, then turns the setting "bee" on
POST /bee/disconnect                     logs this person's Bee out and turns "bee" off
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

from beelink import cli as bee
from beelink.planner import OUTBOUND_LOG
from meta.session import add_event
from voice import apple_signin, voip_push
from voice.continuation import session_dir
from voice.costs import spent_today
from voice.dispatch import AGENT_NAME, RING_SECONDS, call_metadata, room_name
from voice.offers import OfferStore
from voice.people import People, dev_person, waiting_code

load_dotenv(".env.local")
log = logging.getLogger("bee-call.token")
KEEPALIVE_SECONDS = 15.0
BEE_APPROVAL_SECONDS = 330  # Bee's approval link lasts about 5 minutes (bee login --no-wait)
OPEN_PATHS = ("/auth/nonce", "/auth/apple")  # the only paths that need no sign-in
BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID") or voip_push.DEFAULT_BUNDLE_ID

PERSON = web.RequestKey("person", str)  # who is asking, set by signed_in()
URL, KEY, SECRET = web.AppKey("url", str), web.AppKey("key", str), web.AppKey("secret", str)
ROOM_EXISTS = web.AppKey("room_exists", object)
PEOPLE = web.AppKey("people", People)
NONCES = web.AppKey("nonces", apple_signin.Nonces)
VERIFY = web.AppKey("verify", object)  # apple_signin.verify, or a stand-in in tests
BEE_LOGINS = web.AppKey("bee_logins", dict)  # person -> the task waiting for their Bee approval


def env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is missing from .env.local")
    return value


# ---------- who is asking ----------

def bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else None


def from_this_machine(request: web.Request) -> bool:
    """Made on this machine itself, not passed on by a proxy in front (which adds X-Forwarded-For)."""
    return request.remote in ("127.0.0.1", "::1") and "X-Forwarded-For" not in request.headers


@web.middleware
async def signed_in(request: web.Request, handler):
    if request.path in OPEN_PATHS:
        return await handler(request)
    person = request.app[PEOPLE].who(bearer(request))
    if person is None and not bearer(request) and from_this_machine(request):
        person = dev_person()
    if person is None:
        raise web.HTTPUnauthorized(text="sign in first\n")
    request[PERSON] = person
    return await handler(request)


def person(request: web.Request) -> str:
    return request[PERSON]


def workspace(request: web.Request) -> Path:
    return request.app[PEOPLE].workspace(person(request))


# ---------- signing in ----------

async def new_nonce(request: web.Request) -> web.Response:
    return web.json_response({"nonce": request.app[NONCES].new()})


async def apple_sign_in(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        identity_token, nonce = str(body["identity_token"]), str(body["nonce"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='expected {"identity_token": ..., "nonce": ...}\n')
    if not request.app[NONCES].use(nonce):
        raise web.HTTPBadRequest(text="unknown or used nonce; ask /auth/nonce again\n")
    try:
        identity = await asyncio.to_thread(request.app[VERIFY], identity_token, BUNDLE_ID, nonce)
    except apple_signin.SignInError as exc:
        raise web.HTTPUnauthorized(text=f"{exc}\n")
    people = request.app[PEOPLE]
    who = people.sign_in(identity.sub, identity.email, datetime.now())
    if who is None:
        code = waiting_code(identity.sub)
        log.info("sign-in waiting for approval: code %s", code)
        return web.json_response({"waiting": code}, status=403)
    return web.json_response({"token": people.new_session(who), "person": who})


async def sign_out(request: web.Request) -> web.Response:
    token = bearer(request)
    if token:
        request.app[PEOPLE].end_session(token)
    return web.Response(status=204)


async def me(request: web.Request) -> web.Response:
    settings = request.app[PEOPLE].settings(person(request))
    return web.json_response({"person": person(request), "settings": asdict(settings),
                              "spent_today_usd": round(spent_today(workspace(request), datetime.now()), 2),
                              "bee_connecting": person(request) in request.app[BEE_LOGINS]})


async def change_settings(request: web.Request) -> web.Response:
    try:
        changes = await request.json()
        if not isinstance(changes, dict):
            raise ValueError("expected a JSON object")
        settings = request.app[PEOPLE].change_settings(person(request), changes)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"{exc}\n")
    return web.json_response({"settings": asdict(settings)})


# ---------- calls ----------

def make_token(identity: str, room: str, answer: bool, key: str, secret: str,
               continue_from: str | None = None, who: str | None = None) -> str:
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
                                          metadata=call_metadata("inbound", continue_from=continue_from,
                                                                 person=who))]))
    return token.to_jwt()


async def livekit_room_exists(app: web.Application, room: str) -> bool:
    lk = api.LiveKitAPI(app[URL], app[KEY], app[SECRET])
    try:
        found = await lk.room.list_rooms(api.ListRoomsRequest(names=[room]))
    finally:
        await lk.aclose()
    return any(r.name == room for r in found.rooms)


async def token(request: web.Request) -> web.Response:
    who = person(request)
    answer = request.query.get("answer") == "1"
    room = request.query.get("room")
    if answer:
        # Only a room the AI opened for this person: the name carries the person (voice/dispatch.py room_name).
        if not room or not re.fullmatch(rf"call-{re.escape(who)}-\d{{8}}-\d{{6}}", room):
            raise web.HTTPBadRequest(text="answer=1 needs the room of the AI's call to you\n")
    else:
        room = room_name(who)
    continue_from = request.query.get("continue")  # "Continue this call" in an earlier call's record
    if continue_from is not None and (answer or session_dir(workspace(request), continue_from) is None):
        raise web.HTTPBadRequest(text="continue= needs the id of an earlier call, like 2026-10-14/0055\n")
    if not answer:
        settings = request.app[PEOPLE].settings(who)
        spent = spent_today(workspace(request), datetime.now())
        if spent >= settings.daily_budget_usd:
            raise web.HTTPTooManyRequests(text=f"today's budget of {settings.daily_budget_usd:.2f} USD is used up "
                                               f"(about {spent:.2f} USD so far)\n")
    if answer and not await request.app[ROOM_EXISTS](request.app, room):
        # Joining would create an empty room with no voice server in it.
        raise web.HTTPGone(text="that call has ended\n")
    jwt = make_token(who, room, answer, request.app[KEY], request.app[SECRET], continue_from, who)
    return web.json_response({"url": request.app[URL], "token": jwt, "room": room, "identity": who})


def recent_calls(log_path: Path, now: datetime) -> tuple[list[str], int]:
    """Lines for calls placed in the last RING_SECONDS, and the file offset to follow from."""
    if not log_path.exists():
        return [], 0
    data = log_path.read_bytes()
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
    log_path = workspace(request) / OUTBOUND_LOG
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    lines, pos = recent_calls(log_path, datetime.now())
    last_write = time.monotonic()
    try:
        while True:
            for line in lines:
                await resp.write(f"event: ring\ndata: {line}\n\n".encode())
                last_write = time.monotonic()
            lines = []
            await asyncio.sleep(0.5)
            if log_path.exists() and log_path.stat().st_size > pos:
                with log_path.open("rb") as f:
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
        voip_push.save_device_token(workspace(request), str(body["token"]), str(body["environment"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a VoIP token: {exc}")
    log.info("VoIP push token saved for %s (%s)", person(request), body["environment"])
    return web.Response(status=204)


async def alert_token(request: web.Request) -> web.Response:
    """The app's token for plain notifications (the offers that ask before a call)."""
    try:
        body = await request.json()
        voip_push.save_device_token(workspace(request), str(body["token"]), str(body["environment"]), "alert")
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a notification token: {exc}")
    log.info("notification token saved for %s (%s)", person(request), body["environment"])
    return web.Response(status=204)


async def list_offers(request: web.Request) -> web.Response:
    """The offers still waiting for an answer or a call, for the app's first screen."""
    store = OfferStore(workspace(request))
    return web.json_response({"offers": [o.view() for o in store.open(datetime.now())]})


async def answer_offer(request: web.Request) -> web.Response:
    """A notification button or the app: {"choice": "now" | "later" | "decline"}. voice/outbound.py places the call."""
    try:
        body = await request.json()
        choice = str(body["choice"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='expected {"choice": "now" | "later" | "decline"}\n')
    store = OfferStore(workspace(request))
    try:
        offer = store.answer(request.match_info["offer_id"], choice, datetime.now())
    except KeyError:
        raise web.HTTPNotFound(text="no such offer\n")
    except ValueError as exc:
        raise web.HTTPConflict(text=f"{exc}\n")
    log.info("offer %s of %s: %s -> %s", offer.id, person(request), choice, offer.state)
    add_event(workspace(request), "offer_answered", offer=offer.id, choice=choice, topic=offer.topic)
    return web.json_response({"offer": offer.view()})


# ---------- Bee ----------

async def connect_bee(request: web.Request) -> web.Response:
    who, people = person(request), request.app[PEOPLE]
    folder = people.bee_dir(who)
    try:
        link = await bee.start_login(folder)
    except (bee.BeeError, OSError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(text=f"could not start a Bee login: {exc}\n")
    waiting = request.app[BEE_LOGINS]
    if who in waiting:
        waiting.pop(who).cancel()

    async def wait_for_approval() -> None:
        try:
            if await bee.finish_login(folder, timeout=BEE_APPROVAL_SECONDS):
                people.change_settings(who, {"bee": True}, allowed=("bee",))
                log.info("Bee connected for %s", who)
        except Exception:
            log.exception("finishing the Bee login for %s failed", who)
        finally:
            if waiting.get(who) is asyncio.current_task():
                waiting.pop(who)

    waiting[who] = asyncio.create_task(wait_for_approval())
    return web.json_response({"link": link})


async def disconnect_bee(request: web.Request) -> web.Response:
    who, people = person(request), request.app[PEOPLE]
    task = request.app[BEE_LOGINS].pop(who, None)
    if task:
        task.cancel()
    try:
        await bee.logout(people.bee_dir(who))
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("bee logout for %s failed: %s", who, exc)
    people.change_settings(who, {"bee": False}, allowed=("bee",))
    return web.Response(status=204)


async def stop_bee_logins(app: web.Application) -> None:
    for task in app[BEE_LOGINS].values():
        task.cancel()


def make_app(url: str, key: str, secret: str, people: People, room_exists=livekit_room_exists,
             verify=apple_signin.verify) -> web.Application:
    app = web.Application(middlewares=[signed_in])
    app[URL], app[KEY], app[SECRET], app[ROOM_EXISTS] = url, key, secret, room_exists
    app[PEOPLE], app[NONCES], app[VERIFY], app[BEE_LOGINS] = people, apple_signin.Nonces(), verify, {}
    app.on_shutdown.append(stop_bee_logins)
    app.router.add_get("/auth/nonce", new_nonce)
    app.router.add_post("/auth/apple", apple_sign_in)
    app.router.add_post("/auth/signout", sign_out)
    app.router.add_get("/me", me)
    app.router.add_put("/settings", change_settings)
    app.router.add_get("/token", token)
    app.router.add_get("/ring", ring)
    app.router.add_post("/voip", voip_token)
    app.router.add_post("/push", alert_token)
    app.router.add_get("/offers", list_offers)
    app.router.add_post("/offers/{offer_id}", answer_offer)
    app.router.add_post("/bee/connect", connect_bee)
    app.router.add_post("/bee/disconnect", disconnect_bee)
    return app


if __name__ == "__main__":
    # One line per request, so a phone that cannot reach the server is easy to tell apart.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    bee.use_bee(None)  # Bee works only through a person's own login (beelink/cli.py)
    # The default route can be a VPN, so list the Wi-Fi addresses a phone can actually reach.
    from voice.session_server import lan_addresses

    port = int(os.environ.get("TOKEN_SERVER_PORT", "8787"))
    people = People()
    app = make_app(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"), people)
    print(f"token server on port {port}, people in {people.root.resolve()}. For the iPhone app on the same Wi-Fi:",
          flush=True)
    for iface, ip in lan_addresses() or [("this Mac only", "127.0.0.1")]:
        print(f"  http://{ip}:{port}/token  ({iface})", flush=True)
    if dev_person():
        print(f"requests made on this Mac without a sign-in act as {dev_person()} (BEE_CALL_DEV_PERSON)", flush=True)
    web.run_app(app, host="0.0.0.0", port=port, print=None)
