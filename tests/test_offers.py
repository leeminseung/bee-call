"""Asking before calling: offers, the user's answers, the notification, and the calls placed after a yes.

No network: APNs, the prep and placing a call are fakes here.
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from beelink.planner import OUTBOUND_LOG, RulePlanner
from voice import outbound, voip_push
from voice.language import call_language
from voice.offers import LATE_BY, OfferStore, QuestionWaiting, agreed, call_time, notification, plan_of
from voice.outbound import call_due, handle, saved_trigger
from voice.prep import Prep
from tests.helpers import SEOUL, another_conversation, fake_livekit, next_question, placing, seoul_now, signed_in
from tests.test_outbound import inbox

NOW = datetime(2026, 10, 14, 18, 30, tzinfo=SEOUL)


def asked(tmp_path, now=NOW, workspace=None):
    workspace = workspace or tmp_path / "workspace"
    plan = RulePlanner(workspace, now=lambda: now).decide(saved_trigger(inbox(tmp_path)))
    return OfferStore(workspace).ask(plan, now), plan


def test_the_offer_hold_and_the_planner_read_past_lines_still_being_written(tmp_path):
    """Server review finding 13: a call's events.jsonl and the outbound log are appended to while they are read; a
    half-written last line made the 30-minute hold, the planner's last call and the busy check raise."""
    from beelink.planner import last_call
    from voice.outbound import last_call_end, notification_wait

    me = signed_in(tmp_path)
    now = datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL)
    folder = me.workspace / "2026-10-15" / "2040"
    folder.mkdir(parents=True)
    started = datetime(2026, 10, 15, 20, 40, tzinfo=SEOUL)
    (folder / "events.jsonl").write_text(
        json.dumps({"t": 0.0, "type": "call_started", "started_at": started.isoformat()}) + "\n"
        + json.dumps({"t": 300.0, "type": "call_ended"}) + "\n" + '{"t": 301.0, "type": "bee_wri')
    log = me.workspace / OUTBOUND_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"at": started.isoformat()}) + "\n" + '{"at": "2026-10-15T20:5')
    assert last_call_end(me.workspace) == started + timedelta(minutes=5)
    assert last_call(me.workspace) >= started
    assert notification_wait(me.person, now).startswith("the last call ended at 20:45")


def test_one_bee_conversation_leads_to_at_most_one_offer(tmp_path):
    """BC-014, at the one place every offer is made (OfferStore.ask): the rule path and the meta agent alike."""
    from voice.offers import AlreadyOffered

    offer, plan = asked(tmp_path)
    store = OfferStore(tmp_path / "workspace")
    store.answer(offer.id, "decline", NOW)
    with pytest.raises(AlreadyOffered):
        store.ask(plan, NOW + timedelta(hours=1))
    assert [o.id for o in store.all()] == [offer.id]


def test_an_offer_suggests_the_evening_call_time_today_or_tomorrow():
    assert call_time(NOW) == datetime(2026, 10, 14, 21, 0, tzinfo=SEOUL)
    assert call_time(datetime(2026, 10, 14, 20, 55, tzinfo=SEOUL)) == datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL)  # too close: tomorrow
    assert call_time(datetime(2026, 10, 14, 22, 0, tzinfo=SEOUL)) == datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL)


def test_an_offer_suggests_tomorrow_when_today_s_call_time_is_less_than_ten_minutes_away():
    """BC-010 at its boundary: ten minutes or more before the call time suggests today, less (or past) tomorrow."""
    at = datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL)
    for minutes_before, day in ((11, 15), (10, 15), (9, 16), (0, 16), (-5, 16)):
        now = at - timedelta(minutes=minutes_before)
        assert call_time(now) == at.replace(day=day), minutes_before


def test_an_operator_command_makes_an_offer_from_outside(tmp_path, capsys):
    """Outside test seam 1: offers came only from the meta agent (which needs its model) after a Bee change.
    `voice.offers ask` makes one for an approved person, under the same rules (one question at a time, one offer
    per Bee conversation); its notification goes out from voice/outbound.py's loop, like any held one."""
    from voice import offers as offers_module

    me = signed_in(tmp_path)
    data = str(me.people.root)
    offers_module.main(["--data", data, "ask", "matt", "--topic", "Planned a Saturday hike",
                        "--reason", "Dana asked about Saturday.", "--conversation", "77", "--at", "20:30"])
    made = json.loads(capsys.readouterr().out)
    offer = OfferStore(me.workspace).get(made["id"])
    assert (offer.state, offer.topic, offer.conversation_ids, offer.reason) == (
        "asked", "Planned a Saturday hike", [77], "Dana asked about Saturday.")
    assert offer.suggested_at.split("T")[1].startswith("20:30") and offer.notified_at is None
    assert [o.id for o in OfferStore(me.workspace).unsent(me.person.now())] == [made["id"]]
    with pytest.raises(SystemExit, match="not answered"):  # one question at a time
        offers_module.main(["--data", data, "ask", "matt", "--topic", "Another", "--reason", "r"])
    with pytest.raises(SystemExit, match="not approved"):
        offers_module.main(["--data", data, "ask", "ghost", "--topic", "t", "--reason", "r"])


def test_the_evening_call_time_has_one_default(tmp_path, monkeypatch):
    """Server review 19: 21:00 was written in the person's settings, in voice/offers.py and, with an undocumented
    CALL_TIME environment setting in front of it, in the meta agent's prompt. The person's setting is the rule;
    voice/people.py owns its default."""
    from meta.agent import context
    from voice.people import DEFAULT_CALL_TIME, Settings

    monkeypatch.setenv("CALL_TIME", "08:00")  # no longer read anywhere
    noon = datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL)
    assert DEFAULT_CALL_TIME == Settings().call_time == "21:00"
    assert call_time(noon) == noon.replace(hour=21)
    assert "The evening call time (the time an offer suggests, not a booking) is 21:00." in context(tmp_path, [], noon)


def test_a_call_decision_becomes_a_question_with_the_plan_behind_it(tmp_path):
    offer, plan = asked(tmp_path)
    assert (offer.state, offer.topic, offer.kind) == ("asked", "Planned a Saturday hike with Dana", "conversation")
    assert offer.suggested_at == offer.expires_at == "2026-10-14T21:00:00+09:00"
    again = plan_of(offer)
    assert (again.inbox_dir, again.conversation_ids, again.reason) == (plan.inbox_dir, plan.conversation_ids,
                                                                       plan.reason)
    english = notification(offer, NOW, call_language("en"))
    assert english["aps"]["alert"]["body"] == \
        "About “Planned a Saturday hike with Dana”: free for a quick call at 9:00 PM today?"
    assert (english["aps"]["category"], english["offer"]) == ("CALL_OFFER", offer.id)
    korean = notification(offer, NOW, call_language("ko"))
    assert korean["aps"]["alert"]["body"] == "“Planned a Saturday hike with Dana” 이야기, 오늘 밤 9시에 잠깐 통화 괜찮으세요?"
    assert korean["aps"]["category"] == "CALL_OFFER_KO"


