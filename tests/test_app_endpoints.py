"""The token server's answers for the app (contract/app-contract.md): every body checked against contract/schemas/,
versions with ETag and If-None-Match, and one person's calls never reaching another. The person's data is the
fixture workspace of tests/test_call_view.py; their clock is fixed with the test clock file (voice/people.py)."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.contract import check_body
from tests.helpers import as_sent, signed_in
from voice import token_server

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
EXAMPLES = Path(__file__).parent.parent / "contract" / "examples"
MAIN = "call-minseung-20261015-012301"


@pytest.fixture
def minseung(tmp_path, monkeypatch):
    """Minseung, signed in, with the fixture calls and offers, at 15:00 on 2026-10-15 on their clock."""
    me = signed_in(tmp_path, "minseung")
    shutil.copytree(FIXTURES, me.workspace, dirs_exist_ok=True)
    me.people.change_settings("minseung", {"language": "ko", "call_time": "20:30"})
    me.people.change_settings("minseung", {"bee": True, "daily_budget_usd": 10.0}, allowed=("bee", "daily_budget_usd"))
    clock = tmp_path / "clock"
    clock.write_text("2026-10-15T15:00:00+09:00")
    monkeypatch.setenv("BEE_CALL_CLOCK_FILE", str(clock))
    return me


def run(people, headers, requests):
    """Each (method, path, extra headers, json body) in turn, the path sent exactly as written: (status, headers,
    body bytes) for each."""
    async def main():
        app = token_server.make_app("wss://example", "APIkey", "s" * 32, people, room_exists=None)
        out = []
        async with TestClient(TestServer(app), headers=headers) as client:
            for method, path, extra, body in requests:
                response = await client.session.request(method, as_sent(client, path), headers=extra or {}, json=body)
                out.append((response.status, response.headers, await response.read()))
        return out

    return asyncio.run(main())


def get(me, *paths, headers=None):
    return run(me.people, me.headers, [("GET", path, headers, None) for path in paths])


def write_example(name: str, body: dict) -> None:
    (EXAMPLES / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


# ---------- calls ----------

def test_the_call_list_and_a_call_carry_their_version_as_etag_and_answer_304_when_unchanged(minseung):
    (listed, headers, body), (view, view_headers, view_body) = get(minseung, "/calls?limit=6", f"/calls/{MAIN}")
    assert (listed, view) == (200, 200)
    calls, call = json.loads(body), json.loads(view_body)
    check_body("calls", calls)
    check_body("call-view", call)
    assert headers["ETag"] == f'"{calls["version"]}"' and view_headers["ETag"] == f'"{call["version"]}"'
    assert headers["Cache-Control"] == "no-store"
    assert [c["call_id"] for c in calls["calls"]][:2] == ["call-minseung-20261015-142209", MAIN]
    (again, again_headers, again_body), (weak, _, _), (other, _, _) = run(minseung.people, minseung.headers, [
        ("GET", "/calls?limit=6", {"If-None-Match": f'"{calls["version"]}"'}, None),
        ("GET", f"/calls/{MAIN}", {"If-None-Match": f'"0000000000000000", W/"{call["version"]}"'}, None),
        ("GET", f"/calls/{MAIN}", {"If-None-Match": '"0000000000000000"'}, None)])
    assert (again, again_body, again_headers["ETag"]) == (304, b"", f'"{calls["version"]}"')
    assert (weak, other) == (304, 200)


def test_the_list_pages_and_refuses_a_bad_limit_or_cursor(minseung):
    (_, _, first), = get(minseung, "/calls?limit=6")
    before = json.loads(first)["before"]
    (status, _, body), *bad = get(minseung, f"/calls?limit=6&before={before}", "/calls?limit=0", "/calls?limit=51",
                                  "/calls?limit=ten", "/calls?before=nonsense")
    page = json.loads(body)
    check_body("calls", page)
    assert status == 200 and [c["call_id"] for c in page["calls"]] == ["call-20261011-193004", "fake-20261010-210512"]
    assert page["more"] is False and [s for s, _, _ in bad] == [400] * 4


def test_a_call_that_is_not_this_person_s_has_no_record_or_is_malformed_is_404(minseung):
    other = signed_in(minseung.workspace.parent.parent.parent, "jiwoo")
    paths = [f"/calls/{MAIN}", "/calls/call-minseung-20261014-083100", "/calls/nothing-here", "/calls/..%2Fx",
             f"/calls/{MAIN}/files/jobs/01/report.md"]
    assert [s for s, _, _ in run(other.people, other.headers, [("GET", p, None, None) for p in paths])] == [404] * 5
    (listed, _, body), = run(other.people, other.headers, [("GET", "/calls", None, None)])
    assert (listed, json.loads(body)["calls"]) == (200, [])
    assert [s for s, _, _ in get(minseung, "/calls/call-minseung-20261014-083100", "/calls/decline")] == [404, 404]


def test_a_call_s_files_are_served_by_the_record_server_s_rule(minseung):
    """Contract 2.6: text as plain text, charts as PNG (light and dark) and SVG (sandboxed), never a private part or a
    path out of the call's folder (403), 404 for anything else not there."""
    folder = minseung.workspace / "2026-10-15" / "0123"
    for private in ("audio", "inputs"):
        (folder / private).mkdir()
        (folder / private / "secret.json").write_text("{}")
    (folder / "model-log.jsonl").write_text("{}\n")
    (minseung.workspace / "secret.txt").write_text("outside the call")
    base = f"/calls/{MAIN}/files"
    found = get(minseung, f"{base}/jobs/01/report.md", f"{base}/jobs/01/comparison.csv", f"{base}/jobs/01/price-chart.png",
                f"{base}/jobs/01/price-chart.svg", f"{base}/jobs/01/price-chart-dark.png", f"{base}/audio/secret.json",
                f"{base}/INPUTS/secret.json", f"{base}/model-log.jsonl", f"{base}/jobs/01/nothing.md")
    statuses = [s for s, _, _ in found]
    assert statuses == [200, 200, 200, 200, 200, 403, 403, 403, 404]
    # Paths that try to leave by dot segments, as written or percent-encoded, reach the server as sent (the test
    # client would resolve them first) and meet its rule.
    leaving = get(minseung, f"{base}/jobs/../audio/secret.json", f"{base}/jobs/%2E%2E/audio/secret.json",
                  f"{base}/%2E%2E/%2E%2E/secret.txt", f"{base}/jobs/01/..%2F..%2F..%2F..%2Fsecret.txt")
    assert [(s, body) for s, _, body in leaving] == [(403, b"path not allowed\n")] * 4
    types = [h.get("Content-Type", "") for _, h, _ in found[:5]]
    assert types[0].startswith("text/plain") and "charset=utf-8" in types[0] and types[1].startswith("text/plain")
    assert types[2] == types[4] == "image/png" and types[3].startswith("image/svg+xml")
    assert all(h["Content-Security-Policy"] == "sandbox" and h["X-Content-Type-Options"] == "nosniff"
               for _, h, _ in found[:5])
    assert found[0][2].startswith("# 코딩용".encode())


