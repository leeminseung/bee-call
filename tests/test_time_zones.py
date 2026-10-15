"""Each person's time comes from one clock, Person.now(), in the time zone their phone sent (contract section 1;
server review finding 12: the meta agent read the process's clock, the call AI BEE_TIMEZONE).

The tests pick zones by where the real clock stands, so they hold whenever they run.
"""

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer

from meta.session import quiet
from tests.helpers import signed_in
from tests.test_meta import on_glm, ScriptedModel, tool_use
from tests.test_offers import asked
from voice import outbound, voip_push
from voice.offers import OfferStore
from voice.token_server import make_app

SEOUL = ZoneInfo("Asia/Seoul")
ZONES = ["Pacific/Kiritimati", "Asia/Seoul", "Asia/Kolkata", "Europe/London", "America/New_York",
         "America/Los_Angeles", "Pacific/Honolulu", "Pacific/Pago_Pago"]  # from UTC+14 round to UTC-11


def zone_where(test) -> str:
    """The first zone whose time now passes `test`."""
    return next(name for name in ZONES if test(datetime.now(ZoneInfo(name))))


def someone_in(tmp_path, zone: str):
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"time_zone": zone})
    return me


def test_an_answer_now_is_booked_on_the_person_s_clock(tmp_path):
    me = someone_in(tmp_path, "America/Los_Angeles")
    offer, _ = asked(tmp_path, now=me.person.now(), workspace=me.workspace)

    async def main():
        async with TestClient(TestServer(make_app("wss://x", "k", "s" * 32, me.people)), headers=me.headers) as c:
            return await (await c.post(f"/offers/{offer.id}", json={"choice": "now"})).json()

    answered = asyncio.run(main())["offer"]
    call_at = datetime.fromisoformat(OfferStore(me.workspace).get(offer.id).call_at)
    assert call_at.utcoffset() == me.person.now().utcoffset() and abs(answered["call_epoch"] - call_at.timestamp()) < 1


def test_the_meta_agent_and_its_offer_read_the_person_s_clock(tmp_path, monkeypatch):
    me = someone_in(tmp_path, "America/Los_Angeles")
    model = ScriptedModel([
        {"role": "assistant", "content": [tool_use(1, "offer_call", topic="The flat prices", why="A result is in.")]},
        {"role": "assistant", "content": [{"text": "Asked."}]}])
    thinker = outbound.Thinker(me.workspace, on_glm(model), bee=False)
    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)
    from meta.session import add_event
    add_event(me.workspace, "call_ended", now=me.person.now(), call="x")

    async def main():
        loop = asyncio.create_task(outbound.serve_meta(me.person, thinker))
        await asyncio.sleep(0.2)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(main())
    first = model.sent[0]["messages"][0]["content"][0]["text"]
    assert first.startswith("It is now ") and "(America/Los_Angeles)" in first.splitlines()[0]
    (offer,) = OfferStore(me.workspace).all()
    suggested = datetime.fromisoformat(offer.suggested_at)
    assert (suggested.hour, suggested.minute) == (21, 0) and suggested.utcoffset() == me.person.now().utcoffset()


def test_quiet_hours_are_the_person_s_own(tmp_path, monkeypatch):
    sent = []

    async def alert(payload, person, **kw):
        sent.append(person.id)
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    for zone, notified in ((zone_where(lambda now: quiet(now)), False), (zone_where(lambda now: not quiet(now)), True)):
        me = someone_in(tmp_path / zone.replace("/", "-"), zone)
        offer, _ = asked(tmp_path / zone.replace("/", "-"), now=me.person.now(), workspace=me.workspace)
        asyncio.run(outbound.send_held(me.person, me.person.now()))
        assert (OfferStore(me.workspace).get(offer.id).notified_at is not None) is notified, zone


def test_the_budget_day_is_the_person_s_day(tmp_path):
    zone = zone_where(lambda now: now.date() != datetime.now(SEOUL).date())
    me = someone_in(tmp_path, zone)
    me.people.change_settings("matt", {"daily_budget_usd": 1.0}, allowed=("daily_budget_usd",))
    for when, usd in ((datetime.now(SEOUL), 5.0), (me.person.now(), 0.25)):  # Seoul's today, then the person's
        call = me.workspace / when.strftime("%Y-%m-%d") / when.strftime("%H%M")
        call.mkdir(parents=True)
        (call / "usage.json").write_text(json.dumps({"models": [
            {"type": "llm_usage", "model": "zai.glm-5", "input_tokens": int(usd * 1_000_000)}]}))

    async def main():
        async with TestClient(TestServer(make_app("wss://x", "k", "s" * 32, me.people)), headers=me.headers) as c:
            return await (await c.get("/me")).json(), (await c.get("/token")).status

    me_now, token = asyncio.run(main())
    assert me_now["spent_today_usd"] == 0.25 and token == 200


