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
MAIN = "call-minseung-20261015-012301"


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
    """Contract 2.6: text as plain text, charts as PNG and SVG (sandboxed), never a private part or a path out of the
    call's folder (403), 404 for anything else not there."""
    folder = minseung.workspace / "2026-10-15" / "0123"
    for private in ("audio", "inputs"):
        (folder / private).mkdir()
        (folder / private / "secret.json").write_text("{}")
    (folder / "model-log.jsonl").write_text("{}\n")
    base = f"/calls/{MAIN}/files"
    found = get(minseung, f"{base}/jobs/01/report.md", f"{base}/jobs/01/comparison.csv", f"{base}/jobs/01/price-chart.png",
                f"{base}/jobs/01/price-chart.svg", f"{base}/audio/secret.json", f"{base}/INPUTS/secret.json",
                f"{base}/model-log.jsonl", f"{base}/jobs/../audio/secret.json", f"{base}/jobs/01/nothing.md")
    statuses = [s for s, _, _ in found]
    assert statuses == [200, 200, 200, 200, 403, 403, 403, 403, 404]
    types = [h.get("Content-Type", "") for _, h, _ in found[:4]]
    assert types[0].startswith("text/plain") and "charset=utf-8" in types[0] and types[1].startswith("text/plain")
    assert types[2] == "image/png" and types[3].startswith("image/svg+xml")
    assert all(h["Content-Security-Policy"] == "sandbox" and h["X-Content-Type-Options"] == "nosniff"
               for _, h, _ in found[:4])
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