# ---------- offers ----------

def test_offers_carry_their_version_and_say_what_the_person_can_do(minseung):
    """Contract section 4, with the example files written from the server's answers."""
    (status, headers, body), = get(minseung, "/offers")
    offers = json.loads(body)
    check_body("offers", offers)
    assert status == 200 and headers["ETag"] == f'"{offers["version"]}"'
    assert [(o["id"], o["state"], o["created_at"], o["suggested_at"], o["call_at"]) for o in offers["offers"]] == [
        ("20261015-142731-7c2e", "booked", "2026-10-15T14:27:31+09:00", None, "2026-10-15T19:00:00+09:00"),
        ("20261015-144102-a91f", "asked", "2026-10-15T14:41:02+09:00", "2026-10-15T20:30:00+09:00", None)]
    assert offers["offers"][1]["suggested_epoch"] and offers["offers"][1]["kind"] == "conversation"  # pre-D builds
    write_example("offers", {"offers": [{k: o[k] for k in ("id", "state", "topic", "created_at", "suggested_at",
                                                            "call_at")} for o in offers["offers"]],
                             "version": offers["version"]})
    (unchanged, _, _), (answered, _, answer) = run(minseung.people, minseung.headers, [
        ("GET", "/offers", {"If-None-Match": headers["ETag"]}, None),
        ("POST", "/offers/20261015-144102-a91f", None, {"choice": "later"})])
    answer = json.loads(answer)
    check_body("offer-answer", answer)
    assert (unchanged, answered, answer["offer"]["state"], answer["offer"]["call_at"]) == (
        304, 200, "booked", "2026-10-15T20:30:00+09:00")
    write_example("offer-answer", {"offer": {k: answer["offer"][k] for k in ("id", "state", "topic", "created_at",
                                                                             "suggested_at", "call_at")}})
    (after, after_headers, after_body), = get(minseung, "/offers", headers={"If-None-Match": headers["ETag"]})
    assert after == 200 and json.loads(after_body)["version"] != offers["version"]