def test_the_users_answer_decides_if_and_when_the_ai_calls(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    now_offer, _ = asked(tmp_path)
    answered = store.answer(now_offer.id, "now", NOW + timedelta(minutes=1))
    assert (answered.state, answered.call_at) == ("now", "2026-10-14T18:31:00+09:00")
    with pytest.raises(ValueError):  # one answer only
        store.answer(now_offer.id, "decline", NOW + timedelta(minutes=2))
    later = store.answer(store.ask(another_conversation(plan_of(now_offer), 1), NOW).id, "later", NOW)
    assert (later.state, later.call_at) == ("booked", "2026-10-14T21:00:00+09:00")
    assert store.answer(later.id, "decline", NOW).state == "declined"  # a booked call can be called off
    no = store.answer(store.ask(another_conversation(plan_of(now_offer), 2), NOW).id, "decline", NOW)
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
    assert store.open(datetime(2026, 10, 14, 21, 0, tzinfo=SEOUL)) == [] and store.get(offer.id).state == "expired"
    booked = store.answer(store.ask(another_conversation(plan), NOW).id, "later", NOW)
    assert [o.id for o in store.due(datetime(2026, 10, 14, 21, 0, tzinfo=SEOUL))] == [booked.id]
    store.expire(datetime(2026, 10, 14, 21, 0, tzinfo=SEOUL) + LATE_BY + timedelta(seconds=1))
    assert store.get(booked.id).state == "expired"


def test_how_the_user_agreed_reaches_the_opening(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, plan = asked(tmp_path)
    assert agreed(store.answer(offer.id, "now", NOW)) == 'the user tapped "Call me now" on your notification at 18:30'
    later = store.answer(store.ask(another_conversation(plan), NOW).id, "later", NOW + timedelta(minutes=5))
    assert agreed(later) == "at 18:35 the user accepted the time your notification suggested"
    booked = store.book("check how the interview went", datetime(2026, 10, 15, 8, 0, tzinfo=SEOUL), NOW, "2026-10-14/1820")
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

    monkeypatch.setattr(voip_push, "alert", fake_alert)
    monkeypatch.setattr(outbound, "place_call", fake_place)
    monkeypatch.setattr(outbound, "quiet", lambda now: False)  # the test may run at night
    me = signed_in(tmp_path)
    planner = RulePlanner(me.workspace, now=seoul_now)
    offer_id = asyncio.run(handle(saved_trigger(inbox(tmp_path)), planner, False, me.person))
    assert placed == [] and OfferStore(me.workspace).get(offer_id).state == "asked"
    (payload, kw), = sent
    assert payload["offer"] == offer_id and kw["collapse_id"] == offer_id
    # The planner counts the question like a call: no second question right away.
    again = planner.decide(saved_trigger(inbox(tmp_path / "second")))
    assert not again.call and "minutes apart" in again.reason


def test_a_yes_is_called_with_how_the_user_agreed(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)  # at a fixed time it would have closed
    store.answer(offer.id, "now", datetime.now(SEOUL))
    prepared, requests = [], []

    async def fake_prepare(plan, extra=None, deadline_s=None, **kw):
        prepared.append((plan.topic, extra, deadline_s))
        return Prep(note="note", opening="Hi, calling as you asked.")

    place = placing(requests)
    (room,) = asyncio.run(call_due(me.person, prepare=fake_prepare, place=place))
    (topic, extra, deadline), = prepared
    assert topic == "Planned a Saturday hike with Dana" and "Call me now" in extra["agreed"] and deadline < 20
    assert extra["last_calls"] is None  # no earlier call to carry (voice/continuation.py last_calls)
    meta = json.loads(requests[0].agents[0].metadata)
    assert meta["prep"]["opening"] == "Hi, calling as you asked." and meta["agreed"] == extra["agreed"]
    assert meta["conversation_ids"] == [5121] and store.get(offer.id).state == "called" and room == requests[0].name
    assert asyncio.run(call_due(me.person, prepare=fake_prepare, place=place)) == []  # called once


def test_a_call_that_cannot_be_placed_is_marked_failed(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    booked = store.book("the interview", datetime.now(SEOUL) - timedelta(seconds=1), datetime.now(SEOUL))

    async def fake_prepare(plan, extra=None, deadline_s=None, **kw):
        return Prep()

    async def broken_place(*a, **kw):
        raise RuntimeError("LiveKit is down")

    assert asyncio.run(call_due(me.person, prepare=fake_prepare, place=broken_place)) == []
    assert store.get(booked.id).state == "failed" and "LiveKit is down" in store.get(booked.id).error


def test_the_app_answers_through_the_token_server(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from voice.token_server import make_app

    me = signed_in(tmp_path)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            token = await client.post("/push", json={"token": "ab" * 32, "environment": "development"})
            listed = await (await client.get("/offers")).json()
            yes = await client.post(f"/offers/{offer.id}", json={"choice": "later"})
            twice = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            unknown = await client.post("/offers/20261014-183000-abcd", json={"choice": "now"})
            bad = await client.post(f"/offers/{offer.id}", data="no json")
            return token.status, listed, yes.status, await yes.json(), twice.status, unknown.status, bad.status

    token, listed, yes, body, twice, unknown, bad = asyncio.run(main())
    assert token == 204 and voip_push.load_device_token(me.person, "alert")["token"] == "ab" * 32
    assert voip_push.load_device_token(me.person) is None  # the VoIP token is a different one
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
    me = signed_in(tmp_path)
    voip_push.save_device_token(me.person, "cd" * 32, "development", "alert")
    seen = []

    def apns(request):
        seen.append(request)
        return httpx.Response(200, headers={"apns-id": "A-1"})

    payload = {"aps": {"alert": {"title": "Bee Call", "body": "hi"}}, "offer": "x"}
    got = asyncio.run(voip_push.alert(payload, me.person, expires_epoch=1791900000, collapse_id="x", config=config,
                                      transport=httpx.MockTransport(apns)))
    (request,) = seen
    assert got == "A-1" and str(request.url).endswith("/3/device/" + "cd" * 32)
    assert request.headers["apns-topic"] == "com.leeminseung.beecall" and request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-collapse-id"] == "x" and json.loads(request.content) == payload


def user_said(agent, *lines: str) -> None:
    """The user's turns in the call's conversation, as LiveKit keeps them."""
    chat = agent.chat_ctx.copy()
    for line in lines:
        chat.add_message(role="user", content=line)
    asyncio.run(agent.update_chat_ctx(chat))


def test_a_call_back_is_booked_only_with_the_user_s_own_words_asking_for_it(tmp_path):
    """Coordinator's review of 2026-10-15 (queued): nothing in code checked that the person asked for a call back, so
    a model could book one on its own. book_call now takes the user's own words asking for it or agreeing to it,
    which must be among what they said in this call (punctuation, spacing and case aside)."""
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    workspace = tmp_path / "workspace"
    agent = Coordinator(CallRecorder(workspace, room="r", now=seoul_now()), SimpleNamespace(), workspace,
                        clock=seoul_now)
    user_said(agent, "음, 내일 아침 여덟 시에 다시 전화해 줄래요?")
    at = (datetime.now(SEOUL) + timedelta(days=1)).replace(hour=8, minute=0).isoformat(timespec="minutes")
    for invented in ("Please call me tomorrow morning.", "", "   "):
        refused = asyncio.run(agent.book_call(None, topic="the interview", asked=invented, at=at))
        assert refused.startswith("Not booked") and OfferStore(workspace).all() == [], invented
    said = asyncio.run(agent.book_call(None, topic="the interview", asked="내일 아침 여덟시에 다시 전화해 줄래요", at=at))
    assert said.startswith("Booked")
    user_said(agent, "Can you call me back at nine instead?", "Yes, nine works.")
    nine = at.replace("T08:00", "T09:00")
    assert asyncio.run(agent.book_call(None, topic="the interview", asked="yes nine works", at=nine)).startswith("Moved")


@pytest.mark.parametrize("said, asked, booked", [
    # A part out of a sentence or a clause between commas is refused when what is left out could decide it: a
    # "not", a "don't" or a "안" beside it, the end of Korean words, or most of it.
    (["아니요, 전화는 안 해도 돼요."], "전화", False),
    (["No thanks, don't call me back."], "call me back", False),
    (["Don't call me back."], "call me back", False),
    (["No thanks. Don't call me back."], "call me back", False),
    (["Please don't call me tomorrow."], "call me tomorrow", False),
    (["I'm not free now so call me back tomorrow."], "call me back tomorrow", False),
    (["내일은 전화하지 마세요."], "내일은 전화하지", False),
    (["아니요. 전화하지 마세요."], "전화하지", False),
    (["다시 전화 안 해 줘도 돼요."], "해 줘도 돼요", False),
    (["Then my friend said she'd call me back."], "call me back", False),
    (["Call me back tomorrow — actually no."], "Call me back tomorrow", False),
    (["You cannot call me back tomorrow."], "call me back tomorrow", False),
    # A quote that ends at the dot closing "a.m." or "p.m." is refused when the next words of the same turn take it
    # back (review of the final fixes: "Call me at 9 a.m." out of the first one booked). The next sentence is still a
    # sentence of its own: a constraint or a reason after "a.m." takes nothing back (review of 2026-10-16).
    (["Call me at 9 a.m. no actually don't."], "Call me at 9 a.m.", False),
    (["Call me at 9 p.m. no wait, never mind."], "Call me at 9 p.m.", False),
    (["Call me at 9 a.m. Never mind, don't."], "Call me at 9 a.m.", False),
    (["Call me at 9 p.m. Wait, no."], "Call me at 9 p.m.", False),  # second review, finding 5: the comma forms
    (["Call me at 9 a.m. Actually, no, don't."], "Call me at 9 a.m.", False),
    # Third review, finding 5: a plain full stop too, however the time is written, and in Korean; but words after the
    # request that only add to it take nothing back.
    (["Call me at 9 PM. Wait, no."], "Call me at 9 PM", False),
    (["Call me at nine. Actually, no."], "Call me at nine", False),
    (["Call me back tomorrow. No, wait, never mind."], "Call me back tomorrow", False),
    (["Call me tomorrow at 8 about the dentist. No wait, never mind."], "Call me tomorrow at 8", False),
    (["내일 아침 9시에 전화해 줘. 아니다, 하지 마."], "내일 아침 9시에 전화해 줘", False),
    (["내일 아침 9시에 전화해 줘. 아니, 10시로 해 줘."], "내일 아침 9시에 전화해 줘", False),
    (["Call me at 9 p.m. Don't be late."], "Call me at 9 p.m.", True),
    (["Call me at 9 p.m. No rush though."], "Call me at 9 p.m.", True),
    (["Call me at nine. I'll be up by then."], "Call me at nine", True),
    (["내일 아침 9시에 전화해 줘. 아니면 10시도 괜찮아."], "내일 아침 9시에 전화해 줘", True),
    (["Call me at 9 a.m. I'll be up by then."], "Call me at 9 a.m.", True),
    (["Call me at 7 p.m. My meeting ends at 6."], "Call me at 7 p.m.", True),
    (["I'm not free until 10 a.m. Call me back then."], "Call me back then", True),
    (["I can't talk at 9 a.m. Call me at 10."], "Call me at 10", True),
    (["Call me back at 8 a.m. Not earlier, please."], "Call me back at 8 a.m.", True),
    (["I'm in the U.S. this week. Call me tomorrow."], "Call me tomorrow", True),
    (["I went hiking yesterday."], "yes", False),
    (["Hmm."], "m", False),
    # Whole sentences or clauses, one or several in a row (an answer split at pauses, over turns too), or most of
    # one without its lead-in: the call AI left out a lead-in such as "so" or "그럼" in 19 of its 24 first tries
    # (Haiku 5.5, 2026-10-16), and each of those cost the call a second try while only whole clauses passed.
    (["오늘 회의 정말 길었어요 아무튼 내일 아침 여덟 시에 다시 전화해 줄래요"], "내일 아침 여덟 시에 다시 전화해 줄래요", True),
    (["오늘 회의가 정말 길었어요. 그럼 내일 오후 세 시에 다시 전화해 줄 수 있어요?"], "내일 오후 세 시에 다시 전화해 줄 수 있어요?", True),
    (["The meeting ran long today. Okay, so could you call me back tomorrow at nine?"],
     "could you call me back tomorrow at nine?", True),
    (["음, 내일 아침 여덟 시에 다시 전화해 줄래요?"], "내일 아침 여덟 시에 다시 전화해 줄래요", True),
    (["Call me tomorrow at 8 about the dentist."], "Call me tomorrow at 8", True),  # an English tail left out
    (["내일 아침", "여덟 시에 다시 전화해 줘"], "내일 아침 여덟 시에 다시 전화해 줘", True),
    (["네.", "내일.", "아침 8시에.", "전화 주세요."], "네, 내일 아침 8시에 전화 주세요", True),
    (["Can you call me back at nine?", "Yes, nine works."], "Yes, nine works", True),
    (["네."], "네", True),
    # Dots inside a.m. or U.S. end nothing; a request early in a long call still counts.
    (["Call me at 7 a.m. or 8 a.m. tomorrow."], "Call me at 7 a.m. or 8 a.m. tomorrow.", True),
    (["Call me when the U.S. market opens at 9:30 a.m. tomorrow."],
     "Call me when the U.S. market opens at 9:30 a.m. tomorrow.", True),
    (["Call me tomorrow at nine about the interview."] + ["Okay."] * 12,
     "Call me tomorrow at nine about the interview.", True),
])
def test_the_words_given_for_a_call_back_must_be_most_of_what_the_user_said(tmp_path, said, asked, booked):
    """Review of the last stretch, finding 3: any fragment of anything the user said passed, a word out of a refusal
    too. Review of the final fixes, finding 2: "most of a sentence" still passed "call me back" out of "Don't call me
    back.", split "7 a.m." into sentences no quote could match, joined at most three and looked back only twelve
    turns. The words must now be most of a sentence or clause the user said in this call (or of several in a row);
    they may leave out a lead-in, or an English tail, but no "not" or "don't" and never the end of Korean words.
    Whether they ask for the call stays the call AI's reading."""
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    workspace = tmp_path / "workspace"
    agent = Coordinator(CallRecorder(workspace, room="r", now=seoul_now()), SimpleNamespace(), workspace,
                        clock=seoul_now)
    user_said(agent, *said)
    at = (datetime.now(SEOUL) + timedelta(days=1)).replace(hour=8, minute=0).isoformat(timespec="minutes")
    answer = asyncio.run(agent.book_call(None, topic="a call back", asked=asked, at=at))
    assert answer.startswith("Booked") is booked, answer
    assert len(OfferStore(workspace).all()) == (1 if booked else 0)


def test_a_call_back_the_user_asks_for_is_booked_and_continues_this_call(tmp_path):
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    workspace = tmp_path / "workspace"
    rec = CallRecorder(workspace, room="r", now=seoul_now())
    agent = Coordinator(rec, SimpleNamespace(), workspace, clock=seoul_now)
    tomorrow = (datetime.now(SEOUL) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    user_said(agent, "Call me tomorrow at eight about how the interview went.")
    said = asyncio.run(agent.book_call(None, topic="how the interview went",
                                       asked="Call me tomorrow at eight about how the interview went.",
                                       at=tomorrow.isoformat(timespec="minutes")))
    assert said.startswith("Booked: you will call at")
    (offer,) = OfferStore(workspace).all()
    assert (offer.state, offer.source, offer.topic) == ("booked", "call", "how the interview went")
    assert offer.call_at == tomorrow.isoformat(timespec="seconds")
    assert offer.continue_from == f"{rec.dir.parent.name}/{rec.dir.name}"
    assert offer.reason.startswith("In our call at ") and offer.reason.endswith(", you asked me to call you back at "
                                                                                 "this time.")
    asked = "Call me tomorrow at eight about how the interview went."
    assert "Ask the user for a later time" in asyncio.run(agent.book_call(None, topic="x", asked=asked,
                                                                          at="2020-01-01T08:00"))
    assert "ISO 8601" in asyncio.run(agent.book_call(None, topic="x", asked=asked, at="tomorrow at 8"))
    assert len(OfferStore(workspace).all()) == 1


# Regressions from the review on 2026-10-14.

def test_a_call_me_now_that_was_never_placed_closes(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, _ = asked(tmp_path)
    store.answer(offer.id, "now", NOW)  # voice/outbound.py is not running
    assert store.due(NOW + LATE_BY + timedelta(seconds=1)) == []
    assert store.get(offer.id).state == "expired"


def test_a_claimed_call_is_not_claimed_twice_and_a_call_off_before_its_turn_wins(tmp_path):
    """What this checks, as its name says (server review 16): a second claim of a claimed offer gets nothing, and a
    call called off while another one is prepared is never placed. Runners at the same moment, and a call-off
    while the call itself is prepared, are in test_a_booked_call_called_off_while_it_is_prepared_never_rings and
    the tests after it."""
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    claimed = store.book("claimed", datetime.now(SEOUL) - timedelta(seconds=3), datetime.now(SEOUL))
    assert store.claim(claimed.id, datetime.now(SEOUL)).offer.state == "calling"
    assert store.claim(claimed.id, datetime.now(SEOUL)) is None  # a second runner gets nothing
    first = store.book("first", datetime.now(SEOUL) - timedelta(seconds=2), datetime.now(SEOUL))
    second = store.book("second", datetime.now(SEOUL) - timedelta(seconds=1), datetime.now(SEOUL))
    placed = []

    async def prepare(plan, extra=None, deadline_s=None, **kw):
        other = second if plan.topic == "first" else first
        store.answer(other.id, "decline", datetime.now(SEOUL))  # "Call off" while the other call is prepared
        return Prep()

    async def place(claim, person, prep=None):
        placed.append(claim.offer.topic)
        return f"room-{len(placed)}"

    asyncio.run(call_due(me.person, prepare=prepare, place=place))
    asyncio.run(call_due(me.person, prepare=prepare, place=place))
    assert len(placed) == 1  # one call; the other was called off before its turn
    other = second if placed == ["first"] else first
    assert store.get(other.id).history[-1]["state"] == "declined"


def test_no_call_while_another_rings_and_a_new_question_waits_for_the_old(tmp_path):
    from beelink.planner import OUTBOUND_LOG

    me = signed_in(tmp_path)
    workspace = me.workspace
    store = OfferStore(workspace)
    offer, plan = asked(tmp_path, now=datetime.now(SEOUL), workspace=workspace)
    store.answer(offer.id, "now", datetime.now(SEOUL))
    log = workspace / OUTBOUND_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"at": datetime.now(SEOUL).isoformat(timespec="seconds")}) + "\n")  # an AI call just rang

    async def place(*a, **kw):
        raise AssertionError("must wait")

    assert asyncio.run(call_due(me.person, place=place)) == [] and store.get(offer.id).state == "now"
    older = store.ask(another_conversation(plan, 1), NOW)
    with pytest.raises(QuestionWaiting, match=older.id):  # one question at a time: the older one keeps working
        store.ask(another_conversation(plan, 2), NOW + timedelta(minutes=40))
    assert store.get(older.id).state == "asked" and len(store.all()) == 2


def test_moving_or_calling_off_a_call_back_in_the_same_call(tmp_path):
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    workspace = tmp_path / "workspace"
    agent = Coordinator(CallRecorder(workspace, room="r", now=seoul_now()), SimpleNamespace(), workspace,
                        clock=seoul_now)
    day = (datetime.now(SEOUL) + timedelta(days=1)).replace(second=0, microsecond=0)
    user_said(agent, "Call me at eight tomorrow.", "Make it nine.")
    asyncio.run(agent.book_call(None, topic="the interview", asked="Call me at eight tomorrow",
                                at=day.replace(hour=8, minute=0).isoformat()))
    said = asyncio.run(agent.book_call(None, topic="the interview", asked="Make it nine.",
                                       at=day.replace(hour=9, minute=0).isoformat()))
    assert said.startswith("Moved the call back")
    states = sorted(o.state for o in OfferStore(workspace).all())
    assert states == ["booked", "declined"]
    assert asyncio.run(agent.cancel_call(None)).startswith("Called off")
    assert sorted(o.state for o in OfferStore(workspace).all()) == ["declined", "declined"]
    assert asyncio.run(agent.cancel_call(None)).startswith("No call back")


def test_looking_ahead_for_booked_calls_does_not_close_questions_early(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 14, 20, 0, tzinfo=SEOUL))  # suggests 21:00
    store.due(datetime(2026, 10, 14, 20, 59, 45, tzinfo=SEOUL), ahead=timedelta(seconds=30))
    assert store.answer(offer.id, "later", datetime(2026, 10, 14, 20, 59, 50, tzinfo=SEOUL)).state == "booked"



def test_notifications_wait_for_morning_and_a_fifth_of_the_day_is_held(tmp_path, monkeypatch):
    """A notification asked for at night waits for 08:00, and the fifth of a day is held (server review 16: the old
    name promised it went out tomorrow; the rule is now that it never does, which
    test_the_fifth_offer_of_a_day_is_never_notified_even_when_it_suggests_tomorrow checks)."""
    from voice.outbound import send_held, send_offer

    sent = []

    async def fake_alert(payload, workspace, **kw):
        sent.append(payload["offer"])
        return f"apns-{len(sent)}"

    monkeypatch.setattr(voip_push, "alert", fake_alert)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    night = datetime(2026, 10, 14, 23, 30, tzinfo=SEOUL)
    offer, plan = asked(tmp_path, now=night, workspace=me.workspace)
    assert asyncio.run(send_offer(offer, me.person, night)).startswith("held: quiet hours, the notification goes out at 08:00")
    assert [o.id for o in store.unsent(night)] == [offer.id] and sent == []
    morning = datetime(2026, 10, 15, 8, 0, tzinfo=SEOUL)
    asyncio.run(send_held(me.person, morning))
    assert sent == [offer.id] and store.get(offer.id).notified_at == "2026-10-15T08:00:00+09:00"
    for i in range(3):  # three more today, each 30 minutes after the last (2026-10-15): four in all
        o = next_question(store, plan, i, morning + timedelta(minutes=40 * (i + 1)))
        asyncio.run(send_offer(o, me.person, morning + timedelta(minutes=40 * (i + 1))))
    fifth = next_question(store, plan, 9, morning + timedelta(hours=3))
    assert asyncio.run(send_offer(fifth, me.person, morning + timedelta(hours=3))).startswith("held: 4 notifications")


# ---------- the offers gate: a call rings only after the user's yes (server review findings 2, 5, 8, 15) ----------

def test_a_booked_call_called_off_while_it_is_prepared_never_rings(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer = store.book("the interview", datetime.now(SEOUL) + timedelta(seconds=1), datetime.now(SEOUL))
    answers, rooms = [], []

    async def prepare(plan, **kw):  # the user taps "Call off" while the call is prepared
        try:
            answers.append(store.answer(offer.id, "decline", datetime.now(SEOUL)).state)
        except ValueError as exc:
            answers.append(f"refused: {exc}")
        return Prep(note="n", opening="o")

    assert asyncio.run(call_due(me.person, prepare=prepare, place=placing(rooms))) == []
    assert answers == ["declined"] and rooms == [] and store.get(offer.id).state == "declined"
    assert not (me.workspace / "state" / "outbound.jsonl").exists()  # no ring line either


def test_a_booked_call_called_off_while_it_waits_for_its_time_never_rings(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer = store.book("the interview", datetime.now(SEOUL) + timedelta(seconds=1.5), datetime.now(SEOUL))
    rooms, answers = [], []

    async def main():
        runner = asyncio.create_task(call_due(me.person, prepare=prepared_quickly, place=placing(rooms)))
        await asyncio.sleep(0.5)  # prepared, waiting for the booked time
        try:
            answers.append(store.answer(offer.id, "decline", datetime.now(SEOUL)).state)
        except ValueError as exc:
            answers.append(f"refused: {exc}")
        return await runner

    assert asyncio.run(main()) == [] and answers == ["declined"] and rooms == []
    with pytest.raises(ValueError):  # once declined, it stays declined
        store.answer(offer.id, "now", datetime.now(SEOUL))


async def prepared_quickly(plan, **kw):
    return Prep(note="n", opening="o")


def test_once_the_room_exists_the_call_can_no_longer_be_called_off(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer = store.book("the interview", datetime.now(SEOUL) - timedelta(seconds=1), datetime.now(SEOUL))
    rooms = []
    assert asyncio.run(call_due(me.person, prepare=prepared_quickly, place=placing(rooms))) == [r.name for r in rooms]
    assert len(rooms) == 1 and store.get(offer.id).state == "called" and store.get(offer.id).room == rooms[0].name
    with pytest.raises(ValueError):
        store.answer(offer.id, "decline", datetime.now(SEOUL))


def test_only_a_claimed_offer_still_wanted_reaches_a_room(tmp_path):
    from voice.dispatch import place_call

    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer = store.book("the interview", datetime.now(SEOUL), datetime.now(SEOUL))
    claim = store.claim(offer.id, datetime.now(SEOUL))
    assert claim is not None and store.claim(offer.id, datetime.now(SEOUL)) is None  # a second runner gets nothing
    store.answer(offer.id, "decline", datetime.now(SEOUL))  # called off after the claim
    rooms = []
    assert asyncio.run(place_call(claim, me.person, prep=None, lk=fake_livekit(rooms))) is None
    assert rooms == [] and store.get(offer.id).state == "declined"
    again = store.book("the dentist", datetime.now(SEOUL), datetime.now(SEOUL))
    mine = store.claim(again.id, datetime.now(SEOUL))
    room = asyncio.run(place_call(mine, me.person, prep=None, lk=fake_livekit(rooms)))
    assert [r.name for r in rooms] == [room] and store.get(again.id).state == "called"
    assert asyncio.run(place_call(mine, me.person, prep=None, lk=fake_livekit(rooms))) is None  # a claim places once


def test_a_test_call_from_the_mac_rings_no_phone(tmp_path, monkeypatch):
    from voice.dispatch import open_test_call

    async def no_push(*a, **kw):
        raise AssertionError("a test call must not push")

    monkeypatch.setattr(voip_push, "ring", no_push)
    me = signed_in(tmp_path)
    plan = plan_of(store_offer := OfferStore(me.workspace).book("x", datetime.now(SEOUL), datetime.now(SEOUL)))
    rooms = []
    room = asyncio.run(open_test_call(plan, me.person, prep=None, lk=fake_livekit(rooms)))
    assert [r.name for r in rooms] == [room] and room.startswith("fake-")  # never a room the app would answer
    meta = json.loads(rooms[0].agents[0].metadata)
    assert (meta["direction"], meta["person"]) == ("outbound", "matt")
    assert not (me.workspace / "state" / "outbound.jsonl").exists()
    assert OfferStore(me.workspace).get(store_offer.id).state == "booked"  # no offer was used up


def test_the_rules_path_has_no_switch_that_calls_without_asking():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "voice.outbound", "--rules", "--direct"], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 2 and "unrecognized arguments: --direct" in out.stderr


def test_a_runner_stopped_before_placing_gives_the_offer_back(tmp_path):
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    store.answer(offer.id, "now", datetime.now(SEOUL))  # the user tapped "Call me now"
    rooms = []

    async def slow_prepare(plan, **kw):
        await asyncio.sleep(10)
        return Prep()

    async def main():
        runner = asyncio.create_task(call_due(me.person, prepare=slow_prepare, place=placing(rooms)))
        await asyncio.sleep(0.2)
        runner.cancel()  # a settings change restarts the person's loops; so does a deploy
        await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(main())
    assert store.get(offer.id).state == "now" and [o.id for o in store.due(datetime.now(SEOUL))] == [offer.id]
    (room,) = asyncio.run(call_due(me.person, prepare=prepared_quickly, place=placing(rooms)))
    assert [r.name for r in rooms] == [room] and store.get(offer.id).state == "called"


def test_an_offer_notification_is_recorded_before_it_is_pushed(tmp_path, monkeypatch):
    from voice.outbound import send_held, send_offer

    me = signed_in(tmp_path)
    now = datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL)
    offer, _ = asked(tmp_path, now=now, workspace=me.workspace)
    pushed = []

    class Stopped(BaseException):  # the process stops while the push is under way
        pass

    async def dies(payload, person, **kw):
        pushed.append(payload["offer"])
        raise Stopped()

    async def works(payload, person, **kw):
        pushed.append(payload["offer"])
        return "apns"

    monkeypatch.setattr(voip_push, "alert", dies)
    with pytest.raises(Stopped):
        asyncio.run(send_offer(offer, me.person, now))
    monkeypatch.setattr(voip_push, "alert", works)
    asyncio.run(send_held(me.person, now + timedelta(minutes=1)))  # the loop, started again
    assert pushed == [offer.id] and OfferStore(me.workspace).notified_on(now) == 1


def test_two_senders_at_once_keep_to_the_daily_limit(tmp_path, monkeypatch):
    from voice.outbound import send_offer

    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    now = datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL)
    pushed = []

    async def slow(payload, person, **kw):
        await asyncio.sleep(0.1)
        pushed.append(payload["offer"])
        return "apns"

    monkeypatch.setattr(voip_push, "alert", slow)
    _, plan = asked(tmp_path, now=now, workspace=me.workspace)
    for i in range(3):
        sent = next_question(store, plan, i, now - timedelta(hours=3 - i))
        asyncio.run(send_offer(sent, me.person, now - timedelta(hours=3 - i)))
    pushed.clear()
    last = next_question(store, plan, 9, now)

    async def both():
        return await asyncio.gather(send_offer(last, me.person, now), send_offer(last, me.person, now))

    asyncio.run(both())
    assert pushed == [last.id] and store.notified_on(now) == 4


def test_answers_to_an_offer_get_the_status_the_app_can_tell_apart(tmp_path):
    """Contract section 4: 404 for no such offer (checked first), 400 for an unknown choice, 409 for a closed or
    answered offer, 401 once the sign-in has ended."""
    from aiohttp.test_utils import TestClient, TestServer
    from voice.token_server import make_app

    me = signed_in(tmp_path)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            async def answer(offer_id, choice):
                return (await client.post(f"/offers/{offer_id}", json={"choice": choice})).status

            statuses = [await answer("20261014-183000-abcd", "explode"), await answer(offer.id, "explode"),
                        await answer(offer.id, "decline"), await answer(offer.id, "now")]
            me.people.end_session(me.token)
            return statuses + [await answer(offer.id, "now")]

    assert asyncio.run(main()) == [404, 400, 200, 409, 401]


def test_call_me_back_in_ten_minutes_late_in_a_call_books_ten_minutes_from_now(tmp_path):
    """BC-040: the AI's "It is now" line is fixed when the call starts, so a relative time is counted on the
    server's clock, and the answer gives the time booked for the AI to read back."""
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    workspace = tmp_path / "workspace"
    started = datetime.now(SEOUL) - timedelta(minutes=15)  # the call began a quarter of an hour ago
    agent = Coordinator(CallRecorder(workspace, room="r", now=started), SimpleNamespace(), workspace, clock=seoul_now)
    user_said(agent, "Call me back in ten minutes.")
    said = asyncio.run(agent.book_call(None, topic="the dentist", asked="call me back in ten minutes", in_minutes=10))
    (offer,) = OfferStore(workspace).all()
    booked = datetime.fromisoformat(offer.call_at)
    assert abs((booked - (datetime.now(SEOUL) + timedelta(minutes=10))).total_seconds()) < 5
    assert f"{booked:%-I:%M %p}" in said and "read" in said.lower()
    for bad in (0, -5, 7 * 24 * 60 + 1):
        assert not asyncio.run(agent.book_call(None, topic="x", asked="Call me back in ten minutes",
                                               in_minutes=bad)).startswith(("Booked", "Moved"))
    assert len(OfferStore(workspace).all()) == 1



# ---------- an agreed call that cannot be placed says so (BC-104, contract section 5) ----------

def notices(monkeypatch) -> list[dict]:
    sent = []

    async def alert(payload, person, **kw):
        sent.append(payload)
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    return sent


def test_an_agreed_call_past_the_budget_tells_the_user_once(tmp_path, monkeypatch):
    from voice.outbound import tell_not_placed

    sent = notices(monkeypatch)
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"daily_budget_usd": 0, "language": "ko"},
                              allowed=("daily_budget_usd", "language"))
    night = datetime.now(SEOUL).replace(hour=21, minute=0, second=0, microsecond=0)
    booked = OfferStore(me.workspace).book("the interview", night, night - timedelta(hours=1))
    asyncio.run(call_due(me.person, night, prepare=prepared_quickly, place=placing([])))
    for _ in range(2):  # every round of the loop looks; the user hears it once
        asyncio.run(tell_not_placed(me.person, night))
    (payload,) = sent
    assert payload["aps"]["alert"]["body"] == "밤 9시 통화를 걸지 못했어요. 오늘 예산을 다 썼어요."
    assert payload["aps"]["thread-id"] == "offers" and "category" not in payload["aps"]
    assert OfferStore(me.workspace).get(booked.id).state == "failed"


