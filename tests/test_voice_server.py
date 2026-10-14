"""The voice server's call (voice/agent.py run_call), with LiveKit, the models and the voice faked
(tests/fake_call.py): who answers, when the AI picks up, and what a call leaves behind.
"""

import asyncio
import json
from datetime import datetime

import pytest

from meta.session import MetaSession
from tests.fake_call import FakeCall, FakeSession, use_fakes
from tests.helpers import signed_in
from tests.test_prep import Converse, text
from voice import agent, prep as prep_module
from voice.ringback import STATE_ATTRIBUTE

OPENING = "<opening>Hi Matt, what's on your mind?</opening>"
NOTE = "<note>Matt is weighing a job offer.</note>"


def call_folders(workspace):
    return [p for p in workspace.glob("*/*") if p.is_dir() and (p / "events.jsonl").exists()]


def events_of(folder) -> list[dict]:
    return [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]


@pytest.fixture
def quick(monkeypatch):
    """Seconds shortened so a test call takes a fraction of a second."""
    monkeypatch.setattr(agent, "RING_SECONDS", 0.3)
    monkeypatch.setattr(agent, "OPENING_BY_S", 0.5)
    monkeypatch.setattr(agent, "FIRST_WORDS_WAIT_S", 0.5)


def with_prep_model(monkeypatch, client) -> None:
    """The coordinator's model writes the opening and the note (scripted)."""
    monkeypatch.setattr(agent, "COORDINATOR_LLM", "bedrock")
    monkeypatch.setattr(agent, "prep_model", lambda mode: ("zai.glm-5", "ap-southeast-2"))
    monkeypatch.setattr(prep_module, "bedrock_client", lambda region: client)


def test_a_call_the_user_starts_is_picked_up_with_the_prepared_opening(tmp_path, monkeypatch, quick):
    me = signed_in(tmp_path)
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert call.session.said == ["Hi Matt, what's on your mind?"] and call.ringing == ["start", "stop"]
    assert call.room.local_participant.attributes == {STATE_ATTRIBUTE: "answered"}
    (folder,) = call_folders(me.workspace)
    kinds = [e["type"] for e in events_of(folder)]
    assert kinds[0] == "call_started" and "picked_up" in kinds and kinds[-1] == "call_ended"
    assert [e["kind"] for e in MetaSession(me.workspace).take_events()] == ["call_ended"]


def test_an_opening_not_ready_in_time_falls_back_to_the_fixed_greeting(tmp_path, monkeypatch, quick):
    """Confirmed rule (2026-10-15): an opening not ready within 6 s falls back to the fixed greeting."""
    me = signed_in(tmp_path)
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE), gap=0.2))  # the opening takes about a second
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert call.session.said == ["Hi, it's Bee Call. What's on your mind?"]


def test_an_ai_call_unanswered_in_time_is_missed_and_not_called_again(tmp_path, monkeypatch, quick):
    """Confirmed rule (2026-10-15): an AI call unanswered for 60 s is recorded as missed and not called again;
    the meta agent hears of it."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the interview",
                                "reason": "booked in a call"})
    use_fakes(monkeypatch, call)

    async def main():
        await agent.run_call(call, me.people)  # the phone is never answered
        await call.end()

    asyncio.run(main())
    assert call.deleted and call.session.started is None and call_folders(me.workspace) == []
    (event,) = MetaSession(me.workspace).take_events()
    assert (event["kind"], event["topic"], event["room"]) == ("call_missed", "the interview", call.room.name)


def spend_today(workspace, usd: float) -> None:
    call = workspace / datetime.now().strftime("%Y-%m-%d") / "0001"
    call.mkdir(parents=True, exist_ok=True)
    (call / "usage.json").write_text(json.dumps({"models": [
        {"type": "llm_usage", "provider": "aws", "model": "zai.glm-5", "input_tokens": int(usd * 1_000_000)}]}))
    (call / "events.jsonl").write_text('{"t": 0.0, "type": "call_started"}\n{"t": 60.0, "type": "call_ended"}\n')


def test_a_call_the_user_starts_past_the_budget_is_not_answered(tmp_path, monkeypatch, quick):
    """Server review finding 3: the app's pass to a room lasts two hours, so the voice server checks the budget
    too, when a call the user started begins."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
    spend_today(me.workspace, 0.5)
    before = call_folders(me.workspace)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert call.session.started is None and call.ringing == [] and call.deleted  # the app's call ends
    assert call_folders(me.workspace) == before and MetaSession(me.workspace).take_events() == []


