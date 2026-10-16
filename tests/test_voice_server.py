"""The voice server's call (voice/agent.py run_call), with LiveKit, the models and the voice faked
(tests/fake_call.py): who answers, when the AI picks up, and what a call leaves behind.
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime

import pytest

from meta.session import MetaSession
from tests.fake_call import FakeCall, FakeSession, use_fakes
from tests.helpers import SEOUL, as_sent, signed_in
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
    monkeypatch.setattr(agent, "RINGING_AT_MOST_S", 1.0)


def with_prep_model(monkeypatch, client) -> None:
    """The prep's model writes the opening and the note (scripted), as the bedrock mode runs it."""
    from tests.helpers import converse

    monkeypatch.setattr(agent, "COORDINATOR_LLM", "bedrock")
    monkeypatch.delenv("COORDINATOR_MODEL", raising=False)
    converse(monkeypatch, client)


def test_whose_call_it_is_and_what_it_runs_on_is_decided_without_livekit(tmp_path):
    """Server review 17: run_call's first step on its own: a call for nobody approved is not answered, and a call
    for an approved person runs on their settings as they are when it starts."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"language": "ko", "call_model": "sonnet"})
    assert agent.caller_for({"person": "ghost"}, me.people) is None
    caller = agent.caller_for({"person": "matt"}, me.people)
    assert (caller.id, caller.language.code, caller.call_model.model, caller.uses_bee) == (
        "matt", "ko", "claude-sonnet-5-5", False)
    assert caller.workspace == me.workspace and caller.tts_voice.describe()["provider"] == "polly"


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
    assert call.room.local_participant.attributes == {STATE_ATTRIBUTE: "answered", "bee_call.captions": "1"}
    (folder,) = call_folders(me.workspace)
    kinds = [e["type"] for e in events_of(folder)]
    assert kinds[0] == "call_started" and "picked_up" in kinds and kinds[-1] == "call_ended"
    assert [e["kind"] for e in MetaSession(me.workspace).take_events()] == ["call_ended"]


def test_the_caller_hears_ringing_for_at_most_about_8_seconds(tmp_path, monkeypatch, quick):
    """BC-025: the opening is ready within 6 s and the ringing lasts at most about 8 s. The ringing went on after
    the opening's 6 s for up to 6 s more while the voice had not started."""
    me = signed_in(tmp_path)
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE), gap=0.2))  # the opening misses its time
    monkeypatch.setattr(agent, "RINGING_AT_MOST_S", 0.8)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=None))
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert call.session.said == ["Hi, it's Bee Call. What's on your mind?"]  # the fixed greeting, at 0.5 s
    assert call.rang < 0.8 + 0.15


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


def test_an_ai_call_s_opening_never_waits_for_the_bee_read_kept_for_the_record(tmp_path, monkeypatch, quick):
    """BC-022: the AI's call was prepared before it rang; Bee is read again only for the record (and, without a
    prep note, for the plain Bee context). Answered while that read was slow, the AI said nothing for up to 4 s."""
    import time
    from beelink.context import CallInputs

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the interview",
                                "kind": "voice note"})
    use_fakes(monkeypatch, call)

    async def slow_read(*a, **kw):
        await asyncio.sleep(1.5)
        return CallInputs(facts=[{"id": 7, "text": "Bonus is 15%"}])

    monkeypatch.setattr(agent, "fetch_call_inputs", slow_read)
    said_at = []
    real_say = call.session.say
    call.session.say = lambda text: (said_at.append(time.monotonic()), real_say(text))

    async def main():
        running = asyncio.create_task(agent.run_call(call, me.people))
        await asyncio.sleep(0.05)
        answered = time.monotonic()
        call.join()
        await running
        await asyncio.sleep(1.7)  # the read lands meanwhile
        instructions = call.session.started["agent"].instructions
        await call.end()
        return answered, instructions

    answered, instructions = asyncio.run(main())
    assert said_at and said_at[0] - answered < 0.3
    assert "Bonus is 15%" in instructions  # the plain Bee context still reached the AI, once read
    (folder,) = call_folders(me.workspace)
    assert (folder / "inputs" / "unconfirmed_facts.json").exists()  # and the record keeps the read
    (said,) = [json.loads(l) for l in (folder / "events.jsonl").read_text().splitlines() if '"type": "call"' in l]
    assert (said["topic"], said["kind"]) == ("the interview", "voice note")  # an AI call without a topic falls back by it


def test_a_booked_call_is_told_to_the_ai_as_its_own_call_continuing_the_earlier_one(tmp_path, monkeypatch, quick):
    """BC-042: a call booked out loud continues the call it was booked in; the AI places it, so it must not be told
    the user started it."""
    me = signed_in(tmp_path)
    earlier = me.workspace / "2026-10-15" / "2100"
    earlier.mkdir(parents=True)
    (earlier / "events.jsonl").write_text(json.dumps({"t": 0, "type": "call_started", "room": "r"}) + "\n")
    (earlier / "transcript.md").write_text("- 00:05.0 **User:** Call me back about the dentist at nine.\n")
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the dentist",
                                "continue_from": "2026-10-15/2100"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        instructions = call.session.started["agent"].instructions
        await call.end()
        return instructions

    instructions = asyncio.run(main())
    assert "The user started this call" not in instructions
    assert "You placed this call, as the user booked it in an earlier call with you" in instructions
    assert "dentist" in instructions  # the earlier call, as it was recorded


