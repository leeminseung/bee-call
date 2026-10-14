"""One server, several people (voice/people.py): sign-in, settings, and that one person's things never reach
another person."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa

from beelink import cli
from tests.helpers import signed_in
from voice import apple_signin, costs, session_server, token_server, voip_push
from voice.people import People, Settings, waiting_code
from voice.recorder import CallRecorder

FAKE_BEE = Path(__file__).parent / "fakebee" / "bee"
BUNDLE = "com.leeminseung.beecall"
APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def apple_token(nonce: str, sub: str = "001234.abcd", aud: str = BUNDLE, iss: str = apple_signin.APPLE_ISSUER,
                expires_in: int = 600, hashed: str | None = None) -> str:
    import hashlib
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + expires_in,
              "nonce": hashed or hashlib.sha256(nonce.encode()).hexdigest(), "email": "friend@privaterelay.appleid.com"}
    return jwt.encode(claims, APPLE_KEY, algorithm="RS256", headers={"kid": "test"})


def verify_with_test_key(token: str, audience: str, nonce: str):
    return apple_signin.verify(token, audience, nonce, key_for=lambda _: APPLE_KEY.public_key())


def app_for(people: People, room_exists=None):
    return token_server.make_app("wss://example", "APIkey", "s" * 32, people, room_exists=room_exists,
                                 verify=verify_with_test_key)


# ---------- people and settings ----------

def test_person_ids_cannot_name_another_folder(tmp_path):
    people = People(tmp_path)
    for bad in ("../matt", "Matt", "", "a/b", ".hidden", "x" * 40, None):
        with pytest.raises(ValueError):
            people.folder(bad)
    assert people.folder("jiwoo") == tmp_path / "people" / "jiwoo"


def test_settings_are_checked_and_the_app_changes_only_its_own(tmp_path):
    people = People(tmp_path)
    people.add("matt")
    assert people.settings("matt") == Settings()
    changed = people.change_settings("matt", {"language": "ko", "call_time": "20:30", "record_on_server": False})
    assert (changed.language, changed.call_time, changed.record_on_server) == ("ko", "20:30", False)
    for bad in ({"language": "fr"}, {"call_time": "25:00"}, {"record_on_phone": "yes"}, {"daily_budget_usd": 50},
                {"bee": True}, {"nonsense": 1}):
        with pytest.raises(ValueError):
            people.change_settings("matt", bad)
    assert people.settings("matt") == changed  # a refused change changes nothing
    # A broken file keeps the defaults instead of failing a call.
    (people.folder("matt") / "settings.json").write_text('{"language": "fr", "call_time": 9}')
    assert people.settings("matt") == Settings()


def test_sign_ins_wait_for_approval_and_end_when_the_person_is_removed(tmp_path):
    people = People(tmp_path)
    now = datetime(2026, 10, 14, 21, 0)
    assert people.sign_in("001234.abcd", "friend@example.com", now) is None
    (waiting,) = people.waiting()
    assert waiting["code"] == waiting_code("001234.abcd") and waiting["email"] == "friend@example.com"
    with pytest.raises(ValueError):
        people.approve("ZZZZZZ", "jiwoo")
    people.approve(waiting["code"], "jiwoo")
    assert people.waiting() == [] and people.ids() == ["jiwoo"]
    assert people.sign_in("001234.abcd", None, now) == "jiwoo"
    token = people.new_session("jiwoo", now)
    assert people.who(token, now) == "jiwoo"
    assert people.who(token, now + timedelta(days=181)) is None  # sign-ins expire
    assert people.who("made-up", now) is None and people.who(None, now) is None
    stored = (tmp_path / "sessions.json").read_text()
    assert token not in stored  # only its hash is kept
    people.remove("jiwoo")
    assert people.who(token, now) is None and people.ids() == []
    assert people.folder("jiwoo").is_dir()  # their calls stay
    with pytest.raises(ValueError):
        people.new_session("jiwoo")


# ---------- Sign in with Apple ----------

def test_only_apple_tokens_made_for_this_app_and_this_request_sign_in():
    good = apple_token("n1")
    assert verify_with_test_key(good, BUNDLE, "n1").sub == "001234.abcd"
    for token, nonce in ((apple_token("n1", aud="com.someone.else"), "n1"),
                         (apple_token("n1", iss="https://evil.example"), "n1"),
                         (apple_token("n1", expires_in=-60), "n1"),
                         (good, "n2")):
        with pytest.raises(apple_signin.SignInError):
            verify_with_test_key(token, BUNDLE, nonce)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(apple_signin.SignInError):
        apple_signin.verify(good, BUNDLE, "n1", key_for=lambda _: other_key.public_key())


def test_a_nonce_works_once():
    nonces = apple_signin.Nonces()
    nonce = nonces.new()
    assert nonces.use(nonce) and not nonces.use(nonce) and not nonces.use("made-up")


def test_signing_in_from_the_app(tmp_path):
    people = People(tmp_path / "data")

    async def sign_in(client):
        nonce = (await (await client.get("/auth/nonce")).json())["nonce"]
        return await client.post("/auth/apple", json={"identity_token": apple_token(nonce), "nonce": nonce})

    async def main():
        async with TestClient(TestServer(app_for(people))) as client:
            assert (await client.get("/me")).status == 401
            first = await sign_in(client)
            code = (await first.json())["waiting"]
            assert first.status == 403 and code == waiting_code("001234.abcd")
            people.approve(code, "jiwoo")
            second = await sign_in(client)
            body = await second.json()
            replayed = await client.post("/auth/apple", json={"identity_token": apple_token("x"), "nonce": "x"})
            me = await (await client.get("/me", headers={"Authorization": f"Bearer {body['token']}"})).json()
            out = await client.post("/auth/signout", headers={"Authorization": f"Bearer {body['token']}"})
            after = await client.get("/me", headers={"Authorization": f"Bearer {body['token']}"})
            return second.status, body, replayed.status, me, out.status, after.status

    status, body, replayed, me, out, after = asyncio.run(main())
    assert status == 200 and body["person"] == "jiwoo"
    assert replayed == 400  # a nonce the server never gave out
    assert me["person"] == "jiwoo" and me["settings"]["language"] == "en" and me["spent_today_usd"] == 0
    assert (out, after) == (204, 401)


def test_the_app_changes_its_settings(tmp_path):
    me = signed_in(tmp_path)

    async def main():
        async with TestClient(TestServer(app_for(me.people)), headers=me.headers) as client:
            good = await client.put("/settings", json={"language": "ko", "record_on_phone": True})
            bad = await client.put("/settings", json={"daily_budget_usd": 99})
            return good.status, await good.json(), bad.status

    good, body, bad = asyncio.run(main())
    assert good == 200 and body["settings"]["language"] == "ko" and body["settings"]["record_on_phone"] is True
    assert bad == 400 and me.people.settings("matt").daily_budget_usd == Settings().daily_budget_usd


def test_without_a_sign_in_only_this_machine_gets_the_dev_person(tmp_path, monkeypatch):
    me = signed_in(tmp_path)
    dev = {"X-Bee-Call-Dev": "1"}

    async def main():
        async with TestClient(TestServer(app_for(me.people))) as client:
            plain = await client.get("/me")
            marked = await client.get("/me", headers=dev)
            proxied = await client.get("/me", headers={**dev, "X-Forwarded-For": "203.0.113.9"})
            wrong = await client.get("/me", headers={**dev, "Authorization": "Bearer made-up"})
            elsewhere = await client.get("/me", headers={**dev, "Host": "evil.example"})
            return plain.status, marked.status, proxied.status, wrong.status, elsewhere.status

    assert asyncio.run(main()) == (401, 401, 401, 401, 421)
    monkeypatch.setenv("BEE_CALL_DEV_PERSON", "matt")
    # Only from this machine, with the header no web page can send cross-site, and only off a server.
    assert asyncio.run(main()) == (401, 200, 401, 401, 421)
    monkeypatch.setenv("BEE_CALL_PUBLIC_HOST", "call.example.com")
    assert asyncio.run(main()) == (401, 401, 401, 401, 421)


# ---------- one person's things stay theirs ----------

def test_people_see_only_their_own_offers_tokens_and_calls(tmp_path):
    from tests.test_offers import asked

    matt = signed_in(tmp_path, "matt")
    jiwoo_token = (matt.people.add("jiwoo"), matt.people.new_session("jiwoo"))[1]
    jiwoo = {"Authorization": f"Bearer {jiwoo_token}"}
    offer, _ = asked(tmp_path, now=datetime.now(), workspace=matt.workspace)
    (matt.workspace / "2026-10-14" / "2100").mkdir(parents=True)
    (matt.workspace / "2026-10-14" / "2100" / "events.jsonl").write_text('{"t": 0, "type": "call_started"}\n')

    async def live(app, room):
        return True

    async def main():
        async with TestClient(TestServer(app_for(matt.people, room_exists=live))) as client:
            listed = await (await client.get("/offers", headers=jiwoo)).json()
            answer = await client.post(f"/offers/{offer.id}", json={"choice": "now"}, headers=jiwoo)
            push = await client.post("/push", json={"token": "ab" * 32, "environment": "production"}, headers=jiwoo)
            others_room = await client.get("/token", params={"room": "call-matt-20261014-210000", "answer": "1"},
                                           headers=jiwoo)
            own_room = await client.get("/token", params={"room": "call-jiwoo-20261014-210000", "answer": "1"},
                                        headers=jiwoo)
            continued = await client.get("/token", params={"continue": "2026-10-14/2100"}, headers=jiwoo)
            new = await (await client.get("/token", headers=jiwoo)).json()
            return (listed, answer.status, push.status, others_room.status, own_room.status, continued.status, new)

    listed, answer, push, others_room, own_room, continued, new = asyncio.run(main())
    assert listed == {"offers": []} and answer == 404  # matt's offer is not there for jiwoo
    assert push == 204 and voip_push.load_device_token(matt.workspace, "alert") is None
    assert voip_push.load_device_token(matt.people.workspace("jiwoo"), "alert")["environment"] == "production"
    assert (others_room, own_room, continued) == (400, 200, 400)
    claims = jwt.decode(new["token"], options={"verify_signature": False})
    assert new["room"].startswith("call-jiwoo-") and claims["sub"] == "jiwoo"
    assert json.loads(claims["roomConfig"]["agents"][0]["metadata"])["person"] == "jiwoo"


def test_the_record_screen_shows_each_person_only_their_calls(tmp_path, monkeypatch):
    matt = signed_in(tmp_path, "matt")
    matt.people.add("jiwoo")
    jiwoo_token = matt.people.new_session("jiwoo")
    folder = matt.workspace / "2026-10-14" / "2100"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text('{"t": 0, "type": "call_started", "room": "call-matt-x"}\n')
    (folder / "transcript.md").write_text("# Call\n")
    monkeypatch.setenv("BEE_CALL_PUBLIC_HOST", "call.example.com")

    async def main():
        app = session_server.make_app(matt.people)
        async with TestClient(TestServer(app)) as client:
            nobody = await client.get("/api/sessions")
            mine = await (await client.get("/api/sessions", headers=matt.headers)).json()
            theirs = await (await client.get("/api/sessions", headers={"Authorization": f"Bearer {jiwoo_token}"})).json()
            peek = await client.get("/api/sessions/2026-10-14/2100/files/transcript.md",
                                    headers={"Authorization": f"Bearer {jiwoo_token}"})
            cookie = await client.get("/api/sessions/2026-10-14/2100/files/transcript.md",
                                      cookies={session_server.SESSION_COOKIE: matt.token})
            public = await client.get("/api/sessions", headers={**matt.headers, "Host": "call.example.com"})
            stranger = await client.get("/api/sessions", headers={**matt.headers, "Host": "evil.example"})
            page = await client.get("/")
            return nobody.status, mine, theirs, peek.status, cookie.status, public.status, stranger.status, page.status

    nobody, mine, theirs, peek, cookie, public, stranger, page = asyncio.run(main())
    assert nobody == 401 and [s["id"] for s in mine] == ["2026-10-14/2100"] and theirs == []
    assert (peek, cookie, public, stranger, page) == (404, 200, 200, 421, 200)


# ---------- Bee, per person ----------

@pytest.fixture
def fake_bee(tmp_path, monkeypatch):
    log = tmp_path / "bee.log"
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(log))
    return log


def who_log(log: Path) -> list[dict]:
    path = Path(str(log) + ".who")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_each_person_s_bee_calls_use_their_own_login(tmp_path, fake_bee):
    people = People(tmp_path / "data")

    async def as_person(person: str | None):
        cli.use_bee(people.bee_dir(person) if person else None)
        try:
            return await cli.me()
        except cli.BeeError as exc:
            return str(exc)

    async def main():
        return await asyncio.gather(as_person("matt"), as_person("jiwoo"), as_person(None))

    matt, jiwoo, nobody = asyncio.run(main())
    assert matt["first_name"] == "Matt" and jiwoo["first_name"] == "Matt"  # the fake answers the same
    assert nobody == "Bee is not connected for this person"
    folders = {Path(entry["folder"]) for entry in who_log(fake_bee)}
    assert folders == {people.bee_dir("matt"), people.bee_dir("jiwoo")}


def test_bee_calls_fail_until_someone_s_login_is_chosen(tmp_path, fake_bee):
    async def nobody_chosen():
        try:
            await cli.me()
        except cli.BeeError as exc:
            return str(exc)

    import contextvars
    # A fresh context, as if a new loop forgot use_bee(): no fallback to this machine's own Bee login.
    assert "no person's Bee login was chosen" in contextvars.Context().run(asyncio.run, nobody_chosen())


def test_the_meta_agent_s_file_tools_cannot_reach_a_bee_login(tmp_path):
    from meta import files

    people = People(tmp_path / "data")
    people.add("matt")
    ws = people.workspace("matt")
    login = people.bee_dir("matt")
    login.mkdir(parents=True)
    (login / "token-prod").write_text("secret-bee-token")
    assert not login.resolve().is_relative_to(ws.resolve())  # outside the folder the tools browse
    # Even a login left inside a workspace (the layout before) stays hidden.
    (ws / "bee").mkdir()
    (ws / "bee" / "token-prod").write_text("secret-bee-token")
    (ws / "bee" / "pairing-prod.json").write_text('{"secret": "x"}')
    assert "token-prod" not in files.list_files(ws, "bee") and "pairing" not in files.list_files(ws, "bee")
    with pytest.raises(files.FileError):
        files.read_file(ws, "bee/token-prod")
    assert "secret" not in files.grep(ws, "secret")


def test_connecting_and_disconnecting_bee_from_the_app(tmp_path, fake_bee, monkeypatch):
    me = signed_in(tmp_path)
    folder = me.people.bee_dir("matt")

    async def main():
        async with TestClient(TestServer(app_for(me.people)), headers=me.headers) as client:
            started = await (await client.post("/bee/connect")).json()
            connecting = (await (await client.get("/me")).json())["bee_connecting"]
            (folder / "approve").write_text("")  # the person taps the approval link on their phone
            for _ in range(100):
                if me.people.settings("matt").bee:
                    break
                await asyncio.sleep(0.05)
            connected = me.people.settings("matt").bee
            off = await client.post("/bee/disconnect")
            return started, connecting, connected, off.status

    started, connecting, connected, off = asyncio.run(main())
    assert started == {"link": "https://bee.computer/connect#fake-pairing"} and connecting is True
    assert connected is True and off == 204 and me.people.settings("matt").bee is False
    assert not (folder / "token-prod").exists()


# ---------- the daily budget ----------

def test_spend_counts_calls_prep_jobs_searches_and_meta_thinks_of_today(tmp_path):
    from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage

    ws = tmp_path
    now = datetime(2026, 10, 14, 21, 0)
    done = ws / "2026-10-14" / "2000"
    (done / "jobs" / "01").mkdir(parents=True)
    # What voice/agent.py writes: LiveKit's own usage entries, with the providers as its plugins name them.
    models = [LLMModelUsage(provider="AWS Bedrock", model="zai.glm-5", input_tokens=1_000_000),
              STTModelUsage(provider="Amazon Transcribe", model="en-US", audio_duration=600.0),
              TTSModelUsage(provider="Amazon Polly", model="neural", characters_count=10_000),
              TTSModelUsage(provider="ElevenLabs", model="eleven_v4_turbo", characters_count=5000)]
    (done / "usage.json").write_text(json.dumps({"minutes": 10, "models": [m.model_dump() for m in models]}, default=str))
    (done / "events.jsonl").write_text(json.dumps({"t": 1.0, "type": "call_prep", "model": "zai.glm-5",
                                                   "input_tokens": 100_000, "output_tokens": 0}) + "\n")
    (done / "jobs" / "01" / "usage.json").write_text(json.dumps({
        "input_tokens": 0, "output_tokens": 1_000_000,
        "steps": [{"round_s": 3.0, "calls": [{"name": "web_search", "s": 9.0}, {"name": "save_file", "s": 0.1}]},
                  {"round_s": 2.0, "calls": [{"name": "web_search", "s": 8.0}]}]}))
    going = ws / "2026-10-14" / "2050"
    going.mkdir(parents=True)
    (going / "events.jsonl").write_text('{"t": 0.0, "type": "call_started"}\n{"t": 120.0, "type": "message"}\n')
    yesterday = ws / "2026-10-13" / "2000"
    yesterday.mkdir(parents=True)
    (yesterday / "usage.json").write_text(json.dumps({"models": [
        STTModelUsage(provider="Amazon Transcribe", model="en-US", audio_duration=6000.0).model_dump()]}))
    (ws / "meta").mkdir()
    (ws / "meta" / "session.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"at": "2026-10-14T19:00:00", "model": "zai.glm-5", "usage": {"input_tokens": 500_000, "output_tokens": 0}},
        {"at": "2026-10-13T19:00:00", "model": "zai.glm-5", "usage": {"input_tokens": 9_000_000, "output_tokens": 0}},
    ]) + "\n")
    call = 1.00 + 600 * costs.STT_USD_PER_SECOND + 10_000 * costs.POLLY_USD_PER_CHAR["neural"]  # ElevenLabs: 0
    prep, job = 0.10, 3.20 + 2 * costs.WEB_SEARCH_USD
    expected = call + prep + job + 2 * costs.CALL_USD_PER_MINUTE + 0.50
    assert costs.spent_today(ws, now) == pytest.approx(expected)


def test_a_new_call_waits_for_tomorrow_once_the_budget_is_spent(tmp_path):
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
    call = me.workspace / datetime.now().strftime("%Y-%m-%d") / "0001"
    call.mkdir(parents=True)
    (call / "usage.json").write_text(json.dumps({"models": [
        {"type": "llm_usage", "provider": "aws", "model": "zai.glm-5", "input_tokens": 1_000_000}]}))

    async def main():
        async with TestClient(TestServer(app_for(me.people)), headers=me.headers) as client:
            return (await client.get("/token")).status

    assert asyncio.run(main()) == 429


def test_over_budget_an_agreed_call_is_not_placed(tmp_path):
    from tests.test_offers import asked
    from voice.offers import OfferStore
    from voice.outbound import call_due

    offer, _ = asked(tmp_path, now=datetime.now())
    store = OfferStore(tmp_path / "workspace")
    store.answer(offer.id, "now", datetime.now())
    placed = []

    async def place(*a, **kw):
        placed.append(kw)
        return "call-x"

    assert asyncio.run(call_due(tmp_path / "workspace", place=place, over_budget=lambda now: True)) == []
    assert placed == [] and store.get(offer.id).state == "failed" and "budget" in store.get(offer.id).error


def test_an_agreed_call_carries_its_person(tmp_path):
    from tests.test_offers import asked
    from voice.offers import OfferStore
    from voice.outbound import call_due
    from voice.prep import Prep

    offer, _ = asked(tmp_path, now=datetime.now())
    OfferStore(tmp_path / "workspace").answer(offer.id, "now", datetime.now())
    seen = {}

    async def prepare(plan, extra=None, deadline_s=None, language=None):
        seen["language"] = language.code
        return Prep(note="n", opening="o")

    async def place(plan, ws, prep=None, continue_from=None, agreed=None, person=None):
        seen["person"] = person
        return "call-jiwoo-20261014-210000"

    from voice.language import call_language
    rooms = asyncio.run(call_due(tmp_path / "workspace", prepare=prepare, place=place, person="jiwoo",
                                 language=call_language("ko")))
    assert rooms == ["call-jiwoo-20261014-210000"] and seen == {"language": "ko", "person": "jiwoo"}


# ---------- recording ----------

def test_a_call_can_skip_keeping_its_audio(tmp_path):
    rec = CallRecorder(tmp_path, "call-x", keep_audio=False)
    rec.line("AI", "hello")
    rec.close()
    assert (rec.dir / "transcript.md").exists() and not (rec.dir / "audio").exists()
    kept = CallRecorder(tmp_path, "call-y")
    kept.close()
    assert (kept.dir / "audio").is_dir()


# ---------- the voice server and the loops between calls ----------

def test_a_call_is_answered_only_for_an_approved_person(tmp_path, monkeypatch):
    from voice.agent import person_for_call

    people = People(tmp_path / "data")
    people.add("matt")
    assert person_for_call({"person": "matt"}, people) == "matt"
    assert person_for_call({"person": "jiwoo"}, people) is None  # not approved
    assert person_for_call({"person": "../matt"}, people) is None
    assert person_for_call({}, people) is None  # no person and no dev person
    monkeypatch.setenv("BEE_CALL_DEV_PERSON", "matt")
    assert person_for_call({}, people) == "matt"  # a console call or a test caller on the Mac
    assert person_for_call({"person": "jiwoo"}, people) is None  # the dev person never stands in for someone else


def test_one_person_s_loops_stop_together_and_none_outlive_the_supervisor(tmp_path, monkeypatch):
    from voice import outbound

    people = People(tmp_path / "data")
    people.add("matt")
    people.change_settings("matt", {"bee": True}, allowed=("bee",))
    alive = {"offers": 0, "meta": 0}

    async def forever(kind):
        alive[kind] += 1
        try:
            await asyncio.Event().wait()
        finally:
            alive[kind] -= 1

    async def offers(*args, **kwargs):
        await forever("offers")

    async def meta(*args, **kwargs):
        await forever("meta")

    class BrokenWatcher:  # the Bee stream cannot start, every time
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            raise OSError("bee: command not found")

    monkeypatch.setattr(outbound, "serve_offers", offers)
    monkeypatch.setattr(outbound, "serve_meta", meta)
    monkeypatch.setattr(outbound, "BeeWatcher", BrokenWatcher)
    monkeypatch.setattr(outbound, "PEOPLE_CHECK_S", 0.01)

    async def main():
        supervisor = asyncio.create_task(outbound.serve_people(people))
        most = 0
        for _ in range(50):  # about 50 restarts of matt's loops
            await asyncio.sleep(0.01)
            most = max(most, alive["offers"], alive["meta"])
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        return most

    assert asyncio.run(main()) <= 1  # never a second set beside the first
    assert alive == {"offers": 0, "meta": 0}  # nothing left once the supervisor stopped


def test_moving_an_old_workspace_keeps_the_worker_harness_for_everyone(tmp_path, monkeypatch, capsys):
    from voice import people as people_module

    old = tmp_path / "workspace"
    (old / "state").mkdir(parents=True)
    (old / "state" / "harness.json").write_text('{"harnessArn": "arn:aws:bedrock-agentcore:x"}')
    (old / "2026-10-14" / "2100").mkdir(parents=True)
    people_module.main(["--data", str(tmp_path / "data"), "add", "matt"])
    people_module.main(["--data", str(tmp_path / "data"), "move", str(old), "--to", "matt"])
    assert json.loads((tmp_path / "data" / "harness.json").read_text())["harnessArn"].endswith(":x")
    assert (tmp_path / "data" / "people" / "matt" / "2026-10-14" / "2100").is_dir()
    assert "Bee is not connected for matt yet" in capsys.readouterr().out


def test_the_operator_can_mint_a_sign_in_for_an_approved_person_only(tmp_path, capsys):
    from voice import people as people_module

    data = tmp_path / "data"
    people_module.main(["--data", str(data), "add", "jiwoo"])
    capsys.readouterr()
    people_module.main(["--data", str(data), "session", "jiwoo"])
    token = capsys.readouterr().out.strip()
    assert People(data).who(token) == "jiwoo"
    with pytest.raises(SystemExit, match="not approved"):
        people_module.main(["--data", str(data), "session", "stranger"])