def test_a_call_too_late_or_failed_tells_the_user_why_in_their_language(tmp_path, monkeypatch):
    from voice.outbound import tell_not_placed

    sent = notices(monkeypatch)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    late = store.book("the dentist", datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL), datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL))
    store.expire(datetime(2026, 10, 15, 21, 11, tzinfo=SEOUL))  # the runner was busy with another call
    asked_now, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    store.answer(asked_now.id, "now", datetime.now(SEOUL))

    async def broken(claim, person, **kw):
        store.fail(claim, datetime.now(SEOUL), "LiveKit is down")
        raise RuntimeError("LiveKit is down")

    asyncio.run(call_due(me.person, prepare=prepared_quickly, place=broken))
    asyncio.run(tell_not_placed(me.person, datetime(2026, 10, 15, 23, 30, tzinfo=SEOUL)))  # quiet hours hold none of these
    assert sorted(p["aps"]["alert"]["body"] for p in sent) == [
        "Couldn't place the call you asked for: something went wrong while placing it.",
        "Couldn't place your 9:00 PM call: it could not be placed on time."]
    assert store.get(late.id).state == "expired"


def test_a_question_nobody_answered_closes_without_a_word(tmp_path, monkeypatch):
    from voice.outbound import tell_not_placed

    sent = notices(monkeypatch)
    me = signed_in(tmp_path)
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 15, 18, 0, tzinfo=SEOUL), workspace=me.workspace)
    OfferStore(me.workspace).expire(datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL))
    asyncio.run(tell_not_placed(me.person, datetime(2026, 10, 15, 21, 1, tzinfo=SEOUL)))
    assert sent == [] and OfferStore(me.workspace).get(offer.id).state == "expired"


