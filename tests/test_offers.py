"""Asking before calling: offers, the user's answers, the notification, and the calls placed after a yes.

No network: APNs, the prep and placing a call are fakes here.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from beelink.planner import RulePlanner
from voice import outbound, voip_push
from voice.language import call_language
from voice.offers import LATE_BY, OfferStore, agreed, call_time, notification, plan_of
from voice.outbound import call_due, handle, saved_trigger
from voice.prep import Prep
from tests.test_outbound import inbox

NOW = datetime(2026, 10, 14, 18, 30)


def asked(tmp_path, now=NOW):
    plan = RulePlanner(tmp_path / "workspace").decide(saved_trigger(inbox(tmp_path)))
    return OfferStore(tmp_path / "workspace").ask(plan, now), plan


def test_an_offer_suggests_the_evening_call_time_today_or_tomorrow():
    assert call_time(NOW) == datetime(2026, 10, 14, 21, 0)
    assert call_time(datetime(2026, 10, 14, 20, 55)) == datetime(2026, 10, 15, 21, 0)  # too close: tomorrow
    assert call_time(datetime(2026, 10, 14, 22, 0)) == datetime(2026, 10, 15, 21, 0)


def test_a_call_decision_becomes_a_question_with_the_plan_behind_it(tmp_path):
    offer, plan = asked(tmp_path)
    assert (offer.state, offer.topic, offer.kind) == ("asked", "Planned a Saturday hike with Dana", "conversation")
    assert offer.suggested_at == offer.expires_at == "2026-10-14T21:00:00"
    again = plan_of(offer)
    assert (again.inbox_dir, again.conversation_ids, again.reason) == (plan.inbox_dir, plan.conversation_ids,
                                                                       plan.reason)
    english = notification(offer, NOW, call_language("en"))
    assert english["aps"]["alert"]["body"] == \
        "About “Planned a Saturday hike with Dana”: free for a 5-minute call at 9:00 PM today?"
    assert (english["aps"]["category"], english["offer"]) == ("CALL_OFFER", offer.id)
    korean = notification(offer, NOW, call_language("ko"))
    assert korean["aps"]["alert"]["body"] == "“Planned a Saturday hike with Dana” 이야기, 오늘 밤 9시에 5분 통화 괜찮으세요?"
    assert korean["aps"]["category"] == "CALL_OFFER_KO"


def test_the_users_answer_decides_if_and_when_the_ai_calls(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    now_offer, _ = asked(tmp_path)
    answered = store.answer(now_offer.id, "now", NOW + timedelta(minutes=1))
    assert (answered.state, answered.call_at) == ("now", "2026-10-14T18:31:00")
    with pytest.raises(ValueError):  # one answer only
        store.answer(now_offer.id, "decline", NOW + timedelta(minutes=2))
    later = store.answer(store.ask(plan_of(now_offer), NOW).id, "later", NOW)
    assert (later.state, later.call_at) == ("booked", "2026-10-14T21:00:00")
    assert store.answer(later.id, "decline", NOW).state == "declined"  # a booked call can be called off
    no = store.answer(store.ask(plan_of(now_offer), NOW).id, "decline", NOW)
    assert no.state == "declined"
    for bad in ("../../x", "20261014-183000-zzzz"):
        with pytest.raises(KeyError):
            store.answer(bad, "now", NOW)
    with pytest.raises(ValueError):
        store.answer(no.id, "maybe", NOW)


def test_unanswered_and_missed_offers_close_and_only_yeses_come_due(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, plan = asked(tmp_path)
    assert [o.id for o in store.open(NOW)] == [offer.id] and store.due(NOW) == []
    assert store.open(datetime(2026, 10, 14, 21, 0)) == [] and store.get(offer.id).state == "expired"
    booked = store.answer(store.ask(plan, NOW).id, "later", NOW)
    assert [o.id for o in store.due(datetime(2026, 10, 14, 21, 0))] == [booked.id]
    store.expire(datetime(2026, 10, 14, 21, 0) + LATE_BY + timedelta(seconds=1))
    assert store.get(booked.id).state == "expired"


def test_how_the_user_agreed_reaches_the_opening(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, plan = asked(tmp_path)
    assert agreed(store.answer(offer.id, "now", NOW)) == 'the user tapped "Call me now" on your notification at 18:30'
    later = store.answer(store.ask(plan, NOW).id, "later", NOW + timedelta(minutes=5))
    assert agreed(later) == "at 18:35 the user accepted the time your notification suggested"
    booked = store.book("check how the interview went", datetime(2026, 10, 15, 8, 0), NOW, "2026-10-14/1820")
    assert agreed(booked) == "the user asked for this call back during an earlier call, at 18:30"
    assert (booked.state, booked.source, booked.continue_from) == ("booked", "call", "2026-10-14/1820")


def test_the_watcher_asks_instead_of_calling(tmp_path, monkeypatch):
    sent, placed = [], []

    async def fake_alert(payload, workspace, **kw):
        sent.append((payload, kw))
        return "apns-1"

    async def fake_place(*a, **kw):
        placed.append(a)
        return "room"

    async def fake_prepare(plan, **kw):
        return Prep(note="n", opening="o")

    monkeypatch.setattr(voip_push, "alert", fake_alert)
    monkeypatch.setattr(outbound, "place_call", fake_place)
    monkeypatch.setattr(outbound, "prepare_outbound", fake_prepare)
    planner = RulePlanner(tmp_path / "workspace")
    offer_id = asyncio.run(handle(saved_trigger(inbox(tmp_path)), planner, dry_run=False))
    assert placed == [] and OfferStore(tmp_path / "workspace").get(offer_id).state == "asked"
    (payload, kw), = sent
    assert payload["offer"] == offer_id and kw["collapse_id"] == offer_id
    # The planner counts the question like a call: no second question right away.
    again = planner.decide(saved_trigger(inbox(tmp_path / "second")))
    assert not again.call and "minutes apart" in again.reason
    # --direct (tests only) still calls at once.
    assert asyncio.run(handle(saved_trigger(inbox(tmp_path / "third")), RulePlanner(tmp_path / "w3"), False,
                              direct=True)) == "room"


def test_a_yes_is_called_with_how_the_user_agreed(tmp_path):
    workspace = tmp_path / "workspace"
    store = OfferStore(workspace)
    offer, _ = asked(tmp_path)
    store.answer(offer.id, "now", datetime.now())
    prepared, placed = [], []

    async def fake_prepare(plan, extra=None, deadline_s=None):
        prepared.append((plan.topic, extra, deadline_s))
        return Prep(note="note", opening="Hi, calling as you asked.")

    async def fake_place(plan, ws, prep=None, continue_from=None, agreed=None):
        placed.append((plan.conversation_ids, prep["opening"], agreed))
        return "call-1"

    assert asyncio.run(call_due(workspace, prepare=fake_prepare, place=fake_place)) == ["call-1"]
    (topic, extra, deadline), = prepared
    assert topic == "Planned a Saturday hike with Dana" and "Call me now" in extra["agreed"] and deadline < 20
    assert placed[0][1] == "Hi, calling as you asked." and store.get(offer.id).state == "called"
    assert asyncio.run(call_due(workspace, prepare=fake_prepare, place=fake_place)) == []  # called once


def test_a_call_that_cannot_be_placed_is_marked_failed(tmp_path):
    workspace = tmp_path / "workspace"
    store = OfferStore(workspace)
    booked = store.book("the interview", datetime.now() - timedelta(seconds=1), datetime.now())

    async def fake_prepare(plan, extra=None, deadline_s=None):
        return Prep()

    async def broken_place(*a, **kw):
        raise RuntimeError("LiveKit is down")

    assert asyncio.run(call_due(workspace, prepare=fake_prepare, place=broken_place)) == []
    assert store.get(booked.id).state == "failed" and "LiveKit is down" in store.get(booked.id).error


def test_the_app_answers_through_the_token_server(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from voice.token_server import make_app

    offer, _ = asked(tmp_path, now=datetime.now())

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, tmp_path / "workspace", room_exists=None)
        async with TestClient(TestServer(app)) as client:
            token = await client.post("/push", json={"token": "ab" * 32, "environment": "development"})
            listed = await (await client.get("/offers")).json()
            yes = await client.post(f"/offers/{offer.id}", json={"choice": "later"})
            twice = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            unknown = await client.post("/offers/20261014-183000-abcd", json={"choice": "now"})
            bad = await client.post(f"/offers/{offer.id}", data="no json")
            return token.status, listed, yes.status, await yes.json(), twice.status, unknown.status, bad.status

    token, listed, yes, body, twice, unknown, bad = asyncio.run(main())
    assert token == 204 and voip_push.load_device_token(tmp_path / "workspace", "alert")["token"] == "ab" * 32
    assert voip_push.load_device_token(tmp_path / "workspace") is None  # the VoIP token is a different one
    assert [o["id"] for o in listed["offers"]] == [offer.id] and listed["offers"][0]["suggested_epoch"]
    assert (yes, body["offer"]["state"], twice, unknown, bad) == (200, "booked", 409, 404, 400)


def test_the_alert_push_goes_to_the_app_with_its_own_token(tmp_path):
    import httpx
    from tests.test_voip_push import KEY  # noqa: F401  (the made-up signing key)
    from cryptography.hazmat.primitives import serialization

    key = tmp_path / "AuthKey_TEST123456.p8"
    key.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    config = voip_push.ApnsConfig(key, "TEST123456", "TEAM123456")
    voip_push.save_device_token(tmp_path, "cd" * 32, "development", "alert")
    seen = []

    def apns(request):
        seen.append(request)
        return httpx.Response(200, headers={"apns-id": "A-1"})

    payload = {"aps": {"alert": {"title": "Bee Call", "body": "hi"}}, "offer": "x"}
    got = asyncio.run(voip_push.alert(payload, tmp_path, expires_epoch=1791900000, collapse_id="x", config=config,
                                      transport=httpx.MockTransport(apns)))
    (request,) = seen
    assert got == "A-1" and str(request.url).endswith("/3/device/" + "cd" * 32)
    assert request.headers["apns-topic"] == "com.leeminseung.beecall" and request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-collapse-id"] == "x" and json.loads(request.content) == payload