def test_an_offer_without_a_topic_shows_the_notification_s_line_and_now_reads_as_calling(minseung):
    from beelink.planner import Plan
    from voice.offers import OfferStore

    store = OfferStore(minseung.workspace)
    for offer in store.all():
        store.answer(offer.id, "decline", minseung.person.now())
    plan = Plan(call=True, reason="A new voice note.", inbox_dir=None, conversation_ids=[77], topic=None,
                kind="voice note")
    asked = store.ask(plan, minseung.person.now(), at="20:30")
    (_, _, body), = get(minseung, "/offers")
    (offer,) = json.loads(body)["offers"]
    assert (offer["topic"], offer["state"]) == ("새 음성 메모 이야기", "asked")
    store.answer(asked.id, "now", minseung.person.now())
    (_, _, body), = get(minseung, "/offers")
    (offer,) = json.loads(body)["offers"]
    check_body("offers", json.loads(body))
    assert offer["state"] == "calling" and offer["call_at"]


# ---------- live updates ----------

LIVE = "call-minseung-20261015-145000"


def quiet_fixtures(me) -> None:
    """The fixture calls' files as last written an hour ago, so none of them counts as recent."""
    import os
    import time

    old = time.time() - 3600
    for path in me.workspace.rglob("*"):
        os.utime(path, (old, old))