# ---------- offer notifications wait 30 minutes after a call or a notification, and never ring into a call ----------
# (the user's decision of 2026-10-15; BC-015, BC-016)

def sends(monkeypatch) -> list[str]:
    sent = []

    async def alert(payload, person, **kw):
        sent.append(payload["offer"])
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    return sent


def a_call(workspace, started: datetime, minutes: float) -> None:
    folder = workspace / started.strftime("%Y-%m-%d") / started.strftime("%H%M")
    folder.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "started_at": started.isoformat(timespec="seconds")},
             {"t": minutes * 60, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


def test_an_offer_waits_thirty_minutes_after_a_call_and_still_shows_in_the_app(tmp_path, monkeypatch):
    from voice.outbound import send_held, send_offer

    sent = sends(monkeypatch)
    me = signed_in(tmp_path)
    a_call(me.workspace, datetime(2026, 10, 15, 19, 50, tzinfo=SEOUL), minutes=10)  # ended at 20:00
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 15, 20, 10, tzinfo=SEOUL), workspace=me.workspace)
    said = asyncio.run(send_offer(offer, me.person, datetime(2026, 10, 15, 20, 10, tzinfo=SEOUL)))
    assert said.startswith("held:") and "20:30" in said and sent == []
    assert [o.id for o in OfferStore(me.workspace).open(datetime(2026, 10, 15, 20, 11, tzinfo=SEOUL))] == [offer.id]
    asyncio.run(send_held(me.person, datetime(2026, 10, 15, 20, 29, tzinfo=SEOUL)))
    assert sent == []
    asyncio.run(send_held(me.person, datetime(2026, 10, 15, 20, 30, tzinfo=SEOUL)))
    assert sent == [offer.id]


