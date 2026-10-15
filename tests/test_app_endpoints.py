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
from tests.helpers import signed_in
from voice import token_server

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
EXAMPLES = Path(__file__).parent.parent / "contract" / "examples"


@pytest.fixture
def minseung(tmp_path, monkeypatch):
    """Minseung, signed in, with the fixture calls and offers, at 15:00 on 2026-10-15 on their clock."""
    me = signed_in(tmp_path, "minseung")
    shutil.copytree(FIXTURES, me.workspace, dirs_exist_ok=True)
    me.people.change_settings("minseung", {"language": "ko", "call_time": "20:30"})
    clock = tmp_path / "clock"
    clock.write_text("2026-10-15T15:00:00+09:00")
    monkeypatch.setenv("BEE_CALL_CLOCK_FILE", str(clock))
    return me


def run(people, headers, requests):
    """Each (method, path, extra headers, json body) in turn: (status, headers, body bytes) for each."""
    async def main():
        app = token_server.make_app("wss://example", "APIkey", "s" * 32, people, room_exists=None)
        out = []
        async with TestClient(TestServer(app), headers=headers) as client:
            for method, path, extra, body in requests:
                response = await client.request(method, path, headers=extra or {}, json=body)
                out.append((response.status, response.headers, await response.read()))
        return out

    return asyncio.run(main())


def get(me, *paths, headers=None):
    return run(me.people, me.headers, [("GET", path, headers, None) for path in paths])


def write_example(name: str, body: dict) -> None:
    (EXAMPLES / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


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