def test_a_call_continued_by_the_earlier_call_s_id_records_that_call_s_folder(tmp_path, monkeypatch, quick):
    """The D app continues a call by its call id (contract section 7); the record names the earlier call by its folder
    (<date>/<time>), as the records page links to it and the call view reads it."""
    me = signed_in(tmp_path)
    earlier = me.workspace / "2026-10-15" / "2100"
    earlier.mkdir(parents=True)
    (earlier / "events.jsonl").write_text(json.dumps({"t": 0, "type": "call_started", "room": "call-matt-2100"}) + "\n")
    (earlier / "transcript.md").write_text("- 00:05.0 **User:** Let's talk about the dentist.\n")
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt", "continue_from": "call-matt-2100"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        instructions = call.session.started["agent"].instructions
        await call.end()
        return instructions

    assert "dentist" in asyncio.run(main())
    (folder,) = [f for f in call_folders(me.workspace) if f != earlier]
    (said,) = [json.loads(l) for l in (folder / "events.jsonl").read_text().splitlines() if '"type": "call"' in l]
    assert said["continue_from"] == "2026-10-15/2100"


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
    assert call.deleted == call.room_name and call.session.started is None and call_folders(me.workspace) == []
    (event,) = MetaSession(me.workspace).take_events()
    assert (event["kind"], event["topic"], event["room"]) == ("call_missed", "the interview", call.room_name)


def spend_today(workspace, usd: float) -> None:
    call = workspace / datetime.now(SEOUL).strftime("%Y-%m-%d") / "0001"
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
    assert call.session.started is None and call.ringing == [] and call.deleted == call.room_name  # the call ends
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
            return call.session.started is not None, call.session.closed, bool(call.deleted)

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


@pytest.mark.parametrize("how", ["missed", "over budget", "hung up while it rang"])
def test_a_call_that_never_starts_a_session_gives_livekit_nothing_to_record(tmp_path, monkeypatch, quick, how):
    """Review of this round's fixes, finding 8: when the LiveKit project records, LiveKit keeps the logs of a job
    that never reaches session.start and uploads them when the job ends (livekit/agents/job.py _on_cleanup). The
    voice server tells it, before anything else, to keep nothing."""
    me = signed_in(tmp_path)
    if how == "over budget":
        me.people.change_settings("matt", {"daily_budget_usd": 0.5}, allowed=("daily_budget_usd",))
        spend_today(me.workspace, 0.5)
    monkeypatch.setattr(agent, "CALLER_WAIT_S", 0.1)
    call = FakeCall(me.people, {"direction": "outbound" if how == "missed" else "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        if how == "over budget":
            call.join()
        await agent.run_call(call, me.people)  # no one answers, the budget is used up, or the caller is gone
        await call.end()

    asyncio.run(main())
    assert call.session.started is None
    assert call.recording == {"audio": False, "traces": False, "logs": False, "transcript": False}


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


@pytest.mark.parametrize("direction,language", [("inbound", "en"), ("outbound", "en"), ("inbound", "ko")])
def test_a_call_without_bee_reads_nothing_from_bee_and_never_mentions_it(tmp_path, monkeypatch, quick, bee_log,
                                                                         direction, language):
    import re
    from livekit.agents.llm.tool_context import get_fnc_tool_names

    me = signed_in(tmp_path)  # Use Bee is off unless the person connected Bee
    me.people.change_settings("matt", {"language": language})
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


# ---------- hanging up while it rings (BC-027) ----------

def spend_entries(workspace) -> list[dict]:
    path = workspace / "state" / "spend.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("hang_up_after", [0.1, 0.45])  # while the opening is written; after it, before the voice
def test_a_call_hung_up_while_it_rings_leaves_nothing_but_its_cost(tmp_path, monkeypatch, quick, hang_up_after):
    """BC-027: hung up while it still rings, the prep stops, and no call record or call_ended event is kept. The
    prep's model use still counts (server review finding 4)."""
    from livekit.agents.metrics.usage import TTSModelUsage

    me = signed_in(tmp_path)
    client = Converse(text(OPENING + NOTE * 30), gap=0.03, usage=(5000, 300))  # the opening at about 0.1 s
    with_prep_model(monkeypatch, client)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=0.6))
    # The voice had begun on the opening when the caller left: that much speech was paid for.
    call.session.usage.model_usage = [TTSModelUsage(provider="Amazon Polly", model="neural", characters_count=40)]
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        running = asyncio.create_task(agent.run_call(call, me.people))
        await asyncio.sleep(hang_up_after)
        call.hang_up()
        await running
        await call.end()

    asyncio.run(main())
    assert call_folders(me.workspace) == [] and MetaSession(me.workspace).take_events() == []
    assert call.ringing == ["start", "stop"] and STATE_ATTRIBUTE not in call.room.local_participant.attributes
    assert call.shutdown_reason == "the caller hung up while it rang"
    (prep,) = [e for e in spend_entries(me.workspace) if e["what"] == "call_prep"]
    assert prep["input_tokens"] > 0 and prep["estimated"]  # stopped mid-answer: Bedrock's count never came
    voice = [e for e in spend_entries(me.workspace) if e["what"] == "call_hung_up_while_ringing"]
    assert [m["characters_count"] for e in voice for m in e["models"]] == ([40] if hang_up_after > 0.4 else [])