def test_missed_and_declined_ai_calls_hold_offers_too(tmp_path, monkeypatch):
    from voice.dispatch import decline_ring
    from voice.outbound import send_offer

    sent = sends(monkeypatch)
    me = signed_in(tmp_path)
    log = me.workspace / OUTBOUND_LOG
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps({"at": at, "room": room}) + "\n" for at, room in (
        ("2026-10-15T19:00:00+09:00", "call-matt-20261015-190000"),  # missed: it rang its 60 s
        ("2026-10-15T20:00:00+09:00", "call-matt-20261015-200000"))))  # declined 20 s in
    decline_ring(me.person, "call-matt-20261015-200000", datetime(2026, 10, 15, 20, 0, 20, tzinfo=SEOUL))
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 15, 20, 10, tzinfo=SEOUL), workspace=me.workspace)
    assert "20:30" in asyncio.run(send_offer(offer, me.person, datetime(2026, 10, 15, 20, 30, 10, tzinfo=SEOUL)))  # held
    assert asyncio.run(send_offer(offer, me.person, datetime(2026, 10, 15, 20, 30, 20, tzinfo=SEOUL))).startswith("notification sent")
    assert sent == [offer.id]


def test_an_offer_waits_thirty_minutes_after_the_last_offer_notification(tmp_path, monkeypatch):
    from voice.outbound import send_offer

    sent = sends(monkeypatch)
    me = signed_in(tmp_path)
    first, plan = asked(tmp_path, now=datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL), workspace=me.workspace)
    assert asyncio.run(send_offer(first, me.person, datetime(2026, 10, 15, 12, 0, tzinfo=SEOUL))).startswith("notification sent")
    OfferStore(me.workspace).answer(first.id, "decline", datetime(2026, 10, 15, 12, 5, tzinfo=SEOUL))
    second = OfferStore(me.workspace).ask(another_conversation(plan), datetime(2026, 10, 15, 12, 10, tzinfo=SEOUL))
    assert "12:30" in asyncio.run(send_offer(second, me.person, datetime(2026, 10, 15, 12, 10, tzinfo=SEOUL)))
    assert asyncio.run(send_offer(second, me.person, datetime(2026, 10, 15, 12, 30, tzinfo=SEOUL))).startswith("notification sent")
    assert sent == [first.id, second.id]


