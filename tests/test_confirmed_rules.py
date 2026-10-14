"""Today's behaviour that the user confirmed as the rule on 2026-10-15 (DECISIONS, "지금 동작을 규칙으로 굳힘"), pinned
before the fixes of that day change the code around it.

No network: the prep, placing a call, APNs and the models are fakes here.
"""

import asyncio
import json
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from meta.session import add_event, quiet
from tests.helpers import signed_in
from tests.test_offers import asked
from voice import outbound, voip_push
from voice.offers import OfferStore
from voice.outbound import call_due, send_held, send_offer, serve_meta
from voice.prep import Prep
from voice.token_server import make_app

NIGHT = datetime(2026, 10, 14, 23, 30)


async def prepared(plan, **kw):
    return Prep(note="n", opening="o")


def test_quiet_hours_hold_only_offer_notifications_and_agreed_calls_ring_at_their_time(tmp_path, monkeypatch):
    sent, placed = [], []

    async def alert(payload, workspace, **kw):
        sent.append(payload)
        return "apns-1"

    async def place(plan, workspace, **kw):
        placed.append(plan.topic)
        return f"call-{len(placed)}"

    monkeypatch.setattr(voip_push, "alert", alert)
    workspace = tmp_path / "workspace"
    store = OfferStore(workspace)
    assert quiet(NIGHT)
    question, _ = asked(tmp_path, now=NIGHT)
    assert asyncio.run(send_offer(question, workspace, NIGHT)).startswith("held: quiet hours")
    booked = store.book("the interview", NIGHT, NIGHT - timedelta(hours=2))
    assert asyncio.run(call_due(workspace, NIGHT, prepare=prepared, place=place)) == ["call-1"]
    assert sent == [] and placed == ["the interview"] and store.get(booked.id).state == "called"


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
    spend(me.workspace, 0.5, datetime.now())
    assert spent_today(me.workspace, datetime.now()) == 0.5  # exactly the budget

    async def main():
        async with TestClient(TestServer(make_app("wss://example", "APIkey", "s" * 32, me.people)),
                              headers=me.headers) as client:
            return (await client.get("/token")).status

    assert asyncio.run(main()) == 429


def test_the_meta_agent_skips_thinking_while_over_budget_and_catches_up_the_next_day(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    thought: list[list[str]] = []

    class Thinker:
        async def think(self, events, now):
            thought.append([e["kind"] for e in events])
            return {"seconds": 0.0, "decision": "nothing to do"}

    over = {"today": True}
    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)

    async def main():
        add_event(workspace, "call_ended", call="2026-10-14/2000")
        loop = asyncio.create_task(serve_meta(workspace, Thinker(), over_budget=lambda now: over["today"]))
        await asyncio.sleep(0.1)
        waited = list(thought)
        over["today"] = False  # the next day: the spend starts again from zero
        await asyncio.sleep(0.1)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        return waited

    assert asyncio.run(main()) == [] and thought == [["call_ended"]]


def test_the_meta_agent_wakes_only_for_events(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = OfferStore(workspace)
    thought: list[list[str]] = []

    class Thinker:
        async def think(self, events, now):
            thought.append([e["kind"] for e in events])
            return {"seconds": 0.0, "decision": "nothing to do"}

    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)
    question, _ = asked(tmp_path, now=datetime.now())  # an open question is no event
    soon = store.book("the interview", datetime.now() + timedelta(minutes=9), datetime.now())
    later = store.book("the dentist", datetime.now() + timedelta(minutes=30), datetime.now())

    async def main():
        loop = asyncio.create_task(serve_meta(workspace, Thinker(), over_budget=lambda now: False))
        await asyncio.sleep(0.15)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(main())
    assert thought == [["booked_soon"]]  # once, for the call ten minutes ahead, and nothing on a clock
    (event,) = [json.loads(l) for l in (workspace / "meta" / "events.jsonl").read_text().splitlines()]
    assert (event["offer"], event["topic"]) == (soon.id, "the interview") and later.id != soon.id


def test_an_unanswered_offer_closes_at_its_suggested_time(tmp_path):
    store = OfferStore(tmp_path / "workspace")
    offer, _ = asked(tmp_path, now=datetime(2026, 10, 14, 18, 30))  # suggests 21:00
    assert [o.id for o in store.open(datetime(2026, 10, 14, 20, 59, 59))] == [offer.id]
    assert store.open(datetime(2026, 10, 14, 21, 0)) == [] and store.get(offer.id).state == "expired"


def test_tapping_now_twice_places_one_call(tmp_path):
    me = signed_in(tmp_path)
    offer, _ = asked(tmp_path, now=datetime.now(), workspace=me.workspace)
    placed = []

    async def place(plan, workspace, **kw):
        placed.append(plan.topic)
        return "call-matt-20261014-210000"

    async def main():
        async with TestClient(TestServer(make_app("wss://example", "APIkey", "s" * 32, me.people)),
                              headers=me.headers) as client:
            first = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            second = await client.post(f"/offers/{offer.id}", json={"choice": "now"})
            return first.status, second.status

    assert asyncio.run(main()) == (200, 409)
    for _ in range(2):
        asyncio.run(call_due(me.workspace, prepare=prepared, place=place, person="matt"))
    assert len(placed) == 1


def test_the_fifth_offer_of_a_day_shows_only_in_the_app_list(tmp_path, monkeypatch):
    sent = []

    async def alert(payload, workspace, **kw):
        sent.append(payload["offer"])
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    me = signed_in(tmp_path)
    store = OfferStore(me.workspace)
    morning = datetime(2026, 10, 15, 8, 0)
    question, plan = asked(tmp_path, now=morning, workspace=me.workspace)
    for i in range(4):
        offer = question if i == 0 else store.ask(plan, morning + timedelta(minutes=i))
        assert asyncio.run(send_offer(offer, me.workspace, morning + timedelta(minutes=i))).startswith("notification sent")
    fifth = store.ask(plan, morning + timedelta(minutes=10))
    assert asyncio.run(send_offer(fifth, me.workspace, morning + timedelta(minutes=10))).startswith("held: 4 notifications")
    assert [o.id for o in store.open(morning + timedelta(minutes=11))] == [fifth.id]  # what the app lists
    for moment in (morning + timedelta(hours=3), datetime(2026, 10, 16, 8, 0)):
        asyncio.run(send_held(me.workspace, moment))
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
                           HarnessWorker("arn:harness", client=FakeHarness(rounds), search=fake_search), on_notify)
        runner.start("Never finishes")
        await asyncio.gather(*runner.tasks.values())
        return runner.jobs["01"].status

    assert asyncio.run(main()) == "failed"
    assert news == [("failed", "the worker did not finish within 40 rounds")]