@pytest.mark.parametrize("prep", ["by the model", "none"])  # with no prep model the opening is ready at once
def test_a_caller_gone_before_the_voice_server_joined_leaves_nothing(tmp_path, monkeypatch, quick, prep):
    """Review of this round's fixes, finding 3: the app's join makes the room the voice server is sent to, so a
    caller not in it when the voice server looks has hung up already, and no disconnect will be seen."""
    me = signed_in(tmp_path)
    if prep == "by the model":
        with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)
    monkeypatch.setattr(agent, "CALLER_WAIT_S", 0.1)

    async def main():
        await agent.run_call(call, me.people)  # no one is in the room
        await call.end()

    asyncio.run(main())
    assert call.session.started is None and call_folders(me.workspace) == []
    assert MetaSession(me.workspace).take_events() == []
    assert call.shutdown_reason == "no caller came into the room within 0.1 s"  # told apart from a hang-up


def test_the_voice_server_waits_for_the_caller_as_long_as_the_app_rings():
    """Round 2 (pre-deploy review): a phone slow to join was given up after 5 s and then rang in silence until the
    app's own limit (CallManager.pickUpSeconds, 30 s after it joined) said Bee Call did not pick up."""
    assert agent.CALLER_WAIT_S >= 30


def test_console_mode_picks_up_at_once(tmp_path, monkeypatch, quick):
    """Review of the user-noticed fixes, 6c: LiveKit's console mode (this Mac's microphone) has no caller in a room,
    so the call waited the whole 30 s for one before it picked up."""
    import time

    me = signed_in(tmp_path)
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    monkeypatch.setattr(agent, "CALLER_WAIT_S", 5.0)  # long enough to tell a wait apart
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    call.console = True
    use_fakes(monkeypatch, call)

    async def main():
        started = time.monotonic()
        await agent.run_call(call, me.people)  # nobody ever joins
        took = time.monotonic() - started
        await call.end()
        return took

    took = asyncio.run(main())
    assert took < 2.0 and call.session.said == ["Hi Matt, what's on your mind?"]


def test_a_room_that_closes_before_the_caller_is_seen_ends_the_call_quietly(tmp_path, monkeypatch, quick):
    """LiveKit's wait for a participant raises when the room goes away; that is a caller who never came."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def closed():
        await asyncio.sleep(0.1)
        raise RuntimeError("room disconnected while waiting for participant")

    call.wait_for_participant = closed

    async def main():
        await asyncio.wait_for(agent.run_call(call, me.people), 2)
        await call.end()

    asyncio.run(main())
    assert call.session.started is None and call_folders(me.workspace) == []
    assert call.shutdown_reason.startswith("the room closed before the caller came into it")


def test_a_call_hung_up_after_the_ringing_but_before_the_ai_spoke_leaves_nothing(tmp_path, monkeypatch, quick):
    """Review of this round's fixes, finding 3: the ringing stops after RINGING_AT_MOST_S, but the watch goes on
    until the AI's first words (BC-027)."""
    me = signed_in(tmp_path)
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=None))
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)  # the ringing has stopped; the voice has not started
        call.hang_up()
        await call.end()

    asyncio.run(main())
    assert call.ringing == ["start", "stop"] and call.session.started is not None
    assert call_folders(me.workspace) == [] and MetaSession(me.workspace).take_events() == []


