"""A call in text (voice/text_call.py, outside test seam 3): the voice server's own call with the person's lines
typed, without LiveKit media or AWS speech. The call AI here is the echo stand-in, so nothing needs a network."""

import asyncio
import json
from pathlib import Path

from meta.session import MetaSession
from tests.helpers import SEOUL, signed_in
from voice import agent, call_view, text_call


def test_a_call_in_text_is_a_call_with_its_record_its_view_and_its_end(tmp_path, monkeypatch):
    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "COORDINATOR_LLM", "echo")
    monkeypatch.setattr(agent, "WORKER", "fake")
    said: list[str] = []
    found = asyncio.run(text_call.text_call(me.people, "matt", ["Hello there", "Tell me about my day."],
                                            say=said.append))
    record = Path(found["record"])
    assert record.parent.parent == me.workspace and found["call_id"].startswith("call-matt-")
    events = [json.loads(line) for line in (record / "events.jsonl").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "call_started" and kinds[-1] == "call_ended" and "session_closed" in kinds
    view = call_view.call_view(me.workspace, found["call_id"], zone=SEOUL, language="en", now=me.person.now())
    assert view["call"]["state"] == "ended"
    speech = [(i["speaker"], i["text"]) for i in view["items"] if i["type"] == "speech"]
    assert [text for who, text in speech if who == "person"] == ["Hello there", "Tell me about my day."]
    assert [who for who, _ in speech][0] == "ai"  # the AI picked up first
    assert said[0].startswith("AI: ") and "matt: Hello there" in said
    assert MetaSession(me.workspace).take_events()[-1]["kind"] == "call_ended"


def test_the_stand_ins_test_job_starts_a_job_in_a_call_in_text(tmp_path, monkeypatch):
    """Found by the first call in text: the echo and fake call AIs asked for "test job" with only a task, after
    delegate came to take a title too (design D), so their job never started."""
    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "WORKER", "fake")
    for mode in ("echo", "fake"):
        monkeypatch.setattr(agent, "COORDINATOR_LLM", mode)
        found = asyncio.run(text_call.text_call(me.people, "matt", ["test job"], say=lambda line: None))
        events = [json.loads(line) for line in (Path(found["record"]) / "events.jsonl").read_text().splitlines()]
        started = [e for e in events if e["type"] == "job_started"]
        assert started and started[0]["title"], mode
