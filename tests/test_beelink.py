"""Tests for beelink against a fake `bee` executable; the real Bee account is never touched."""

import asyncio
import os
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from beelink import context
from tests.helpers import SEOUL
from beelink.watcher import BeeWatcher, parse_stream_line

FAKE_BEE = Path(__file__).parent / "fakebee" / "bee"
WRITE_COMMANDS = {("facts", "confirm"), ("facts", "update"), ("facts", "create"), ("facts", "delete"),
                  ("todos", "create"), ("todos", "accept-suggestion"), ("todos", "complete"),
                  ("todos", "update"), ("todos", "dismiss-suggestion")}


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
    asyncio.run(context.fetch_call_inputs(conversation_ids=[5]))
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


def test_a_call_reads_no_fixed_bundle_of_bee_only_what_it_is_about(tmp_path, fake_bee):
    """2026-10-17 (the user's decision): a call starts from the meta agent's notes and what changed in Bee since the
    last call, with no fixed bundle of Bee reads; only the conversation or voice memo behind it is read from Bee."""
    from beelink.calls import OurCalls

    workspace = tmp_path / "workspace"
    asyncio.run(context.fetch_call_inputs(ours=OurCalls.of(workspace)))
    assert calls(fake_bee) == []  # a call the user starts reads nothing from Bee before the prep
    asyncio.run(context.fetch_call_inputs(conversation_ids=[5], memo_ids=["j-1"], ours=OurCalls.of(workspace)))
    assert sorted(c[:2] for c in calls(fake_bee)) == [["conversations", "get"], ["conversations", "transcript"],
                                                     ["journals", "get"]]


def a_batch(workspace: Path, name: str, saved: float, **changed) -> None:
    """One of the watcher's saved Bee changes, inbox/<name>/changed.json, saved at `saved` (epoch seconds)."""
    folder = workspace / "inbox" / name
    folder.mkdir(parents=True)
    path = folder / "changed.json"
    path.write_text(json.dumps({"meta": {"next_cursor": "c"}, **changed}, ensure_ascii=False))
    os.utime(path, (saved, saved))


def test_what_changed_in_bee_since_the_last_call_comes_from_the_watcher_s_batches(tmp_path, fake_bee):
    """What changed since the last call began, from the watcher's saved batches (which leave out what Bee made of our
    calls): the newest version of each item, without transcripts or locations, and no Bee read for it."""
    from beelink.calls import OurCalls

    workspace = tmp_path / "workspace"
    last_call = time.time() - 3 * 3600
    make_session(workspace, datetime.fromtimestamp(last_call), 300)
    a_batch(workspace, "2026-10-17-090000", last_call - 600, facts=[{"id": 1, "text": "Before the call"}])
    a_batch(workspace, "2026-10-17-130000", last_call + 3600,
            conversations=[{"id": 12, "short_summary": "A walk", "utterances_count": 10,
                            "transcriptions": [{"utterances": [{"text": "x"}]}],
                            "primary_location": {"address": "12 Some Street"}}],
            facts=[{"id": 2, "text": "Drinks coffee", "confirmed": False}])
    a_batch(workspace, "2026-10-17-140000", last_call + 7200,
            conversations=[{"id": 12, "short_summary": "A long walk", "utterances_count": 40}],
            journals=[{"id": "j-2", "text": "Buy milk"}], todos=[{"id": 5, "text": "Book the dentist"}],
            dailies=[{"id": 9, "date": "2026-10-16", "short_summary": "Errands"}])
    inputs = asyncio.run(context.fetch_call_inputs(ours=OurCalls.of(workspace)))
    changes = inputs.changes
    assert abs(changes["since"] - last_call) < 2 and changes["batches"] == 2
    assert [(c["id"], c["short_summary"]) for c in changes["conversations"]] == [(12, "A long walk")]  # the newest
    assert "transcriptions" not in json.dumps(changes) and "Some Street" not in json.dumps(changes)
    assert [f["id"] for f in changes["facts"]] == [2] and [m["id"] for m in changes["journals"]] == ["j-2"]
    assert [t["id"] for t in changes["todos"]] == [5] and [d["id"] for d in changes["dailies"]] == [9]
    assert calls(fake_bee) == []
    first = context.changes_since(workspace, None)  # before a first call: the last FIRST_CALL_DAYS
    assert first["batches"] == 3