def test_a_hang_up_after_the_ai_spoke_keeps_the_call(tmp_path, monkeypatch, quick):
    """Only a hang-up before the AI's first words leaves nothing; after them it is an ordinary end of a call."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)  # the AI has spoken
        call.hang_up()
        await call.end()

    asyncio.run(main())
    (folder,) = call_folders(me.workspace)
    assert [e["kind"] for e in MetaSession(me.workspace).take_events()] == ["call_ended"]


def test_a_quiet_call_s_record_shows_life_while_the_call_runs(tmp_path, monkeypatch, quick):
    """Contract section 1: a call writes lines only when something happens, and a record without life for 120 s
    reads as dropped. So while the call runs its record's file time is refreshed every REFRESH_S (30 s), and two
    quiet minutes never read as dropped; it stops with the call."""
    import os

    from voice import recorder
    from voice.call_folders import call_folders

    monkeypatch.setattr(recorder, "REFRESH_S", 0.05)
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)
    long_ago = 1_000_000_000

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        (folder,) = call_folders(me.workspace)
        events = folder / "events.jsonl"
        os.utime(events, (long_ago, long_ago))
        await asyncio.sleep(0.25)
        while_running = events.stat().st_mtime
        call.hang_up()
        await call.end()
        os.utime(events, (long_ago, long_ago))
        await asyncio.sleep(0.25)
        return while_running, events.stat().st_mtime

    while_running, after_the_end = asyncio.run(main())
    assert while_running > long_ago + 1000 and after_the_end == long_ago


def test_after_the_call_its_title_is_asked_for_once_the_record_is_closed(tmp_path, monkeypatch, quick):
    """Decision 7 of the contract: the title is written after the call, beside the Bee retry, and never keeps the
    record open (voice/titles.py)."""
    from voice.call_folders import call_folders

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"language": "ko"})
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)
    asked = []

    async def title_call(folder, language, *, workspace):
        asked.append((folder, language.code, workspace, json.loads(
            (folder / "events.jsonl").read_text().splitlines()[-1])["type"]))
        return "제목"

    monkeypatch.setattr(agent, "title_call", title_call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    asyncio.run(main())
    (folder,) = call_folders(me.workspace)
    assert asked == [(folder, "ko", me.workspace, "call_ended")]


def test_the_voice_server_says_when_its_own_room_reconnected_so_the_app_stops_counting_captions(
        tmp_path, monkeypatch, quick):
    """App contract section 3, rule 6: captions are not sent while the voice server is away from the room, though
    what it hears is still recorded, so the caption numbers may no longer match. It sets bee_call.captions as it
    picks up and changes it after each reconnect of its own; the app then draws the view alone for that call."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        at_pick_up = dict(call.room.local_participant.attributes)
        call.room.emit("reconnected")
        await asyncio.sleep(0.05)
        after_one = dict(call.room.local_participant.attributes)
        call.room.emit("reconnected")
        await asyncio.sleep(0.05)
        after_two = call.room.local_participant.attributes["bee_call.captions"]
        call.hang_up()
        await call.end()
        return at_pick_up, after_one, after_two

    at_pick_up, after_one, after_two = asyncio.run(main())
    assert at_pick_up == {STATE_ATTRIBUTE: "answered", "bee_call.captions": "1"}
    assert (after_one["bee_call.captions"], after_two) == ("2", "3")


def test_claude_s_history_is_kept_when_it_answers_with_glm_5_standing_by(tmp_path, monkeypatch, quick):
    """COORDINATOR_LLM=anthropic: the session's model is the stand-by wrapper, and Claude's history (exactly what
    Claude was sent) is still kept for checking the call afterwards."""
    from tests.helpers import ScriptedLLM
    from voice.claude_llm import ClaudeBedrockLLM
    from voice.fake_claude import FakeAnthropic
    from voice.fallback_llm import FirstRequestFallback

    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    claude = ClaudeBedrockLLM(client=FakeAnthropic(), model="claude-haiku-5-5", thinking="disabled")
    call.session.llm = FirstRequestFallback(claude, lambda: ScriptedLLM([]), on_choice=lambda model, reason: None)
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    asyncio.run(main())
    (folder,) = call_folders(me.workspace)
    assert json.loads((folder / "claude-history.json").read_text()) == {"system": None, "messages": []}


def test_the_call_ai_is_the_person_s_call_model_and_the_record_names_it(tmp_path, monkeypatch, quick):
    """Setting call_model, read as the call starts like the language: "sonnet" makes the call AI Claude Sonnet 5.5,
    and the record's coordinator event names the model in use."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"call_model": "sonnet"})
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    monkeypatch.setattr(agent, "COORDINATOR_LLM", "anthropic")
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    monkeypatch.setenv("COORDINATOR_THINKING", "disabled")  # left over from before call_model: not used, and said
    asyncio.run(main())
    assert call.call_model.model == "claude-sonnet-5-5"
    (folder,) = call_folders(me.workspace)
    (coordinator,) = [e for e in events_of(folder) if e["type"] == "coordinator"]
    assert coordinator["llm"] == "anthropic" and coordinator["model"] == "claude-sonnet-5-5"
    assert coordinator["call_model"] == "sonnet" and coordinator["settings_not_used"] == ["COORDINATOR_THINKING"]


@pytest.mark.parametrize("elevenlabs_key, tags", [("an-elevenlabs-key", True), ("", False)])
def test_the_call_ai_may_write_audio_tags_only_when_its_voice_performs_them(tmp_path, monkeypatch, quick,
                                                                            elevenlabs_key, tags):
    """voice/audio_tags.py: with an ElevenLabs v4 voice the call AI is told of [laughs] and [sighs], and its voice
    gets them; without the ElevenLabs key the call speaks with Polly, so it is told to write none, and a tag it writes
    anyway is removed before speaking."""
    me = signed_in(tmp_path)
    monkeypatch.setenv("ELEVENLABS_API_KEY", elevenlabs_key)
    monkeypatch.delenv("TTS", raising=False)
    monkeypatch.delenv("ELEVENLABS_MODEL", raising=False)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        started = call.session.started["agent"]
        call.hang_up()
        await call.end()
        return started

    started = asyncio.run(main())
    assert ("[laughs]" in started.instructions and "sparingly" in started.instructions) is tags
    assert started._audio_tags is tags


def test_the_call_ai_knows_the_user_s_name_from_bee_and_from_its_opening(tmp_path, monkeypatch, quick, bee_log):
    """voice/name_guard.py: the call's replies are said without the user's name, in each form the call knows it:
    Bee's first name, and the Korean name the opening greeted with. The opening itself keeps it."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    with_prep_model(monkeypatch, Converse(text("<opening>매트 님, 안녕하세요. 무슨 일로 전화 주셨어요?</opening>" + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        started = call.session.started["agent"]
        call.hang_up()
        await call.end()
        return started

    started = asyncio.run(main())
    assert call.session.said == ["매트 님, 안녕하세요. 무슨 일로 전화 주셨어요?"]
    assert started.user_names == ("Matt", "매트")


