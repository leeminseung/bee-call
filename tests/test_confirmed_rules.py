"""Today's behaviour that the user confirmed as the rule on 2026-10-15 (DECISIONS, "지금 동작을 규칙으로 굳힘"), pinned
before the fixes of that day change the code around it.

No network: the prep, placing a call, APNs and the models are fakes here.
"""

import asyncio
import json
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from meta.session import add_event, quiet
from tests.helpers import SEOUL, another_conversation, placing, signed_in
from tests.test_offers import asked
from voice import outbound, voip_push
from voice.offers import OfferStore
from voice.outbound import call_due, send_held, send_offer, serve_meta
from voice.prep import Prep
from voice.token_server import make_app

NIGHT = datetime(2026, 10, 14, 23, 30, tzinfo=SEOUL)


async def prepared(plan, **kw):
    return Prep(note="n", opening="o")


def test_quiet_hours_hold_only_offer_notifications_and_agreed_calls_ring_at_their_time(tmp_path, monkeypatch):
    sent, rooms = [], []

    async def alert(payload, person, **kw):
        sent.append(payload)
        return "apns-1"

    monkeypatch.setattr(voip_push, "alert", alert)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    assert quiet(NIGHT)
    question, _ = asked(tmp_path, now=NIGHT, workspace=me.workspace)
    assert asyncio.run(send_offer(question, me.person, NIGHT)).startswith("held: quiet hours")
    booked = store.book("the interview", NIGHT, NIGHT - timedelta(hours=2))
    assert asyncio.run(call_due(me.person, NIGHT, prepare=prepared, place=placing(rooms))) == [rooms[0].name]
    assert sent == [] and store.get(booked.id).state == "called" and store.get(question.id).state == "asked"


def spend(workspace, usd: float, now: datetime) -> None:
    """A finished call today that cost exactly `usd` (GLM 5 input: 1.00 USD per million tokens)."""
    call = workspace / now.strftime("%Y-%m-%d") / "0001"
    call.mkdir(parents=True)
    (call / "usage.json").write_text(json.dumps({"models": [
        {"type": "llm_usage", "provider": "aws", "model": "zai.glm-5", "input_tokens": int(usd * 1_000_000)}]}))


def test_spend_equal_to_the_budget_counts_as_over(tmp_path):
    from voice.costs import spent_today

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
    spend(me.workspace, 0.5, datetime.now(SEOUL))
    assert spent_today(me.workspace, datetime.now(SEOUL)) == 0.5  # exactly the budget

    async def main():
        async with TestClient(TestServer(make_app("wss://example", "APIkey", "s" * 32, me.people)),
                              headers=me.headers) as client:
            return (await client.get("/token")).status

    assert asyncio.run(main()) == 429


def test_the_meta_agent_skips_thinking_while_over_budget_and_catches_up_the_next_day(tmp_path, monkeypatch):
    from voice.people import Person

    me = signed_in(tmp_path)
    workspace = me.workspace
    me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
    today = datetime(2026, 10, 15, 14, 0, tzinfo=SEOUL)
    spend(workspace, 0.5, today)
    clock = {"now": today}
    monkeypatch.setattr(Person, "now", lambda self: clock["now"])  # the person's clock, which the loop reads
    thought: list[list[str]] = []

    class Thinker:
        async def think(self, events, now):
            thought.append([e["kind"] for e in events])
            return {"seconds": 0.0, "decision": "nothing to do"}

    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)

    async def main():
        add_event(workspace, "call_ended", now=today, call="2026-10-15/1300")
        loop = asyncio.create_task(serve_meta(me.person, Thinker()))
        await asyncio.sleep(0.1)
        waited = list(thought)
        clock["now"] = today + timedelta(days=1)  # the next day: nothing spent yet
        await asyncio.sleep(0.1)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        return waited

    assert asyncio.run(main()) == [] and thought == [["call_ended"]]