def test_the_call_ai_the_prep_and_the_meta_agent_read_nothing_bee_made_of_our_calls(tmp_path, fake_bee):
    """BC-060 for the reads made while working, now one toolkit for all three (beelink/toolkit.py): the fake's
    conversation 5 and fact 7 fall in an earlier call."""
    from types import SimpleNamespace
    from meta.agent import Thinker
    from tests.helpers import seoul_now
    from voice.coordinator import BeeCoordinator
    from voice.prep import Prep, _read
    from voice.providers import ToolCall
    from voice.recorder import CallRecorder
    from beelink.calls import OurCalls

    workspace = tmp_path / "workspace"
    make_session(workspace, datetime.fromtimestamp(time.time() - 3600 - 120), 3600)
    rec = CallRecorder(workspace, room="r", now=seoul_now())
    coordinator = BeeCoordinator(rec, SimpleNamespace(), workspace, clock=seoul_now)
    reads = {tool.info.name: tool for tool in coordinator.tools if tool.info.name.startswith("bee_")}
    thinker = Thinker(workspace, bee=True)

    async def main():
        today = await reads["bee_today"](raw_arguments={}, context=None)
        conversation = await reads["bee_conversation"](raw_arguments={"id": 5}, context=None)
        meta_today = await thinker._tool("bee_today", {}, seoul_now(), [])
        pending = await thinker._tool("bee_facts", {"status": "unconfirmed"}, seoul_now(), [])
        read = await _read(ToolCall("t", "bee_conversation", {"id": 5}), Prep(), seoul_now().tzinfo,
                           ours=OurCalls.of(workspace))
        return today, conversation, meta_today, pending, read.text

    today, conversation, meta_today, pending, read = asyncio.run(main())
    rec.close()
    assert '"id":5,' not in today and '"id":5,' not in meta_today
    assert "Bonus is 15%" not in pending and "1 fact Bee made of your calls with the user is left out" in pending
    for text in (conversation, read):
        assert "left out" in text and "Talked through a job offer" not in text


def test_our_call_windows_read_each_finished_call_once(tmp_path, monkeypatch):
    """Review of the user-noticed fixes, 4: every Bee read re-read every call record of the person, on the call's own
    event loop (250 ms at 300 calls, and growing). A call's window is read again only when its record changed."""
    from beelink import calls

    workspace = tmp_path / "workspace"
    for hours in (5, 4, 3):
        make_session(workspace, datetime.fromtimestamp(time.time() - hours * 3600), 600)
    reads = []
    real_read = calls.read_events
    monkeypatch.setattr(calls, "read_events", lambda path: reads.append(path) or real_read(path))
    first = calls.our_call_windows(workspace)
    second = calls.our_call_windows(workspace)
    assert second == first and len(first) == 3 and len(reads) == 3  # the second time from what was read
    latest = sorted(workspace.glob("*/*/events.jsonl"))[-1]
    with latest.open("a") as f:
        f.write(json.dumps({"t": 700.0, "type": "bee_write", "ok": True}) + "\n")  # a later line, as Bee writes add
    calls.our_call_windows(workspace)
    assert reads[3:] == [latest]


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


def test_a_cursor_save_cut_off_halfway_keeps_the_last_cursor(tmp_path, monkeypatch):
    """Server review 19: the meta session saved its cursor in one step (a new file put in place), the watcher wrote
    over its cursor file directly, so a save cut off halfway (a full disk, a kill) left half a file, and the next
    catch-up could not read where it was."""
    watcher = BeeWatcher(tmp_path / "workspace", lambda trigger: None)
    watcher._save_cursor("cursor-1")
    real_write = Path.write_text

    def cut_off(self, text, *args, **kwargs):
        real_write(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", cut_off)
    with pytest.raises(OSError):
        watcher._save_cursor("cursor-2")
    monkeypatch.setattr(Path, "write_text", real_write)
    assert watcher._load_cursor() == "cursor-1"


def test_watcher_ignores_empty_batches(tmp_path, fake_bee, monkeypatch):
    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps({"meta": {"next_cursor": "c"}, "conversations": [], "facts": [],
                                        "todos": [], "journals": [], "dailies": []}))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(changed_file))

    async def on_trigger(trigger):
        raise AssertionError("no trigger expected")

    assert asyncio.run(BeeWatcher(tmp_path / "workspace", on_trigger).catch_up("test")) is None