def test_no_offer_notification_goes_out_during_a_call(tmp_path, monkeypatch):
    from voice.outbound import send_offer

    sent = sends(monkeypatch)
    me = signed_in(tmp_path)
    live = me.workspace / datetime.now(SEOUL).strftime("%Y-%m-%d") / "0001"
    live.mkdir(parents=True)
    (live / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started",
                                                   "started_at": datetime.now(SEOUL).isoformat(timespec="seconds")}) + "\n")
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    said = asyncio.run(send_offer(offer, me.person, datetime.now(SEOUL)))
    assert said.startswith("held: a call is going on") and sent == []


def test_the_fifth_offer_of_a_day_is_never_notified_even_when_it_suggests_tomorrow(tmp_path, monkeypatch):
    """The user's rule (2026-10-15): the fifth offer of a day shows only in the app list."""
    from voice.outbound import send_held, send_offer

    sent = sends(monkeypatch)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    morning = datetime(2026, 10, 15, 8, 0, tzinfo=SEOUL)
    _, plan = asked(tmp_path, now=morning, workspace=me.workspace)
    for i in range(4):  # four sent, 40 minutes apart
        o = next_question(store, plan, i, morning + timedelta(minutes=40 * i))
        assert asyncio.run(send_offer(o, me.person, morning + timedelta(minutes=40 * i))).startswith("notification")
    late = datetime(2026, 10, 15, 20, 55, tzinfo=SEOUL)  # suggests 21:00 tomorrow
    fifth = next_question(store, plan, 9, late)
    assert asyncio.run(send_offer(fifth, me.person, late)).startswith("held: 4 notifications")
    asyncio.run(send_held(me.person, datetime(2026, 10, 16, 8, 0, tzinfo=SEOUL)))  # a new day, with the offer still open
    assert fifth.id not in sent and [o.id for o in store.open(datetime(2026, 10, 16, 8, 0, tzinfo=SEOUL))] == [fifth.id]