def test_a_call_back_at_a_clock_time_is_the_person_s_clock_time(tmp_path):
    from types import SimpleNamespace
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    me = someone_in(tmp_path, "America/New_York")
    rec = CallRecorder(me.workspace, room="r", now=me.person.now())
    agent = Coordinator(rec, SimpleNamespace(), me.workspace, clock=me.person.now)
    tomorrow = (me.person.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    from tests.test_offers import user_said

    user_said(agent, "Call me tomorrow at 8 about the dentist.")
    said = asyncio.run(agent.book_call(None, topic="the dentist", asked="Call me tomorrow at 8", at=f"{tomorrow}T08:00"))
    (offer,) = OfferStore(me.workspace).all()
    call_at = datetime.fromisoformat(offer.call_at)
    assert call_at == datetime.fromisoformat(f"{tomorrow}T08:00").replace(tzinfo=ZoneInfo("America/New_York"))
    assert "8:00 AM tomorrow" in said
    assert rec.dir.parent.name == me.person.now().strftime("%Y-%m-%d")  # the call's folder is dated on their clock


def test_the_call_ai_the_prep_and_a_job_read_the_person_s_clock(tmp_path):
    from types import SimpleNamespace
    from beelink.context import CallInputs
    from voice.coordinator import Coordinator
    from voice.prep import seed_text
    from voice.recorder import CallRecorder
    from worker.runner import JobRunner

    me = someone_in(tmp_path, "Europe/London")
    rec = CallRecorder(me.workspace, room="r", now=me.person.now())
    agent = Coordinator(rec, SimpleNamespace(), me.workspace, clock=me.person.now)
    assert "(Europe/London). Use this for words like today" in agent.instructions
    assert "(Europe/London)." in seed_text(CallInputs(), {"direction": "inbound"}, me.person.now(), bee=False)

    async def main():
        class Worker:
            async def run(self, job, progress, relay):
                return f"now {job.now.tzinfo}"

        async def notify(news):
            pass

        runner = JobRunner(rec.dir, lambda *a, **k: None, Worker(), notify, clock=me.person.now)
        job = runner.start("anything")
        await asyncio.gather(*runner.tasks.values())
        return job

    assert str(asyncio.run(main()).now.tzinfo) == "Europe/London"



def test_a_test_clock_file_sets_the_person_s_time_and_it_runs_on(tmp_path, monkeypatch):
    """Outside test seam 7: Person.now() read the real clock only, so a rule that needs a time of day (the suggested
    call time, quiet hours, the day's budget) could not be checked from outside. BEE_CALL_CLOCK_FILE names a file
    holding one time with its zone: from the moment the file was written it is that time, and the clock runs on.
    Never on a server (deploy/config.py NOT_FOR_THE_SERVER)."""
    from deploy.config import NOT_FOR_THE_SERVER
    from tests.helpers import signed_in
    from voice.offers import call_time

    me = signed_in(tmp_path)
    clock = tmp_path / "clock.txt"
    clock.write_text("2026-10-15T20:55:00+09:00\n")
    monkeypatch.setenv("BEE_CALL_CLOCK_FILE", str(clock))
    now = me.person.now()
    assert abs((now - datetime(2026, 10, 15, 20, 55, tzinfo=ZoneInfo("Asia/Seoul"))).total_seconds()) < 5
    assert call_time(now).day == 16  # five minutes before 21:00: tomorrow's
    clock.write_text("2026-10-15T03:00:00+00:00")  # set again, in another zone
    assert me.person.now().hour == 12 and call_time(me.person.now()).day == 15
    clock.write_text("not a time")
    assert abs((me.person.now() - datetime.now(ZoneInfo("Asia/Seoul"))).total_seconds()) < 5  # the real clock
    assert "BEE_CALL_CLOCK_FILE" in NOT_FOR_THE_SERVER