def test_parse_stream_line():
    # `bee stream --agent` names each event first
    assert parse_stream_line("Event update-conversation: id=1 state=COMPLETED") == "update-conversation"
    assert parse_stream_line("Event journal-created.\n") == "journal-created"
    assert parse_stream_line("Event connected: stream connected.") == "connected"
    # a JSON line whose data names no event is not an event
    assert parse_stream_line('{"conversation": {"id": 1, "state": "COMPLETED"}}') is None
    # a stand-in CLI may print a JSON object that names the event
    assert parse_stream_line('{"event": "update-conversation", "data": {"conversation": {"id": 1}}}') == (
        "update-conversation")
    assert parse_stream_line("Connecting to event stream...") is None
    assert parse_stream_line("[1, 2]") is None
    assert parse_stream_line("not json") is None
    assert parse_stream_line("") is None


def test_watcher_catches_up_on_an_event_from_bee_stream(tmp_path, fake_bee, monkeypatch):
    """A new voice memo is looked at once `bee stream` reports it, not only when the stream reconnects. The watcher
    reads `bee stream --agent`, whose lines always name the event; this stand-in's `--json` lines leave the name
    out, so the test fails if the watcher reads `--json` and relies on the data naming the event."""
    memo = {"id": 7, "text": "Can't decide between a Mac mini and a PC", "created_at": int(time.time() * 1000)}
    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps({"meta": {"next_cursor": "c1"}, "conversations": [], "facts": [],
                                        "todos": [], "journals": [memo], "dailies": []}))
    monkeypatch.setenv("FAKE_BEE_CHANGED", str(changed_file))
    stream_file = tmp_path / "stream.json"
    stream_file.write_text(json.dumps([{"event": "journal-created", "data": {"journal": {"id": 7}}}]))
    monkeypatch.setenv("FAKE_BEE_STREAM", str(stream_file))
    reasons = []

    async def watch() -> None:
        heard = asyncio.Event()

        async def on_trigger(trigger):
            reasons.append(trigger.reason)
            if trigger.reason != "connected":
                heard.set()

        task = asyncio.create_task(BeeWatcher(tmp_path / "workspace", on_trigger, debounce=0.1).run())
        try:
            await asyncio.wait_for(heard.wait(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(watch())
    assert reasons == ["connected", "journal-created"]
    assert [c[:2] for c in calls(fake_bee) if c[:1] == ["stream"]] == [["stream", "--agent"]]


def test_context_saves_inputs_and_card(tmp_path, fake_bee):
    session = tmp_path / "workspace" / "2026-10-12" / "1530"
    session.mkdir(parents=True)
    from beelink.calls import OurCalls

    inputs = asyncio.run(context.fetch_call_inputs(conversation_ids=[5], ours=OurCalls.of(tmp_path / "workspace")))
    assert inputs.errors == [] and inputs.changes["batches"] == 0
    card = context.save_call_inputs(session, inputs, SEOUL)
    names = sorted(p.name for p in (session / "inputs").iterdir())
    assert names == ["changes_since_last_call.json", "conversation-5.json", "transcript-5.json"]
    assert card["conversation_id"] == 5 and card["summary"] == "Talked through a job offer"
    assert card["pending_fact_ids"] == []
    assert card["utterances"][0]["text"] == "They offered 15 percent bonus."
    # Full times with the person's offset (contract 2.2): a quote and the conversation's start, not "HH:MM".
    from datetime import datetime
    for moment in (card["utterances"][0]["at"], card["started_at"]):
        assert datetime.fromisoformat(moment).utcoffset().total_seconds() == 9 * 3600


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
        watcher.on_event("update-conversation-summary")  # as bee stream reports them (server review 16)
        await asyncio.sleep(0.15)  # past the quiet period: the first catch-up is placing its call
        watcher.on_event("journal-created")
        watcher.on_event("journal-text")  # a piece of the memo's text: the quiet period starts over, for the memo
        await asyncio.sleep(1.0)

    asyncio.run(main())
    assert finished == ["update-conversation-summary", "journal-created"]


def test_a_voice_memo_is_looked_at_once_its_text_stopped_coming(tmp_path, fake_bee, monkeypatch):
    """2026-10-17: a memo's text arrives in pieces (journal-text) after journal-created, and Bee's reply to it later
    (journal-updated): each piece starts the quiet period over, so the catch-up sees the finished memo."""
    from beelink.watcher import STREAM_TYPES

    assert "journal-updated" in STREAM_TYPES.split(",") and "journal-text" in STREAM_TYPES.split(",")
    workspace = tmp_path / "workspace"
    one_conversation(tmp_path, monkeypatch)
    seen = []

    async def on_trigger(trigger):
        seen.append((trigger.reason, time.monotonic()))

    async def main():
        watcher = BeeWatcher(workspace, on_trigger, debounce=0.15)
        watcher.on_event("journal-created")
        for _ in range(5):
            await asyncio.sleep(0.08)  # shorter than the quiet period
            watcher.on_event("journal-text")
        last = time.monotonic()
        watcher.on_event("journal-updated")  # Bee's reply came
        await asyncio.sleep(0.6)
        return last

    last = asyncio.run(main())
    assert [reason for reason, _ in seen] == ["journal-created"] and seen[0][1] >= last + 0.14


def test_a_burst_of_events_that_never_stops_is_looked_at_within_the_longest_wait(tmp_path, fake_bee, monkeypatch):
    """Review of 2026-10-17, finding 6: with an event every 0.2 s and a 0.3 s quiet period, nothing was looked at
    until the events stopped. A conversation Bee keeps updating is looked at once the longest wait is up; a voice
    memo's pieces still start the quiet period over (above), since a memo ends."""
    workspace = tmp_path / "workspace"
    one_conversation(tmp_path, monkeypatch)
    seen = []

    async def on_trigger(trigger):
        seen.append(time.monotonic())

    async def main():
        watcher = BeeWatcher(workspace, on_trigger, debounce=0.3, longest_wait=1.0)
        started = time.monotonic()
        while time.monotonic() - started < 2.5:
            watcher.on_event("update-conversation")
            await asyncio.sleep(0.2)
        return started

    started = asyncio.run(main())
    assert seen and seen[0] - started < 1.4


def test_the_longest_wait_never_cuts_a_voice_memo_still_coming_in(tmp_path, fake_bee, monkeypatch):
    """Second review of 2026-10-17, finding 2: once the longest wait was up, a conversation event started a catch-up at
    once, while the memo's text was still coming in, and the meta agent read the memo unfinished. A catch-up the
    longest wait brings forward still waits the quiet period after the memo's last piece."""
    workspace = tmp_path / "workspace"
    one_conversation(tmp_path, monkeypatch)
    seen = []

    async def on_trigger(trigger):
        seen.append(time.monotonic())

    async def main():
        watcher = BeeWatcher(workspace, on_trigger, debounce=0.3, longest_wait=1.0)
        started = time.monotonic()
        watcher.on_event("journal-created")
        last, sent = started, []
        while time.monotonic() - started < 1.3:
            await asyncio.sleep(0.1)
            if time.monotonic() - started > 1.15 and not sent:
                sent.append(True)
                watcher.on_event("update-conversation-summary")  # past the longest wait, mid-memo
                await asyncio.sleep(0.05)  # time for a catch-up to start, before the memo's next piece
            watcher.on_event("journal-text")
            last = time.monotonic()
        await asyncio.sleep(0.8)
        return last

    last = asyncio.run(main())
    assert len(seen) == 1 and seen[0] >= last + 0.25


def test_the_bee_login_link_is_read_as_the_readme_documents(tmp_path, monkeypatch):
    """Outside test seam 8 (README, "A Bee login"): `bee login --no-wait` must exit 0 and print, on its output or
    error stream, `Authentication link: https://...`; the first such link is the one the app opens. Without one, the
    login fails (POST /bee/connect answers 502). The login runs in the person's own Bee folder (BEE_CONFIG_DIR)."""
    from beelink import cli

    script = tmp_path / "bee"
    monkeypatch.setenv("BEE_BIN", str(script))
    folder = tmp_path / "bee-login"
    for body, expected in (
            ('echo "Open this link on your phone." >&2\necho "Authentication link: https://bee.example/a" >&2\n'
             'echo "Authentication link: https://bee.example/b"\necho "$BEE_CONFIG_DIR" > "$BEE_CONFIG_DIR/where"',
             "https://bee.example/a"),
            ('echo "Waiting for approval..."', None),
            ('echo "Authentication link: https://bee.example/c"\nexit 3', None)):
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        if expected:
            assert asyncio.run(cli.start_login(folder)) == expected
            assert (folder / "where").read_text().strip() == str(folder)
        else:
            with pytest.raises(cli.BeeError):
                asyncio.run(cli.start_login(folder))


def test_a_call_s_prep_reads_one_transcription_of_a_conversation(tmp_path, fake_bee, monkeypatch):
    """Review of 2026-10-17 (booking from any Bee input), finding 3: bee conversations transcript joins every
    transcription, the live one too, so each line came twice. The conversation Bee answers with holds them; one is
    read, as the Bee CLI shows it (the first that is not live), and the conversation is kept without them."""
    said = [{"id": 1, "speaker": "Matt", "text": "Remind me about the movers.", "spoken_at": 1792229360000}]
    (tmp_path / "t.json").write_text(json.dumps({"88": said, "88:realtime": said}))
    monkeypatch.setenv("FAKE_BEE_TRANSCRIPTS", str(tmp_path / "t.json"))
    inputs = asyncio.run(context.fetch_call_inputs(conversation_ids=[88]))
    ((conv_id, conv, lines),) = inputs.conversations
    assert conv_id == 88 and [line["text"] for line in lines] == ["Remind me about the movers."]
    assert "transcriptions" not in conv
    from beelink import toolkit

    read = asyncio.run(toolkit.read("bee_transcript", {"id": 88}, ours=None, zone=SEOUL, timeout=5, max_chars=3000))
    assert read.text.count("Remind me about the movers.") == 1


def test_the_prep_and_the_plain_context_carry_what_changed_since_the_last_call(tmp_path):
    from voice.prep import call_brief, seed_text

    changes = {"since": datetime(2026, 10, 17, 21, 5, tzinfo=SEOUL).timestamp(), "batches": 2,
               "conversations": [{"id": 12, "short_summary": "A long walk", "summary": "Talked about servers.",
                                  "utterances_count": 40}],
               "journals": [{"id": "j-2", "text": "Buy milk", "aiResponse": {"message": "A reminder to buy milk."}}],
               "facts": [{"id": 2, "text": "Drinks coffee", "confirmed": False}],
               "todos": [{"id": 5, "text": "Book the dentist", "completed": False}],
               "dailies": [{"id": 9, "date": "2026-10-16", "short_summary": "Errands"}]}
    inputs = context.CallInputs(changes=changes)
    seed = seed_text(inputs, {"direction": "inbound"}, datetime(2026, 10, 18, 9, 0, tzinfo=SEOUL), bee=True)
    assert "What changed in Bee since the last call (since Oct 17, 21:05), newest first:" in seed
    for line in ("[conversation 12] time unknown, 40 lines: A long walk. Talked about servers.",
                 '[voice memo j-2]: "Buy milk" Bee\'s reply: A reminder to buy milk.', "[fact 2, unconfirmed] Drinks coffee",
                 "[todo 5] Book the dentist", "[daily summary 2026-10-16] Errands"):
        assert line in seed, line
    assert "What changed in Bee since the last call" in call_brief(inputs, {}, SEOUL)
    nothing = seed_text(context.CallInputs(changes={**changes, **{k: [] for k in context.CHANGE_KINDS}}),
                        {"direction": "inbound"}, datetime(2026, 10, 18, 9, 0, tzinfo=SEOUL), bee=True)
    assert "What changed in Bee since the last call (since Oct 17, 21:05): nothing the watcher saw." in nothing
    many = {**changes, "facts": [{"id": n, "text": f"Fact number {n} " + "x" * 300} for n in range(20)]}
    long = seed_text(context.CallInputs(changes=many), {"direction": "inbound"}, datetime.now(SEOUL), bee=True)
    assert "more changes are not shown; the Bee tools read them." in long