def test_the_budget_is_checked_only_before_a_call_starts_and_never_cuts_one(tmp_path, monkeypatch, quick):
    """Confirmed rule (2026-10-15): the budget is checked only before a call starts; a call in progress is not cut,
    and the AI's call, checked before it was placed, is answered whatever the spend is by then."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
    for meta in ({"direction": "inbound", "person": "matt"},
                 {"direction": "outbound", "person": "matt", "topic": "the interview"}):
        call = FakeCall(me.people, meta)
        use_fakes(monkeypatch, call)

        async def main():
            call.join()
            if meta["direction"] == "outbound":
                spend_today(me.workspace, 1.0)  # past the budget between placing and answering
            await agent.run_call(call, me.people)
            spend_today(me.workspace, 1.0)  # and past it during the call
            await asyncio.sleep(0.2)
            return call.session.started is not None, call.session.closed, call.deleted

        assert asyncio.run(main()) == (True, False, False)
        asyncio.run(call.end())


def test_livekit_never_records_a_call_and_audio_follows_the_setting_at_answer_time(tmp_path, monkeypatch, quick):
    """Server review finding 6 and contract section 1: LiveKit's own recording is off in code, whatever the LiveKit
    project says; our own recorder keeps audio by record_on_server, read again when the phone answers."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the interview"})
    use_fakes(monkeypatch, call)

    async def main():
        task = asyncio.create_task(agent.run_call(call, me.people))
        await asyncio.sleep(0.05)  # dispatched, ringing; the app sends the person's settings as they answer
        me.people.change_settings("matt", {"record_on_server": False})
        call.join()
        await task
        await call.end()

    asyncio.run(main())
    assert call.session.started["record"] is False
    (folder,) = call_folders(me.workspace)
    assert not (folder / "audio").exists()
    assert next(e for e in events_of(folder) if e["type"] == "coordinator")["audio_on_server"] is False


# ---------- people without Bee (BC-057): the mode the user's friend uses ----------

BEE_WORDING = r"\bBee\b(?! Call)"  # "Bee" the wearable; "Bee Call" is the product's own name


@pytest.fixture
def bee_log(tmp_path, monkeypatch):
    """The fake Bee CLI, keeping every call made to it."""
    from tests.test_beelink import FAKE_BEE

    log = tmp_path / "bee-calls.jsonl"
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(log))
    return log


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_a_call_without_bee_reads_nothing_from_bee_and_never_mentions_it(tmp_path, monkeypatch, quick, bee_log,
                                                                         direction):
    import re
    from livekit.agents.llm.tool_context import get_fnc_tool_names

    me = signed_in(tmp_path)  # Use Bee is off unless the person connected Bee
    assert me.people.settings("matt").bee is False
    client = Converse(text(OPENING + NOTE))
    with_prep_model(monkeypatch, client)
    meta = {"direction": direction, "person": "matt"}
    if direction == "outbound":
        meta.update(topic="the job offer", reason="The user asked to talk the offer through.",
                    prep={"note": "Matt is deciding on a job offer.", "opening": "Hi Matt!"})
    call = FakeCall(me.people, meta)
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert not bee_log.exists()  # the Bee CLI never ran
    (folder,) = call_folders(me.workspace)
    assert not (folder / "inputs").exists()
    assert [e for e in events_of(folder) if e["type"] in ("error", "why_calling")] == []
    coordinator = call.session.started["agent"]
    tools = get_fnc_tool_names(coordinator.tools)
    assert not [t for t in tools if "bee" in t] and {"delegate", "book_call", "end_call"} <= set(tools)
    assert re.findall(BEE_WORDING, coordinator.instructions) == []
    for request in client.requests:  # the prep, for the call the user started
        assert "toolConfig" not in request
        assert re.findall(BEE_WORDING, request["system"][0]["text"] + json.dumps(request["messages"])) == []
