"""Hands out LiveKit room tokens to the iPhone app, and everything else the app asks this server for.

Every request except signing in carries the app's sign-in token (Authorization: Bearer <token>) and works only
on that person's own folder (voice/people.py). On a Mac, BEE_CALL_DEV_PERSON lets requests made on the machine
itself in without a token when they carry the header X-Bee-Call-Dev: 1 (curl, test callers); a web page in a
browser cannot send that header to another site without asking first, and this server never says yes. The dev
fallback is off on a server (BEE_CALL_PUBLIC_HOST set), and requests must name a known host.

Run: uv run python -m voice.token_server

Signing in (Sign in with Apple, voice/apple_signin.py):
GET  /auth/nonce                         {"nonce"}: a one-time value the app hashes into Apple's sign-in request
POST /auth/apple {"identity_token", "nonce"}
                                         200 {"token", "person"}; 403 {"waiting": CODE} until Matt approves that
                                         sign-in (uv run python -m voice.people approve CODE --as ID)
POST /auth/signout                       ends this sign-in; the push tokens it registered go with it
GET  /me                                 who is signed in, their settings and today's estimated spend
PUT  /settings {"language": "ko", ...}   the settings the app may change (voice/people.py APP_SETTINGS), with
                                         time_zone, the phone's IANA zone (400 for a name zoneinfo does not know),
                                         and call_model, "haiku" or "sonnet" (voice/call_models.py)

Calls:
GET  /token                              a token for a new call the user starts; the voice server is dispatched
                                         into the room when the app joins. 429 once today's budget is spent
GET  /token?room=R&answer=1              a token to answer the AI's call in room R (already dispatched); 410 if
                                         the room is gone (the voice server gave up after RING_SECONDS)
GET  /token?continue=D/T                 a new call that continues the earlier call D/T (voice/continuation.py)
GET  /ring                               Server-Sent Events: one "ring" event per call the AI places for this
                                         person (calls placed in the last RING_SECONDS and not declined, then new
                                         ones): how an open app learns the AI is calling without VoIP push
POST /calls/decline {"room": R}          the user declined the AI's call in room R while it rang: 204 (also once
                                         it is over, and again), 400 for a room that is not this person's. The
                                         voice server stops waiting and records it as declined; /ring never
                                         replays it
POST /voip  {"token": hex, "environment": "development"|"production"}
                                         the app's VoIP push token, so an AI call rings a closed app too. It
                                         belongs to the signed-in person alone from then on (another person who
                                         had it loses it) and leaves with this sign-in
POST /push  {"token": hex, "environment": ...}
                                         the app's token for plain notifications (the offers that ask first),
                                         kept the same way
GET  /offers                             offers still waiting for an answer or a call
POST /offers/<id>  {"choice": "now"|"later"|"decline"}
                                         the user's answer; voice/outbound.py places the call. "decline" also
                                         calls off a booked call until its room is being made. 404 unknown
                                         (checked first), 400 another choice, 409 already answered or closed

Bee, for the people who use it:
POST /bee/connect                        {"link"}: the approval link the app opens. The server waits for the
                                         approval in the background, then turns the setting "bee" on
POST /bee/disconnect                     logs this person's Bee out and turns "bee" off
"""

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

from beelink import cli as bee
from beelink.clock import parse_time
from beelink.planner import OUTBOUND_LOG
from meta.session import add_event
from voice import apple_signin, voip_push
from voice.continuation import session_dir
from voice.costs import day_budget
from voice.dispatch import AGENT_NAME, RING_SECONDS, call_metadata, call_room_of, decline_ring, declined_rooms, \
    room_name
from voice.offers import OfferClosed, OfferStore, UnknownChoice
from voice.people import People, Person, dev_person, waiting_code
from voice.session_server import local_host

load_dotenv(".env.local")
log = logging.getLogger("bee-call.token")
KEEPALIVE_SECONDS = 15.0
BEE_APPROVAL_SECONDS = 330  # Bee's approval link lasts about 5 minutes (bee login --no-wait)
# A Bee login waiting for the person's approval, kept in their Bee folder, so a restart of this server goes on
# waiting for it: {"started_at": epoch seconds}.
BEE_PENDING = "pending.json"
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


def dev_request(request: web.Request) -> bool:
    return (not os.environ.get("BEE_CALL_PUBLIC_HOST") and from_this_machine(request)
            and request.headers.get("X-Bee-Call-Dev") == "1")


@web.middleware
async def known_hosts_only(request: web.Request, handler):
    """Refuses host names other than this machine's and the server's own, so a web page cannot point its own
    domain at this server (DNS rebinding)."""
    public = os.environ.get("BEE_CALL_PUBLIC_HOST", "").strip().lower()
    host = (request.url.host or "").lower().rstrip(".")
    if not local_host(request.url.host) and not (public and host == public):
        raise web.HTTPMisdirectedRequest(text="unknown host name\n")
    return await handler(request)