def test_a_caller_whose_phone_drops_and_comes_back_before_the_ai_speaks_keeps_the_call(tmp_path, monkeypatch, quick):
    """Second review of this round's fixes, finding 3: LiveKit keeps the session through a dropped network and the
    caller comes back to it; that call is not one hung up while it rang."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=0.8))
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        running = asyncio.create_task(agent.run_call(call, me.people))
        await asyncio.sleep(0.1)
        call.drop()
        await asyncio.sleep(0.1)
        call.come_back()
        await running  # the ringing stops before the voice starts
        await asyncio.sleep(1.0)  # the voice starts
        await call.end()

    asyncio.run(main())
    assert call.shutdown_reason is None
    (folder,) = call_folders(me.workspace)
    assert [e["kind"] for e in MetaSession(me.workspace).take_events()] == ["call_ended"]


def test_a_caller_who_leaves_and_rejoins_before_the_ai_speaks_keeps_the_call(tmp_path, monkeypatch, quick):
    """A caller back in the room is no longer gone, however they left."""
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=0.8))
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)  # the ringing stopped; the voice has not started
        call.hang_up()
        call.come_back()
        await asyncio.sleep(1.0)  # the voice starts
        await call.end()

    asyncio.run(main())
    (folder,) = call_folders(me.workspace)
    assert [e["kind"] for e in MetaSession(me.workspace).take_events()] == ["call_ended"]


def test_the_jobs_of_a_call_left_before_the_ai_spoke_still_count(tmp_path, monkeypatch, quick):
    """Second review of this round's fixes, finding 3: such a call leaves no folder, so what its background jobs
    used goes into the spend log first (server review finding 4)."""
    from voice.costs import day_budget

    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"}, session=FakeSession(first_words_after=None))
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        (folder,) = call_folders(me.workspace)
        (folder / "jobs" / "01").mkdir(parents=True)
        (folder / "jobs" / "01" / "usage.json").write_text(json.dumps(
            {"input_tokens": 1_000_000, "output_tokens": 0, "status": "cancelled",
             "steps": [{"round_s": 1.0, "calls": [{"name": "web_search", "s": 1.0}]}]}))
        call.hang_up()
        await call.end()

    asyncio.run(main())
    assert call_folders(me.workspace) == []
    (job,) = [e for e in spend_entries(me.workspace) if e["what"] == "job_in_call_left_before_the_ai_spoke"]
    assert job["job"]["input_tokens"] == 1_000_000
    assert day_budget(me.person, me.person.now()).spent >= 1.00  # GLM 5 input: 1.00 a million tokens


def test_an_ai_call_declined_while_it_rings_is_declined_not_missed(tmp_path, monkeypatch, quick):
    """Contract section 2: after POST /calls/decline the voice server stops waiting within a few seconds and the
    meta agent hears of a declined call, not a missed one."""
    from voice.dispatch import decline_ring

    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "RING_SECONDS", 30)
    monkeypatch.setattr(agent, "DECLINE_CHECK_S", 0.05)
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the interview"})
    use_fakes(monkeypatch, call)

    async def main():
        waiting = asyncio.create_task(agent.run_call(call, me.people))
        await asyncio.sleep(0.1)
        decline_ring(me.person, call.room_name)  # what the token server does for the app
        await asyncio.wait_for(waiting, 2)
        await call.end()

    asyncio.run(main())
    assert call.deleted == call.room_name and call.session.started is None and call_folders(me.workspace) == []
    (event,) = MetaSession(me.workspace).take_events()
    assert (event["kind"], event["topic"]) == ("call_declined", "the interview")


# ---------- Bee writes in the call: after it, a retry and one notification (contract sections 5 and 6) ----------

def test_after_a_bee_call_failed_writes_are_tried_again_and_one_notification_says_what_went_in(
        tmp_path, monkeypatch, quick, bee_log):
    from beelink.writer import BeeWrites
    from tests.test_bee_writes import saved
    from voice import voip_push

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    (tmp_path / "fail-once").write_text("todos create")
    monkeypatch.setenv("FAKE_BEE_FAIL_ONCE", str(tmp_path / "fail-once"))
    sent = []

    async def alert(payload, person, **kw):
        sent.append((payload, person.id))
        return "apns"

    monkeypatch.setattr(voip_push, "alert", alert)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        coordinator = call.session.started["agent"]
        said = await saved(coordinator, "Shall I add 'note the bug' to your Bee todos?", "Yes.",
                           kind="todo_create", text="Note the bug")  # Bee is down for a moment
        await call.end()
        return said

    said = asyncio.run(main())
    assert "tried again after the call" in said
    (folder,) = call_folders(me.workspace)
    assert [w.status for w in BeeWrites(folder).all()] == ["ok"]
    results = [e["result"] for e in events_of(folder) if e["type"] == "bee_write"]
    assert results == ["failed", "ok"]  # said in the call, then written after it
    ((payload, whose),) = sent
    assert whose == "matt" and payload["aps"]["alert"]["body"] == "Added to Bee: todo 'Note the bug'"
    assert payload["aps"]["thread-id"] == "bee"


def test_an_ai_call_whose_room_drops_while_it_rings_is_missed_not_answered(tmp_path, monkeypatch, quick):
    """livekit-agents raises from wait_for_participant when the room disconnects; that is no answer."""
    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "RING_SECONDS", 30)
    monkeypatch.setattr(agent, "DECLINE_CHECK_S", 0.05)
    call = FakeCall(me.people, {"direction": "outbound", "person": "matt", "topic": "the interview"})
    use_fakes(monkeypatch, call)

    async def dropped():
        await asyncio.sleep(0.1)
        raise RuntimeError("room disconnected while waiting for participant")

    call.wait_for_participant = dropped

    async def main():
        await asyncio.wait_for(agent.run_call(call, me.people), 2)
        await call.end()

    asyncio.run(main())
    assert call.session.started is None and call_folders(me.workspace) == []
    (event,) = MetaSession(me.workspace).take_events()
    assert event["kind"] == "call_missed"


# ---------- the model log: everything the call AI's model received (the user's decision, 2026-10-15) ----------

def test_every_model_request_of_a_call_is_kept_whole_on_the_server(tmp_path, monkeypatch):
    """Whatever the model: the instructions, the whole chat with tool calls and their results, the tools offered,
    and what came back, one line per request in the call folder's model-log.jsonl."""
    from types import SimpleNamespace
    from livekit.agents import Agent, llm
    from tests.helpers import seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
    coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)

    async def model(agent_, chat_ctx, tools, model_settings):
        yield llm.ChatChunk(id="1", delta=llm.ChoiceDelta(role="assistant", content="Let me check."))
        yield llm.ChatChunk(id="1", delta=llm.ChoiceDelta(role="assistant", tool_calls=[
            llm.FunctionToolCall(name="job_status", arguments="{}", call_id="c1")]))

    monkeypatch.setattr(Agent.default, "llm_node", staticmethod(model))

    async def main():
        chat = llm.ChatContext()
        chat.add_message(role="system", content=coordinator.instructions)
        chat.add_message(role="user", content="Did Bee hear about the trip?")
        chat.insert(llm.FunctionCall(call_id="c0", name="search_bee", arguments='{"query": "trip"}'))
        chat.insert(llm.FunctionCallOutput(call_id="c0", name="search_bee", output="A trip to Busan", is_error=False))
        return [chunk async for chunk in coordinator.llm_node(chat, list(coordinator.tools), None)]

    asyncio.run(main())
    rec.close()
    (entry,) = [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]
    items = entry["chat"]
    assert items[0]["role"] == "system" and "Bee Call" in items[0]["content"][0]
    assert [i.get("type") for i in items[2:]] == ["function_call", "function_call_output"]
    assert items[3]["output"] == "A trip to Busan"
    assert "delegate" in [tool["name"] for tool in entry["tools"]]
    assert entry["reply"] == {"text": "Let me check.",
                              "tool_calls": [{"name": "job_status", "arguments": "{}", "call_id": "c1"}]}