def live_call(me) -> Path:
    folder = me.workspace / "2026-10-15" / "1450"
    folder.mkdir()
    lines = [{"t": 0.0, "type": "call_started", "room": LIVE, "started_at": "2026-10-15T14:50:00+09:00"},
             {"t": 0.01, "type": "call", "direction": "inbound", "topic": None, "reason": None},
             {"t": 3.0, "type": "message", "role": "assistant", "text": "네, Bee Call이에요.", "interrupted": False}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
    return folder


async def changed_events(response, seconds: float) -> list[dict]:
    """The data of each `changed` event read from an open stream within `seconds`."""
    loop = asyncio.get_running_loop()
    end, block, found = loop.time() + seconds, [], []
    while loop.time() < end:
        try:
            line = await asyncio.wait_for(response.content.readline(), end - loop.time())
        except asyncio.TimeoutError:
            break
        line = line.decode().rstrip("\n")
        if line:
            block.append(line)
            continue
        fields = dict(part.split(": ", 1) for part in block if ": " in part and not part.startswith(":"))
        if fields.get("event") == "changed":
            found.append(json.loads(fields["data"]))
        block = []
    return found


def test_changes_announces_every_watched_path_on_connect_then_each_new_version(minseung, monkeypatch):
    """Contract section 3: on connect, /calls, /offers and each call live or ended in the last ten minutes; then a
    `changed` whenever a version changes, with the version GET answers."""
    monkeypatch.setattr(token_server, "CHANGES_POLL_S", 0.05)
    quiet_fixtures(minseung)
    folder = live_call(minseung)

    async def main():
        app = token_server.make_app("wss://example", "APIkey", "s" * 32, minseung.people, room_exists=None)
        async with TestClient(TestServer(app), headers=minseung.headers) as client:
            stream = await client.get("/changes")
            first = await changed_events(stream, 0.5)
            with (folder / "events.jsonl").open("a") as f:
                f.write(json.dumps({"t": 6.0, "type": "user_transcript", "text": "오늘 할 일 정리해 줘."},
                                   ensure_ascii=False) + "\n")
            later = await changed_events(stream, 0.5)
            view = await (await client.get(f"/calls/{LIVE}")).json()
            calls = await (await client.get("/calls")).json()
            return stream.status, stream.headers, first, later, view, calls

    status, headers, first, later, view, calls = asyncio.run(main())
    assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
    for event in first + later:
        check_body("change", event)
    assert {e["path"] for e in first} == {"/calls", "/offers", f"/calls/{LIVE}"} and len(first) == 3
    assert {e["path"]: e["version"] for e in later} == {f"/calls/{LIVE}": view["version"],
                                                         "/calls": calls["version"]}


def test_changes_announces_what_the_clock_alone_changes(minseung, monkeypatch):
    """A live call that goes quiet turns dropped, and a ring nobody answered turns missed, without any file changing:
    /changes rebuilds them every CHANGES_REBUILD_S."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from voice import call_view

    monkeypatch.setattr(token_server, "CHANGES_POLL_S", 0.05)
    monkeypatch.setattr(token_server, "CHANGES_REBUILD_S", 0.2)
    monkeypatch.setattr(call_view, "LIVE_S", 1)
    quiet_fixtures(minseung)
    live_call(minseung)
    now = minseung.person.now()
    ring = {"at": (now - timedelta(seconds=59)).isoformat(timespec="seconds"), "room": "call-minseung-20261015-145959",
            "expires_at": (now + timedelta(seconds=1)).isoformat(timespec="seconds"), "topic": "점심 약속", "kind": None}
    with (minseung.workspace / "state" / "outbound.jsonl").open("a") as f:
        f.write(json.dumps(ring, ensure_ascii=False) + "\n")

    async def main():
        app = token_server.make_app("wss://example", "APIkey", "s" * 32, minseung.people, room_exists=None)
        async with TestClient(TestServer(app), headers=minseung.headers) as client:
            stream = await client.get("/changes")
            first = await changed_events(stream, 0.3)
            later = await changed_events(stream, 2.0)
            view = await (await client.get(f"/calls/{LIVE}")).json()
            calls = await (await client.get("/calls")).json()
            return first, later, view, calls

    first, later, view, calls = asyncio.run(main())
    assert f"/calls/{LIVE}" in {e["path"] for e in first}
    assert view["call"]["state"] == "dropped"
    assert calls["calls"][0]["call_id"] == "call-minseung-20261015-145959" and calls["calls"][0]["state"] == "missed"
    final = {e["path"]: e["version"] for e in later}
    assert final[f"/calls/{LIVE}"] == view["version"] and final["/calls"] == calls["version"]


def test_a_quiet_change_stream_sends_keepalives(minseung, monkeypatch):
    """The app treats 40 s of silence as a dead stream; with nothing changing, a comment goes out every 15 s."""
    monkeypatch.setattr(token_server, "CHANGES_POLL_S", 0.02)
    monkeypatch.setattr(token_server, "KEEPALIVE_SECONDS", 0.1)
    quiet_fixtures(minseung)

    async def main():
        app = token_server.make_app("wss://example", "APIkey", "s" * 32, minseung.people, room_exists=None)
        async with TestClient(TestServer(app), headers=minseung.headers) as client:
            stream = await client.get("/changes")
            lines = []
            while b": keepalive\n" not in lines:
                lines.append(await asyncio.wait_for(stream.content.readline(), 2))
            return lines

    assert b": keepalive\n" in asyncio.run(main())


# ---------- voices and settings ----------

def test_voices_list_the_call_language_s_catalog_and_samples_need_no_sign_in(minseung):
    """Contract section 5: 1 to 12 voices per language, the default first; samples are the same for everyone, so
    they need no sign-in and may be cached for good (their names carry their content's hash)."""
    (ko, _, ko_body), (en, _, en_body), *bad = get(minseung, "/voices?language=ko", "/voices?language=en",
                                                   "/voices", "/voices?language=fr")
    korean, english = json.loads(ko_body), json.loads(en_body)
    check_body("voices", korean)
    check_body("voices", english)
    assert (ko, en, [s for s, _, _ in bad]) == (200, 200, [400, 400])
    assert korean["voices"][0] == {"id": "ko-sian", "name": "Sian", "character": {"ko": "차분함", "en": "Calm"},
                                   "sample_url": korean["voices"][0]["sample_url"], "default": True}
    assert english["voices"][0] == {"id": "en-zara", "name": "Zara", "character": english["voices"][0]["character"],
                                    "sample_url": english["voices"][0]["sample_url"], "default": True}
    write_example("voices", korean)
    write_example("voices-en", english)  # the app's Debug build reads it for the English list
    sample = korean["voices"][1]["sample_url"]
    found = run(minseung.people, {}, [("GET", sample, None, None), ("GET", "/voices/samples/ko-han-00000000.mp3", None,
                                                                     None), ("GET", "/voices?language=ko", None, None)])
    (status, headers, body), (missing, _, _), (listing, _, _) = found
    assert (status, headers["Content-Type"], body[:3]) == (200, "audio/mpeg", b"ID3")
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert (missing, listing) == (404, 401)  # the list is the person's; a sample is anyone's


def test_me_and_settings_answer_the_contract_s_settings_and_the_voice_follows_the_language(minseung):
    """Contract section 6: GET /me and PUT /settings bodies, a voice of the call language, and 400 for another."""
    found = run(minseung.people, minseung.headers, [
        ("PUT", "/settings", None, {"voice": "ko-yooni"}),
        ("PUT", "/settings", None, {"voice": "en-zara"}),
        ("GET", "/me", None, None),
        ("PUT", "/settings", None, {"language": "en"}),
        ("PUT", "/settings", None, {"language": "ko", "voice": "ko-anna"})])
    (picked, _, picked_body), (wrong, _, wrong_body), (me, _, me_body), (_, _, english), (_, _, both) = found
    check_body("settings", json.loads(picked_body))
    check_body("me", json.loads(me_body))
    assert (picked, wrong, me) == (200, 400, 200) and b"not one of the ko voices" in wrong_body
    assert json.loads(me_body)["settings"]["voice"] == "ko-yooni" and "tts" not in json.loads(me_body)["settings"]
    # Models Minseung never picked: /me sends each role's default, and settings.json keeps "no pick" (null).
    shown = json.loads(me_body)["settings"]
    assert (shown["call_model"], shown["worker_model"]) == ("haiku", "sonnet")
    stored = json.loads((minseung.people.folder("minseung") / "settings.json").read_text())
    assert (stored["call_model"], stored["worker_model"]) == (None, None)
    assert json.loads(english)["settings"]["voice"] is None and json.loads(both)["settings"]["voice"] == "ko-anna"
    write_example("settings", json.loads(picked_body))
    write_example("me", json.loads(me_body))


# ---------- models ----------

def test_models_list_a_person_facing_role_s_choices_and_settings_take_only_those(minseung):
    """Contract section 5b and 6: GET /models?role=call|worker, default first; PUT /settings takes call_model and
    worker_model only from their role's ids; the server's own roles are not the person's to pick."""
    found = run(minseung.people, minseung.headers, [
        ("GET", "/models?role=call", None, None), ("GET", "/models?role=worker", None, None),
        ("GET", "/models?role=meta", None, None), ("GET", "/models", None, None),
        ("PUT", "/settings", None, {"worker_model": "glm", "call_model": "sonnet"}),
        ("PUT", "/settings", None, {"worker_model": "haiku"}),
        ("PUT", "/settings", None, {"call_model": "glm"})])
    (call, _, call_body), (worker, _, worker_body), (meta, _, _), (missing, _, _) = found[:4]
    (picked, _, picked_body), (wrong_worker, _, _), (wrong_call, _, _) = found[4:]
    for name, body in (("models-call", call_body), ("models-worker", worker_body)):
        check_body(name, json.loads(body))
        write_example(name, json.loads(body))
    assert (call, worker, meta, missing) == (200, 200, 400, 400)
    assert [(m["id"], m["default"]) for m in json.loads(worker_body)["models"]] == [("sonnet", True), ("glm", False)]
    settings = json.loads(picked_body)["settings"]
    check_body("settings", json.loads(picked_body))
    assert (picked, settings["worker_model"], settings["call_model"]) == (200, "glm", "sonnet")
    assert (wrong_worker, wrong_call) == (400, 400)