@web.middleware
async def signed_in(request: web.Request, handler):
    if request.path in OPEN_PATHS:
        return await handler(request)
    person = request.app[PEOPLE].who(bearer(request))
    if person is None and not bearer(request) and dev_request(request):
        person = dev_person()
    if person is None:
        raise web.HTTPUnauthorized(text="sign in first\n")
    request[PERSON] = person
    return await handler(request)


def person(request: web.Request) -> str:
    return request[PERSON]


def workspace(request: web.Request) -> Path:
    return request.app[PEOPLE].workspace(person(request))


def someone(request: web.Request) -> Person:
    """The signed-in person, for what acts on their behalf (their phone's push tokens)."""
    return request.app[PEOPLE].person(person(request))


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
                              "spent_today_usd": round(day_budget(someone(request), someone(request).now()).spent, 2),
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
        if not call_room_of(who, room):
            raise web.HTTPBadRequest(text="answer=1 needs the room of the AI's call to you\n")
    else:
        room = room_name(who)
    continue_from = request.query.get("continue")  # "Continue this call" in an earlier call's record
    if continue_from is not None and (answer or session_dir(workspace(request), continue_from) is None):
        raise web.HTTPBadRequest(text="continue= needs the id of an earlier call, like 2026-10-14/0055\n")
    if not answer:
        budget = day_budget(someone(request), someone(request).now())
        if budget.used_up:
            raise web.HTTPTooManyRequests(text=f"{budget.why()}\n")
    if answer and not await request.app[ROOM_EXISTS](request.app, room):
        # Joining would create an empty room with no voice server in it.
        raise web.HTTPGone(text="that call has ended\n")
    jwt = make_token(who, room, answer, request.app[KEY], request.app[SECRET], continue_from, who)
    return web.json_response({"url": request.app[URL], "token": jwt, "room": room, "identity": who})