def test_the_record_server_never_serves_the_model_log_or_the_claude_history(tmp_path, monkeypatch):
    """BC-095: both hold what the model was given, Bee's raw data included."""
    from aiohttp.test_utils import TestClient, TestServer
    from voice import session_server

    me = signed_in(tmp_path)
    folder = me.workspace / "2026-10-15" / "2100"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text('{"t": 0, "type": "call_started", "room": "call-matt-x"}\n')
    for name in ("model-log.jsonl", "claude-history.json", "transcript.md"):
        (folder / name).write_text("{}")

    async def main():
        async with TestClient(TestServer(session_server.make_app(me.people)), headers=me.headers) as client:
            return {name: (await client.get(f"/api/sessions/2026-10-15/2100/files/{name}")).status
                    for name in ("model-log.jsonl", "MODEL-LOG.JSONL", "claude-history.json", "transcript.md")}

    statuses = asyncio.run(main())
    assert statuses.pop("transcript.md") == 200 and set(statuses.values()) == {403}, statuses


def test_the_record_server_never_serves_a_call_s_audio_or_its_raw_bee_inputs(tmp_path):
    """Server review 16: the record server's refusal of audio/ and inputs/ had no test, and the fixture call that has
    both folders was never used. Served by name or by any spelling of it, they are refused; the call's transcript
    and its job files are served."""
    import shutil

    from aiohttp.test_utils import TestClient, TestServer
    from voice import session_server

    me = signed_in(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "sessions" / "2026-10-11" / "1930"
    shutil.copytree(fixture, me.workspace / "2026-10-11" / "1930")
    names = ("audio/user.wav", "AUDIO/USER.WAV", "audio/ai.wav", "inputs/conversation-5121.json",
             "Inputs/unconfirmed_facts.json", "jobs/../audio/user.wav", "jobs/%2E%2E/audio/user.wav",
             "%2E%2E/1930/audio/user.wav", "transcript.md")

    async def main():
        async with TestClient(TestServer(session_server.make_app(me.people)), headers=me.headers) as client:
            found = {name: await client.session.get(as_sent(client, f"/api/sessions/2026-10-11/1930/files/{name}"))
                     for name in names}  # sent as written: the test client would resolve the dot segments first
            return {name: (r.status, await r.read()) for name, r in found.items()}

    answers = asyncio.run(main())
    status, body = answers.pop("transcript.md")
    assert status == 200 and body
    for name, (status, body) in answers.items():
        assert status in (403, 404) and b"RIFF" not in body and b"utterances" not in body, (name, status)


def test_a_call_starts_on_an_empty_bee_day_and_keeps_its_prep_note(tmp_path, monkeypatch, quick, bee_log):
    """BC-028: a person with Use Bee on and nothing in Bee today still gets their call. BC-031: the prep note is
    kept as prep.md in the call's folder."""
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    monkeypatch.setenv("FAKE_BEE_EMPTY", "1")
    with_prep_model(monkeypatch, Converse(text(OPENING + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    asyncio.run(main())
    assert call.session.said == ["Hi Matt, what's on your mind?"]
    (folder,) = call_folders(me.workspace)
    assert "Matt is weighing a job offer." in (folder / "prep.md").read_text()
    assert any("today" in line for line in bee_log.read_text().splitlines())  # Bee was read, and was empty


def test_a_call_keeps_the_language_it_started_in(tmp_path, monkeypatch, quick):
    """BC-108: the phone sends its settings as it answers, and the voice server reads them again then (for the audio
    switch), but a language changed meanwhile applies from the next call."""
    me = signed_in(tmp_path)

    class ChangesLanguageMidway(Converse):
        def converse_stream(self, **request):
            me.people.change_settings("matt", {"language": "ko"})  # the app syncs while the call is being prepared
            return super().converse_stream(**request)

    with_prep_model(monkeypatch, ChangesLanguageMidway(text(OPENING + NOTE)))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        await call.end()

    asyncio.run(main())
    assert me.people.settings("matt").language == "ko"  # changed during the call
    (folder,) = call_folders(me.workspace)
    (coordinator,) = [e for e in events_of(folder) if e["type"] == "coordinator"]
    assert coordinator["language"] == "en" and call.session.said == ["Hi Matt, what's on your mind?"]


def test_a_call_keeps_both_voices_and_its_transcript_as_they_were_recorded(tmp_path):
    """BC-092: with "save on the server" on, the user's and the AI's audio are kept as audio/user.wav and ai.wav.
    BC-097: the transcript and the events only grow; nothing earlier is rewritten."""
    import wave

    from livekit import rtc
    from tests.helpers import seoul_now
    from voice.recorder import CallRecorder

    rec = CallRecorder(tmp_path, room="r", now=seoul_now())
    frame = rtc.AudioFrame(data=bytes(320), sample_rate=16000, num_channels=1, samples_per_channel=160)
    for _ in range(3):
        rec.user_audio.write(frame)
    rec.ai_audio.write(frame)
    rec.line("User", "Hi there.")
    before = (rec.dir / "transcript.md").read_text(), (rec.dir / "events.jsonl").read_text()
    rec.line("Assistant", "Hello!")
    rec.event("note", text="later")
    rec.close()
    with wave.open(str(rec.dir / "audio" / "user.wav")) as user, wave.open(str(rec.dir / "audio" / "ai.wav")) as ai:
        assert (user.getnframes(), ai.getnframes()) == (480, 160)
    after = (rec.dir / "transcript.md").read_text(), (rec.dir / "events.jsonl").read_text()
    assert after[0].startswith(before[0]) and after[1].startswith(before[1]) and "Hello!" in after[0]


def test_a_started_job_cannot_be_changed(tmp_path):
    """BC-127: the call AI is told a started job cannot be changed, and it has no tool that would change one."""
    from types import SimpleNamespace

    from livekit.agents.llm.tool_context import get_fnc_tool_names
    from tests.helpers import seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    agent_ = Coordinator(CallRecorder(tmp_path, room="r", now=seoul_now()), SimpleNamespace(), tmp_path,
                         clock=seoul_now)
    names = set(get_fnc_tool_names(agent_.tools))
    assert {"delegate", "job_status"} <= names and not any("job" in n and n != "job_status" for n in names)
    (delegate,) = [t for t in agent_.tools if getattr(getattr(t, "info", None), "name", None) == "delegate"]
    assert "A started job cannot be changed" in delegate.info.description


# ---------- where the time of each turn went (the user's request, 2026-10-15) ----------

def test_each_user_turn_records_where_its_time_went(tmp_path):
    """One turn_timing event per user turn, from LiveKit's per-message metrics: when the user's speech ended, the
    transcription and end-of-turn delays, the model's first token, the voice's first audio, and the total."""
    from types import SimpleNamespace
    from livekit.agents import llm
    from tests.helpers import seoul_now
    from voice.recorder import CallRecorder

    rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
    session = FakeSession()
    agent.wire_events(session, rec)

    def said(role, text, metrics):
        session.emit("conversation_item_added", SimpleNamespace(item=llm.ChatMessage(role=role, content=[text],
                                                                                    metrics=metrics)))

    said("user", "어", {"started_speaking_at": 99.0, "stopped_speaking_at": 100.0, "transcription_delay": 0.2,
                       "end_of_turn_delay": 0.8, "on_user_turn_completed_delay": 0.01})
    said("user", "그러니까 내일 회의 말이야", {"stopped_speaking_at": 104.0, "transcription_delay": 0.3,
                                      "end_of_turn_delay": 0.5})
    said("assistant", "내일 회의요?", {"started_speaking_at": 105.6, "llm_node_ttft": 0.6, "llm_node_ttfs": 0.7,
                                    "tts_node_ttfb": 0.25, "e2e_latency": 1.6})
    said("user", "응", {})  # a turn LiveKit gave no metrics for
    session.emit("close", SimpleNamespace(reason="user hung up"))
    rec.close()
    timings = [e for e in events_of(rec.dir) if e["type"] == "turn_timing"]
    assert [t["user_stopped_at"] for t in timings] == [100.0, 104.0, None]
    first, second, third = timings
    assert first["reply"] is None and first["end_of_turn_delay"] == 0.8  # the next turn came before any reply
    assert (second["transcription_delay"], second["end_of_turn_delay"], second["llm_ttft"], second["tts_ttfb"],
            second["ai_started_at"], second["speech_end_to_first_audio"]) == (0.3, 0.5, 0.6, 0.25, 105.6, 1.6)
    assert third["reply"] is None


def test_turn_timing_never_breaks_a_call(tmp_path):
    from types import SimpleNamespace
    from tests.helpers import seoul_now
    from voice.recorder import CallRecorder

    rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
    session = FakeSession()
    agent.wire_events(session, rec)
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(
        role="user", text_content="hi", interrupted=False, metrics={"stopped_speaking_at": "not a time"})))
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(
        role="assistant", text_content="Hi!", interrupted=False, metrics=object())))
    rec.close()
    assert [e["type"] for e in events_of(rec.dir)].count("turn_timing") == 1


