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
