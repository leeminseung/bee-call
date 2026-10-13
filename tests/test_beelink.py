"""Tests for beelink against a fake `bee` executable; the real Bee account is never touched."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from beelink import context, writer
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


def test_writer_dry_run_writes_nothing(tmp_path, fake_bee):
    session = tmp_path / "workspace" / "2026-10-12" / "1530"
    write_queue(session)
    results = asyncio.run(writer.run(session, apply=False))
    assert [r["dry_run"] for r in results] == [True, True, True]
    assert not any(tuple(c[:2]) in WRITE_COMMANDS for c in calls(fake_bee))
    assert not (session / "bee-writes.jsonl").exists()


def test_writer_apply_then_rerun_skips(tmp_path, fake_bee):
    session = tmp_path / "workspace" / "2026-10-12" / "1530"
    write_queue(session)
    started = datetime.now().isoformat(timespec="seconds")
    (session / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started", "started_at": started}) + "\n")
    first = asyncio.run(writer.run(session, apply=True))
    assert all(r["ok"] for r in first)
    written = [c for c in calls(fake_bee) if tuple(c[:2]) in WRITE_COMMANDS]
    assert [c[:2] for c in written] == [["facts", "update"], ["todos", "create"], ["todos", "accept-suggestion"]]
    assert ["--alarm-at", "2026-10-13T09:00:00+09:00"] == written[1][4:6]
    # The session screen sees each result as a bee_write_done line, matched to its queued item.
    done = [json.loads(l) for l in (session / "events.jsonl").read_text().splitlines()][1:]
    assert [(d["type"], d["kind"], d["ok"]) for d in done] == [
        ("bee_write_done", "fact_update", True), ("bee_write_done", "todo_create", True), ("bee_write_done", "todo_accept", True)]
    assert done[2]["item"] == {"kind": "todo_accept", "suggestion_id": 34} and done[0]["t"] >= 0
    again = asyncio.run(writer.run(session, apply=True))
    assert again == []
    assert len((session / "events.jsonl").read_text().splitlines()) == 4
    assert len([c for c in calls(fake_bee) if tuple(c[:2]) in WRITE_COMMANDS]) == 3


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