def test_the_app_sees_a_booked_call_as_booked_until_its_room_is_being_made(tmp_path):
    """The app shows "Call off" for booked offers. A booked call claimed 30 s early can still be called off, so the
    app sees it as booked until its room is being made; a "Call me now" being prepared shows as calling."""
    from aiohttp.test_utils import TestClient, TestServer
    from voice.token_server import make_app

    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    booked = store.book("the interview", datetime.now(SEOUL) + timedelta(seconds=20), datetime.now(SEOUL))
    tapped, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    store.answer(tapped.id, "now", datetime.now(SEOUL))
    claims = [store.claim(o.id, datetime.now(SEOUL)) for o in (booked, tapped)]

    async def listed():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            return {o["id"]: o["state"] for o in (await (await client.get("/offers")).json())["offers"]}

    assert asyncio.run(listed()) == {booked.id: "booked", tapped.id: "calling"}
    store.begin_placing(claims[0], datetime.now(SEOUL))
    assert asyncio.run(listed())[booked.id] == "calling"


def test_an_unknown_offer_is_404_whatever_the_request_body(tmp_path):
    """Contract section 4: whether the offer exists is checked before the body is read (acceptance BC-170)."""
    from aiohttp.test_utils import TestClient, TestServer
    from voice.token_server import make_app

    me = signed_in(tmp_path)

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            statuses = []
            for body in (None, b"", b"not json", b"[]", b'"now"'):
                response = await client.post("/offers/20261014-183000-abcd", data=body)
                statuses.append(response.status)
            return statuses

    assert asyncio.run(main()) == [404] * 5