def recent_calls(log_path: Path, now: datetime) -> tuple[list[str], int]:
    """Lines for calls placed in the last RING_SECONDS that the user did not decline, and the file offset to follow
    from. The log is workspace/state/outbound.jsonl (voice/dispatch.py)."""
    if not log_path.exists():
        return [], 0
    data = log_path.read_bytes()
    end = data.rfind(b"\n") + 1
    declined = declined_rooms(log_path.parent.parent)
    lines = []
    for line in data[:end].decode(errors="replace").splitlines():
        try:
            ring_line = json.loads(line)
            placed = parse_time(ring_line["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if (now - placed).total_seconds() < RING_SECONDS and ring_line.get("room") not in declined:
            lines.append(line)
    return lines, end


async def ring(request: web.Request) -> web.StreamResponse:
    log_path = workspace(request) / OUTBOUND_LOG
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    lines, pos = recent_calls(log_path, someone(request).now())
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


async def decline_call(request: web.Request) -> web.Response:
    """The user declined the AI's call while it rang (the CallKit end action before answering)."""
    try:
        body = await request.json()
        room = body["room"] if isinstance(body, dict) else None
    except (ValueError, KeyError, TypeError):
        room = None
    if not isinstance(room, str) or not call_room_of(person(request), room):
        raise web.HTTPBadRequest(text='expected {"room": the room of the AI\'s call to you}\n')
    decline_ring(someone(request), room)
    log.info("%s declined the call in room %s", person(request), room)
    return web.Response(status=204)


async def voip_token(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        voip_push.save_device_token(someone(request), str(body["token"]), str(body["environment"]),
                                    session_token=bearer(request))
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a VoIP token: {exc}")
    log.info("VoIP push token saved for %s (%s)", person(request), body["environment"])
    return web.Response(status=204)


async def alert_token(request: web.Request) -> web.Response:
    """The app's token for plain notifications (the offers that ask before a call)."""
    try:
        body = await request.json()
        voip_push.save_device_token(someone(request), str(body["token"]), str(body["environment"]), "alert",
                                    session_token=bearer(request))
    except (ValueError, KeyError, TypeError) as exc:
        raise web.HTTPBadRequest(text=f"expected a notification token: {exc}")
    log.info("notification token saved for %s (%s)", person(request), body["environment"])
    return web.Response(status=204)


async def list_offers(request: web.Request) -> web.Response:
    """The offers still waiting for an answer or a call, for the app's first screen."""
    store = OfferStore(workspace(request))
    return web.json_response({"offers": [o.view() for o in store.open(someone(request).now())]})


async def answer_offer(request: web.Request) -> web.Response:
    """A notification button or the app: {"choice": "now" | "later" | "decline"}. voice/outbound.py places the call."""
    store = OfferStore(workspace(request))
    if store.get(request.match_info["offer_id"]) is None:  # contract section 4: an unknown offer is 404 first
        raise web.HTTPNotFound(text="no such offer\n")
    try:
        body = await request.json()
        choice = str(body["choice"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='expected {"choice": "now" | "later" | "decline"}\n')
    now = someone(request).now()
    try:
        offer = store.answer(request.match_info["offer_id"], choice, now)
    except KeyError:
        raise web.HTTPNotFound(text="no such offer\n")
    except UnknownChoice as exc:
        raise web.HTTPBadRequest(text=f"{exc}\n")
    except OfferClosed as exc:
        raise web.HTTPConflict(text=f"{exc}\n")
    log.info("offer %s of %s: %s -> %s", offer.id, person(request), choice, offer.state)
    add_event(workspace(request), "offer_answered", now=now, offer=offer.id, choice=choice, topic=offer.topic)
    return web.json_response({"offer": offer.view()})


# ---------- Bee ----------

def wait_for_bee_approval(app: web.Application, who: str, seconds: float) -> None:
    """Waits (in the background) up to `seconds` for the person to approve their Bee login, then turns Bee on.
    The pending file stays while the wait is only stopped (a restart, another connect), so it can go on."""
    people = app[PEOPLE]
    folder = people.bee_dir(who)
    waiting = app[BEE_LOGINS]

    async def wait() -> None:
        try:
            try:
                if await bee.finish_login(folder, timeout=seconds):
                    people.change_settings(who, {"bee": True}, allowed=("bee",))
                    log.info("Bee connected for %s", who)
            except asyncio.CancelledError:
                raise  # a restart or another connect: the pending login is kept
            except Exception:
                log.exception("finishing the Bee login for %s failed", who)
            (folder / BEE_PENDING).unlink(missing_ok=True)  # it ended: approved, timed out or failed
        finally:
            if waiting.get(who) is asyncio.current_task():
                waiting.pop(who)

    waiting[who] = asyncio.create_task(wait())


async def stop_waiting_for_bee(app: web.Application, who: str) -> None:
    """Stops a wait for this person's Bee approval, and its `bee login`, before anything else runs on the folder."""
    task = app[BEE_LOGINS].pop(who, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def connect_bee(request: web.Request) -> web.Response:
    who, people = person(request), request.app[PEOPLE]
    folder = people.bee_dir(who)
    await stop_waiting_for_bee(request.app, who)  # one `bee login` at a time on a folder
    try:
        link = await bee.start_login(folder)
    except (bee.BeeError, OSError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(text=f"could not start a Bee login: {exc}\n")
    (folder / BEE_PENDING).write_text(json.dumps({"started_at": time.time()}))
    wait_for_bee_approval(request.app, who, BEE_APPROVAL_SECONDS)
    return web.json_response({"link": link})


async def disconnect_bee(request: web.Request) -> web.Response:
    """Bee off, and the person's Bee login gone from this server, whatever `bee logout` says: 204 only once the
    login folder is removed (the app clears its status on 204)."""
    who, people = person(request), request.app[PEOPLE]
    await stop_waiting_for_bee(request.app, who)
    folder = people.bee_dir(who)
    try:
        await bee.logout(folder)
    except (bee.BeeError, OSError, asyncio.TimeoutError) as exc:
        log.warning("bee logout for %s failed (%s); removing the login here anyway", who, exc)
    people.change_settings(who, {"bee": False}, allowed=("bee",))
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("could not remove the Bee login of %s: %s", who, exc)
        raise web.HTTPBadGateway(text="Bee is off, but the server could not remove the Bee login; try again\n")
    return web.Response(status=204)


async def resume_bee_logins(app: web.Application) -> None:
    """At start: goes on waiting for the Bee logins a restart interrupted, while their approval links last."""
    people = app[PEOPLE]
    for pending in sorted((people.root / "bee").glob(f"*/{BEE_PENDING}")):
        who = pending.parent.name
        try:
            started = float(json.loads(pending.read_text())["started_at"])
        except (OSError, ValueError, KeyError, TypeError):
            started = 0.0
        left = BEE_APPROVAL_SECONDS - (time.time() - started)
        if left > 0 and people.known(who):
            wait_for_bee_approval(app, who, left)
        else:
            pending.unlink(missing_ok=True)


async def stop_bee_logins(app: web.Application) -> None:
    for task in app[BEE_LOGINS].values():
        task.cancel()


def make_app(url: str, key: str, secret: str, people: People, room_exists=livekit_room_exists,
             verify=apple_signin.verify) -> web.Application:
    app = web.Application(middlewares=[known_hosts_only, signed_in])
    app[URL], app[KEY], app[SECRET], app[ROOM_EXISTS] = url, key, secret, room_exists
    app[PEOPLE], app[NONCES], app[VERIFY], app[BEE_LOGINS] = people, apple_signin.Nonces(), verify, {}
    app.on_startup.append(resume_bee_logins)
    app.on_shutdown.append(stop_bee_logins)
    app.router.add_get("/auth/nonce", new_nonce)
    app.router.add_post("/auth/apple", apple_sign_in)
    app.router.add_post("/auth/signout", sign_out)
    app.router.add_get("/me", me)
    app.router.add_put("/settings", change_settings)
    app.router.add_get("/token", token)
    app.router.add_get("/ring", ring)
    app.router.add_post("/calls/decline", decline_call)
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
        print(f"requests made on this Mac without a sign-in, with the header X-Bee-Call-Dev: 1, act as {dev_person()} "
              "(BEE_CALL_DEV_PERSON)", flush=True)
    web.run_app(app, host="0.0.0.0", port=port, print=None)
