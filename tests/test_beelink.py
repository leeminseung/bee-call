"""Tests for beelink against a fake `bee` executable; the real Bee account is never touched."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from beelink import context
from tests.helpers import SEOUL
from beelink.watcher import BeeWatcher, parse_stream_line

FAKE_BEE = Path(__file__).parent / "fakebee" / "bee"
WRITE_COMMANDS = {("facts", "confirm"), ("facts", "update"), ("todos", "create"), ("todos", "accept-suggestion")}


@pytest.fixture
def fake_bee(tmp_path, monkeypatch):
    log = tmp_path / "bee-calls.jsonl"
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(log))
    return log


def test_no_test_can_run_the_real_bee_cli():
    """A test that reads Bee without setting up the fake still gets the fake: the real `bee` on this Mac is signed in
    to the user's own Bee account (the review of 2026-10-15 found a prep test running five real reads)."""
    import os

    from beelink import cli

    assert Path(cli.bee_bin()).resolve() == FAKE_BEE.resolve()
    asyncio.run(context.fetch_call_inputs())
    assert calls(Path(os.environ["FAKE_BEE_LOG"]))  # every read went to the fake


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


def test_what_bee_made_of_our_own_call_is_never_new_context(tmp_path, fake_bee, monkeypatch):
    """BC-060: for the time of one of our calls our own record is the source; Bee's conversation, and the facts,
    todos and notes it made from it (some minutes after it ended), are left out, also from the saved batch."""
    workspace = tmp_path / "workspace"
    our_call = datetime.fromtimestamp(time.time() - 3600)
    make_session(workspace, our_call, 600)
    during, soon_after = (int((our_call.timestamp() + s) * 1000) for s in (120, 600 + 300))
    later = int((our_call.timestamp() + 600 + 3 * 3600) * 1000)
    changed = {"meta": {"next_cursor": "c2"},
               "conversations": [{"id": 2, "start_time": during, "end_time": during + 60000}],
               "facts": [{"id": 1, "text": "Said to the AI", "created_at": soon_after},
                         {"id": 2, "text": "Said to a friend", "created_at": later}],
               "todos": [{"id": 3, "text": "From our call", "created_at": during}],
               "journals": [{"id": 4, "text": "A note while on our call", "created_at": during},
                            {"id": 5, "text": "A voice note right after the call", "created_at": soon_after}],
               "dailies": []}
    (tmp_path / "changed.json").write_text(json.dumps(changed))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(tmp_path / "changed.json"))

    async def on_trigger(trigger):
        pass

    trigger = asyncio.run(BeeWatcher(workspace, on_trigger).catch_up("test"))
    assert trigger.counts == {"conversations": 0, "facts": 1, "todos": 0, "journals": 1, "dailies": 0}
    saved = json.loads((trigger.inbox_dir / "changed.json").read_text())
    assert [f["id"] for f in saved["facts"]] == [2] and saved["conversations"] == saved["todos"] == []
    # A voice note is the user's own words: Bee makes none from a conversation, so one recorded in the minutes after
    # our call is kept; only one recorded during the call is left out (the wearable heard the call).
    assert [j["id"] for j in saved["journals"]] == [5]
    assert "Said to the AI" not in json.dumps(saved, ensure_ascii=False)  # the meta agent can read this file


def test_a_call_s_bee_inputs_leave_out_what_bee_made_of_our_calls(tmp_path, fake_bee):
    """BC-060, the prep's reads: the fake's recent conversation 5 and fact 7 fall in an earlier call of ours."""
    from beelink.calls import OurCalls

    workspace = tmp_path / "workspace"
    make_session(workspace, datetime.fromtimestamp(time.time() - 3600 - 120), 3600)  # covers the fake's "an hour ago"
    plain = asyncio.run(context.fetch_call_inputs())
    assert [c["id"] for c in plain.recent] == [5, 4] and [f["id"] for f in plain.facts] == [7]
    inputs = asyncio.run(context.fetch_call_inputs(ours=OurCalls.of(workspace)))
    assert [c["id"] for c in inputs.recent] == [4]
    assert inputs.today["recentConversations"] == [] and inputs.facts == []


def test_the_call_ai_the_prep_and_the_meta_agent_read_nothing_bee_made_of_our_calls(tmp_path, fake_bee):
    """BC-060 for the reads made while working: the fake's conversation 5 and fact 7 fall in an earlier call."""
    from types import SimpleNamespace
    from meta.agent import Thinker
    from tests.helpers import seoul_now
    from voice.coordinator import BeeCoordinator
    from voice.prep import Prep, _read
    from voice.recorder import CallRecorder
    from beelink.calls import OurCalls

    workspace = tmp_path / "workspace"
    make_session(workspace, datetime.fromtimestamp(time.time() - 3600 - 120), 3600)
    rec = CallRecorder(workspace, room="r", now=seoul_now())
    coordinator = BeeCoordinator(rec, SimpleNamespace(), workspace, clock=seoul_now)
    thinker = Thinker(workspace, ("m", "r"), client=None, bee=True)

    async def main():
        today = await coordinator.bee_today(None)
        conversation = await coordinator.bee_conversation(None, 5)
        meta_today = await thinker._tool("bee_today", {}, seoul_now(), [])
        pending = await thinker._tool("bee_pending", {}, seoul_now(), [])
        read = await _read({"name": "bee_conversation", "input": {"conversation_id": 5}, "toolUseId": "t"}, Prep(),
                           seoul_now().tzinfo, ours=OurCalls.of(workspace))
        return today, conversation, meta_today, pending, read["toolResult"]["content"][0]["text"]

    today, conversation, meta_today, pending, read = asyncio.run(main())
    rec.close()
    assert "[conversation 5]" not in today and "[conversation 5]" not in meta_today
    assert "[unconfirmed fact 7]" not in pending
    for text in (conversation, read):
        assert "left out" in text and "Talked through a job offer" not in text


def test_our_call_windows_survive_a_line_still_being_written(tmp_path):
    """The call AI's Bee reads now look at our calls' records, including the call in progress, whose last line
    may be half-written when it is read (server review finding 13)."""
    from beelink.calls import our_call_windows

    workspace = tmp_path / "workspace"
    started = datetime.fromtimestamp(time.time() - 120)
    make_session(workspace, started, 60)
    events = next(workspace.glob("*/*/events.jsonl"))
    events.write_text(events.read_text().replace('{"t": 60', '{"t": 30.0, "type": "x"}\n{"t": 60')
                      + '{"t": 61.0, "type": "bee_wr')
    (window,) = our_call_windows(workspace)
    assert window[1] - window[0] == 60


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
    card = context.save_call_inputs(session, inputs, SEOUL)
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
    card = context.save_call_inputs(tmp_path, inputs, SEOUL)
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