def test_a_call_booked_in_a_korean_call_says_why_in_korean():
    """Decision 2 of the app contract: the reason shows on the call's card, in the person's language."""
    store_dir = Path(tempfile.mkdtemp(prefix="book-"))
    now = datetime(2026, 10, 15, 14, 27, tzinfo=SEOUL)
    offer = OfferStore(store_dir).book("도서관에서 정리한 할 일 다시 보기", now + timedelta(hours=4), now,
                                       language=call_language("ko"))
    assert offer.reason == "오후 2시 27분 통화에서 이 시간에 다시 전화해 달라고 하셨어요."


# ---------- 2026-10-17: a call a voice memo asks for ----------

def test_a_call_booked_from_a_memo_is_called_off_from_the_app_and_carries_its_memo(tmp_path):
    from voice.dispatch import call_metadata
    from voice.offers import agreed, memo_booked_notification, plan_of

    now = datetime(2026, 10, 17, 18, 30, tzinfo=SEOUL)
    store = OfferStore(tmp_path)
    offer, joined = store.book_from_memo("이사 준비 정리", now.replace(hour=21, minute=0), now,
                                         reason="메모에서 부탁하셨어요.", memo_id="j-41", asked="오늘 밤 9시에 전화해서 같이 정리하자")
    assert offer.view("ko", SEOUL)["state"] == "booked" and not joined  # the app shows its "Call off"
    plan = plan_of(offer)
    assert plan.memo_ids == ["j-41"] and json.loads(call_metadata("outbound", plan))["memo_ids"] == ["j-41"]
    assert "voice memo" in agreed(offer) and "오늘 밤 9시에 전화해서 같이 정리하자" in agreed(offer)
    body = memo_booked_notification(offer, now, call_language("ko"))["aps"]["alert"]["body"]
    assert body == "오늘 밤 9시에 전화할게요: 이사 준비 정리. 앱에서 취소할 수 있어요."
    english = memo_booked_notification(offer, now, call_language("en"))
    assert english["aps"]["alert"]["body"] == "I'll call you at 9:00 PM today: 이사 준비 정리. You can call it off in the app."
    assert "category" not in english["aps"]  # a plain notification: no buttons
    assert store.answer(offer.id, "decline", now).state == "declined"


def test_a_memo_asking_for_a_time_near_a_booked_call_joins_it_and_one_further_off_is_booked(tmp_path):
    """Review of 2026-10-17, finding 3: two memos asking for 9 tonight booked two calls at 21:00. A memo asking for a
    time within 10 minutes of a booked call joins it; one asking for 9:25 does not join a 9:00 call, since the call
    keeps its time and the memo may have said why it wanted later ("9시 반 넘어서 전화해 줘, 그 전엔 밥 먹어";
    second review, finding 3)."""
    now = datetime(2026, 10, 17, 18, 30, tzinfo=SEOUL)
    store = OfferStore(tmp_path)
    first, _ = store.book_from_memo("이사", now.replace(hour=21, minute=0), now, reason="r", memo_id="j-41", asked="a")
    near, joined = store.book_from_memo("짐", now.replace(hour=21, minute=5), now, reason="r", memo_id="j-42", asked="b")
    assert joined and near.id == first.id and near.memo_ids == ["j-41", "j-42"]
    assert near.call_at == first.call_at and near.topic == "이사"  # the call stays as booked and told
    later, joined = store.book_from_memo("밥 다음", now.replace(hour=21, minute=25), now, reason="r", memo_id="j-43",
                                         asked="c")
    assert not joined and later.id != first.id and later.call_at == "2026-10-17T21:25:00+09:00"
    call_back = OfferStore(tmp_path / "other").book("통화 이어서", now.replace(hour=21, minute=0), now)
    joined_call, joined = OfferStore(tmp_path / "other").book_from_memo(
        "메모", now.replace(hour=21, minute=2), now, reason="r", memo_id="j-44", asked="d")
    assert joined and joined_call.id == call_back.id  # a call-back booked out loud is joined the same way


@pytest.mark.parametrize("days, ko, en", [
    (0, "오늘 밤 9시에 전화할게요", "I'll call you at 9:00 PM today"),
    (1, "내일 밤 9시에 전화할게요", "I'll call you at 9:00 PM tomorrow"),
    (3, "10월 20일 화요일 밤 9시에 전화할게요", "I'll call you at 9:00 PM on Tuesday, October 20"),
])
def test_the_memo_booking_notification_names_the_day_of_the_call(tmp_path, days, ko, en):
    """Review of 2026-10-17, finding 4: a call booked for three days ahead read "tomorrow"."""
    from voice.offers import memo_booked_notification

    now = datetime(2026, 10, 17, 18, 30, tzinfo=SEOUL)
    offer, _ = OfferStore(tmp_path).book_from_memo("이사", (now + timedelta(days=days)).replace(hour=21, minute=0), now,
                                                   reason="r", memo_id="j-41", asked="a")
    assert memo_booked_notification(offer, now, call_language("ko"))["aps"]["alert"]["body"].startswith(ko)
    assert memo_booked_notification(offer, now, call_language("en"))["aps"]["alert"]["body"].startswith(en)


def test_a_call_after_midnight_reads_as_early_morning_in_korean(tmp_path):
    """Second review of 2026-10-17, finding 7: "내일 밤 12시 30분" for a call 40 minutes away reads as the next night."""
    from voice.offers import clock_label, when_label

    now = datetime(2026, 10, 17, 23, 50, tzinfo=SEOUL)
    soon = datetime(2026, 10, 18, 0, 30, tzinfo=SEOUL)
    assert clock_label(soon, call_language("ko")) == "새벽 12시 30분"
    assert when_label(soon, now, call_language("ko")) == "내일 새벽 12시 30분"
    assert when_label(soon + timedelta(days=1), now, call_language("ko")) == "10월 19일 월요일 새벽 12시 30분"
    assert when_label(soon, now, call_language("en")) == "12:30 AM tomorrow"
    assert clock_label(datetime(2026, 10, 17, 21, 0, tzinfo=SEOUL), call_language("ko")) == "밤 9시"


def test_the_prep_of_an_ai_call_reads_the_last_calls(tmp_path):
    """The user's amendment of 2026-10-18: the prep of a call the AI places carries the last calls too."""
    from tests.test_prep import a_record

    me = signed_in(tmp_path)
    a_record(me.workspace, "2026-10-14/2100", "2026-10-14T21:00:00+09:00", [("User", "Talk tomorrow about the offer.")],
             title="The two offers", ended="hung up")
    store = OfferStore(me.workspace)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    store.answer(offer.id, "now", datetime.now(SEOUL))
    prepared = []

    async def fake_prepare(plan, extra=None, deadline_s=None, **kw):
        prepared.append(extra)
        return Prep(note="note", opening="Hi.")

    asyncio.run(call_due(me.person, prepare=fake_prepare, place=placing([])))
    (extra,) = prepared
    assert '"The two offers": the user hung up' in extra["last_calls"]
    assert "User: Talk tomorrow about the offer." in extra["last_calls"]