def test_a_call_record_s_line_format_is_the_one_the_readme_documents(tmp_path, monkeypatch, quick):
    """Outside test seam 5: a call's events.jsonl, as the README's "A call's record" documents it. Each line has
    `type` and `t` (seconds since the record began); the first is call_started with the room and the start on the
    person's clock; and every event type in the call records kept as fixtures is named there."""
    import re
    from datetime import datetime

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    (documented,) = re.findall(r"- \*\*A call's record\.\*\*.*", readme)
    me = signed_in(tmp_path)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    asyncio.run(main())
    (folder,) = call_folders(me.workspace)
    lines = events_of(folder)
    assert all(isinstance(e["type"], str) and isinstance(e["t"], (int, float)) and e["t"] >= 0 for e in lines)
    first = lines[0]
    assert (first["type"], first["room"]) == ("call_started", call.room_name)
    assert datetime.fromisoformat(first["started_at"]).utcoffset() is not None
    kept = Path(__file__).parent / "fixtures" / "sessions"
    seen = {e["type"] for path in kept.glob("*/*/events.jsonl") for e in map(json.loads, path.read_text().splitlines())}
    seen |= {e["type"] for e in lines}
    assert {kind for kind in seen if f"`{kind}`" not in documented} == set()


def record_kinds_in_the_code() -> dict[str, set[str]]:
    """Every kind of line the code writes to a call's events.jsonl, read from the code itself: the recorder's event
    and after_call, the after-call title line (append_event), the Claude adapter's events (_emit, which goes to the
    recorder through on_event) and the job runner's (its emit is the recorder's event). Each kind is named in place,
    as a string, wherever one is written."""
    import ast

    repo = Path(__file__).parent.parent
    kinds: dict[str, set[str]] = {}
    for path in sorted([*(repo / "voice").rglob("*.py"), *(repo / "worker").rglob("*.py")]):
        where = str(path.relative_to(repo))
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            f, args = node.func, node.args
            named = None
            if isinstance(f, ast.Attribute) and (f.attr in ("event", "after_call", "_emit")
                                                 or (f.attr == "emit" and where == "worker/runner.py")):
                named = args[0] if args else None
            elif isinstance(f, ast.Name) and f.id == "append_event":
                named = args[1] if len(args) > 1 else None
            if named is None:
                continue
            assert isinstance(named, ast.Constant) and isinstance(named.value, str), \
                f"{where}:{node.lineno} writes a kind of record line not named in place"
            kinds.setdefault(named.value, set()).add(where)
    return kinds


def test_every_kind_of_line_the_code_writes_to_a_call_s_record_is_in_the_readme():
    """Outside test seam 5, from the code's side: the record test above sees only what an echo call and the kept
    records write, which left claude_turn, the Claude adapter's other events and job_standby out of the README
    (review of 2026-10-15, finding 4). Every kind the code can write must be named there."""
    import re

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    (documented,) = re.findall(r"- \*\*A call's record\.\*\*.*", readme)
    kinds = record_kinds_in_the_code()
    assert {"call_started", "claude_turn", "job_standby", "call_titled", "bee_write"} <= set(kinds)  # the walk works
    assert {kind: sorted(where) for kind, where in kinds.items() if f"`{kind}`" not in documented} == {}