def test_the_meta_agent_wakes_only_for_events(tmp_path, monkeypatch):
    me = signed_in(tmp_path)
    workspace = me.workspace
    store = OfferStore(workspace)
    thought: list[list[str]] = []

    class Thinker:
        async def think(self, events, now):
            thought.append([e["kind"] for e in events])
            return {"seconds": 0.0, "decision": "nothing to do"}

    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)
    question, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=workspace)  # an open question is no event
    soon = store.book("the interview", datetime.now(SEOUL) + timedelta(minutes=9), datetime.now(SEOUL))
    later = store.book("the dentist", datetime.now(SEOUL) + timedelta(minutes=30), datetime.now(SEOUL))

    async def main():
        loop = asyncio.create_task(serve_meta(me.person, Thinker()))
        await asyncio.sleep(0.15)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(main())
    assert thought == [["booked_soon"]]  # once, for the call ten minutes ahead, and nothing on a clock
    (event,) = [json.loads(l) for l in (workspace / "meta" / "events.jsonl").read_text().splitlines()]
    assert (event["offer"], event["topic"]) == (soon.id, "the interview") and later.id != soon.id


def test_an_unanswered_offer_closes_at_its_suggested_time(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 14, 18, 30, tzinfo=SEOUL))  # suggests 21:00
    assert [o.id for o in store.open(datetime(2026, 10, 14, 20, 59, 59, tzinfo=SEOUL))] == [offer.id]
    assert store.open(datetime(2026, 10, 14, 21, 0, tzinfo=SEOUL)) == [] and store.get(offer.id).state == "expired"


def test_tapping_now_twice_places_one_call(tmp_path):
    me = signed_in(tmp_path)
    offer, _ = asked(tmp_path, now=datetime.now(SEOUL), workspace=me.workspace)
    rooms = []

    async def main():
        async with TestClient(TestServer(make_app("wss://example", "APIkey", "s" * 32, me.people)),
                              headers=me.headers) as client:
            first = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            second = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            return first.status, second.status

    assert asyncio.run(main()) == (200, 409)
    for _ in range(2):
        asyncio.run(call_due(me.person, prepare=prepared, place=placing(rooms)))
    assert len(rooms) == 1 and OfferStore(me.workspace).get(offer.id).state == "called"


def test_the_fifth_offer_of_a_day_shows_only_in_the_app_list(tmp_path, monkeypatch):
    sent = []

    async def alert(payload, person, **kw):
        sent.append(payload["offer"])
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    morning = datetime(2026, 10, 15, 8, 0, tzinfo=SEOUL)
    question, plan = asked(tmp_path, now=morning, workspace=me.workspace)
    for i in range(4):  # 40 minutes apart: a notification waits 30 minutes after the last one (2026-10-15)
        at = morning + timedelta(minutes=40 * i)
        offer = question if i == 0 else store.ask(another_conversation(plan, i), at)
        assert asyncio.run(send_offer(offer, me.person, at)).startswith("notification sent")
    fifth = store.ask(another_conversation(plan, 9), morning + timedelta(hours=3))
    assert asyncio.run(send_offer(fifth, me.person, morning + timedelta(hours=3))).startswith("held: 4 notifications")
    assert [o.id for o in store.open(morning + timedelta(hours=3, minutes=1))] == [fifth.id]  # what the app lists
    for moment in (morning + timedelta(hours=3), datetime(2026, 10, 16, 8, 0, tzinfo=SEOUL)):
        asyncio.run(send_held(me.person, moment))
    assert fifth.id not in sent and len(sent) == 4


def test_a_background_job_fails_after_forty_rounds(tmp_path):
    from tests.test_harness import FakeHarness, assistant, fake_search
    from worker.harness import MAX_ROUNDS, HarnessWorker
    from worker.runner import JobRunner

    assert MAX_ROUNDS == 40
    rounds = [assistant(("tool", f"t{i}", "progress", ['{"text": "still working"}'])) for i in range(MAX_ROUNDS)]
    news = []

    async def main():
        async def on_notify(n):
            news.append((n.status, n.error))

        runner = JobRunner(tmp_path, lambda name, **f: None,
                           HarnessWorker("arn:harness", client=FakeHarness(rounds), search=fake_search), on_notify,
                           clock=lambda: datetime.now(SEOUL))
        runner.start("Never finishes")
        await asyncio.gather(*runner.tasks.values())
        return runner.jobs["01"].status

    assert asyncio.run(main()) == "failed"
    assert news == [("failed", "the worker did not finish within 40 rounds")]
