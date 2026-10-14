"""Tests for beelink against a fake `bee` executable; the real Bee account is never touched."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from beelink import context
from beelink.watcher import BeeWatcher, parse_stream_line

FAKE_BEE = Path(__file__).parent / "fakebee" / "bee"
WRITE_COMMANDS = {("facts", "confirm"), ("facts", "update"), ("todos", "create"), ("todos", "accept-suggestion")}


@pytest.fixture
def fake_bee(tmp_path, monkeypatch):
    log = tmp_path / "bee-calls.jsonl"
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(log))
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def write_queue(session: Path) -> None:
    session.mkdir(parents=True, exist_ok=True)
    items = [
        {"kind": "fact_update", "fact_id": 7, "text": "Bonus is 10%"},
        {"kind": "todo_create", "text": "Ask about bonus terms", "alarm_at": "2026-10-13T09:00:00+09:00"},
        {"kind": "todo_accept", "suggestion_id": 34},
    ]
    (session / "bee-queue.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))


def make_session(workspace: Path, started: datetime, seconds: float) -> None:
    folder = workspace / started.strftime("%Y-%m-%d") / started.strftime("%H%M")
    folder.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "room": "r", "started_at": started.isoformat(timespec="seconds")},
             {"t": seconds, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))


def test_watcher_skips_our_calls_and_keeps_cursor(tmp_path, fake_bee, monkeypatch):
    workspace = tmp_path / "workspace"
    our_call = datetime.fromtimestamp(time.time() - 3600)
    make_session(workspace, our_call, 600)
    inside = int((our_call.timestamp() + 120) * 1000)
    outside = int((our_call.timestamp() - 7200) * 1000)
    changed = {"meta": {"next_cursor": "cursor-2"},
               "conversations": [{"id": 1, "start_time": outside, "end_time": outside + 600000},
                                 {"id": 2, "start_time": inside, "end_time": inside + 60000}],
               "facts": [], "todos": [], "journals": [], "dailies": []}
    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps(changed))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(changed_file))

    seen = []

    async def on_trigger(trigger):
        seen.append(trigger)

    watcher = BeeWatcher(workspace, on_trigger)
    trigger = asyncio.run(watcher.catch_up("test"))
    assert trigger is not None and trigger.conversation_ids == [1]
    assert seen == [trigger]
    assert (trigger.inbox_dir / "changed.json").exists()
    assert json.loads((workspace / "state" / "bee_cursor.json").read_text())["cursor"] == "cursor-2"
    asyncio.run(watcher.catch_up("again"))
    assert ["changed", "--cursor", "cursor-2", "--json"] in calls(fake_bee)


def test_watcher_ignores_empty_batches(tmp_path, fake_bee, monkeypatch):
    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps({"meta": {"next_cursor": "c"}, "conversations": [], "facts": [],
                                        "todos": [], "journals": [], "dailies": []}))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(changed_file))

    async def on_trigger(trigger):
        raise AssertionError("no trigger expected")

    assert asyncio.run(BeeWatcher(tmp_path / "workspace", on_trigger).catch_up("test")) is None


def test_parse_stream_line():
    assert parse_stream_line('{"event": "update-conversation", "data": {"conversation": {"id": 1}}}') == (
        "update-conversation", {"conversation": {"id": 1}})
    assert parse_stream_line("not json") is None
    assert parse_stream_line("") is None


def test_context_saves_inputs_and_card(tmp_path, fake_bee):
    session = tmp_path / "workspace" / "2026-10-12" / "1530"
    session.mkdir(parents=True)
    inputs = asyncio.run(context.fetch_call_inputs(conversation_ids=[5]))
    assert inputs.errors == [] and inputs.first_name == "Matt" and inputs.today["activeTodos"][0]["id"] == 51
    assert inputs.facts[0]["id"] == 7 and inputs.suggestions[0]["id"] == 34 and [c["id"] for c in inputs.recent] == [5, 4]
    card = context.save_call_inputs(session, inputs)
    names = sorted(p.name for p in (session / "inputs").iterdir())
    assert names == ["conversation-5.json", "recent_conversations.json", "today.json", "todo_suggestions.json",
                     "transcript-5.json", "unconfirmed_facts.json"]
    assert card["conversation_id"] == 5 and card["summary"] == "Talked through a job offer"
    assert card["pending_fact_ids"] == [7]
    assert card["utterances"][0]["text"] == "They offered 15 percent bonus."


def test_a_conversation_bee_returns_as_null_does_not_break_a_call(tmp_path, fake_bee):
    from voice.prep import call_brief

    inputs = asyncio.run(context.fetch_call_inputs(conversation_ids=[999]))
    assert inputs.conversations == [(999, {}, [])] and inputs.errors == []
    card = context.save_call_inputs(tmp_path, inputs)
    assert card["conversation_id"] == 999 and card["summary"] is None
    assert call_brief(inputs, {"direction": "outbound", "reason": "A Bee conversation just ended."})


def one_conversation(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    changed = {"meta": {"next_cursor": "cursor-9"},
               "conversations": [{"id": 7, "start_time": now - 600000, "end_time": now - 60000}],
               "facts": [], "todos": [], "journals": [], "dailies": []}
    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps(changed))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(changed_file))


def test_cursor_stays_when_the_change_could_not_be_handled(tmp_path, fake_bee, monkeypatch):
    workspace = tmp_path / "workspace"
    one_conversation(tmp_path, monkeypatch)

    async def failing(trigger):
        raise RuntimeError("LiveKit is down")

    with pytest.raises(RuntimeError):
        asyncio.run(BeeWatcher(workspace, failing).catch_up("test"))
    assert not (workspace / "state" / "bee_cursor.json").exists()  # the same change comes back next time

    async def working(trigger):
        pass

    asyncio.run(BeeWatcher(workspace, working).catch_up("retry"))
    assert json.loads((workspace / "state" / "bee_cursor.json").read_text())["cursor"] == "cursor-9"


def test_a_new_event_never_cancels_a_call_being_placed(tmp_path, fake_bee, monkeypatch):
    workspace = tmp_path / "workspace"
    one_conversation(tmp_path, monkeypatch)
    finished = []

    async def slow_call(trigger):
        await asyncio.sleep(0.3)  # like the LiveKit round trip that opens the call room
        finished.append(trigger.reason)

    async def main():
        watcher = BeeWatcher(workspace, slow_call, debounce=0.05)
        watcher._schedule("first")
        await asyncio.sleep(0.15)  # past the quiet period: the first catch-up is placing its call
        watcher._schedule("second")
        await asyncio.sleep(1.0)

    asyncio.run(main())
    assert finished == ["first", "second"]
