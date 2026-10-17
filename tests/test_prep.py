"""The call prep step: the coordinator's model reads Bee before it picks up and writes itself a note.

A scripted stand-in replaces the Bedrock Converse stream, and the fake `bee` answers the reads.
"""

import asyncio
import json
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import AgentSession

from beelink import toolkit
from beelink.context import CallInputs, fetch_call_inputs
from voice.coordinator import BeeCoordinator, Coordinator
from voice.echo_llm import EchoLLM
from voice.costs import SpendLog
from voice import model_catalog, providers
from voice.model_catalog import Role
from voice.prep import MAX_READS, Prep, Preparation, prepare_call, seed_text
from voice.providers.bedrock import Bedrock
from voice.recorder import CallRecorder
from tests.test_beelink import fake_bee  # noqa: F401  (pytest fixture)

NOW = datetime(2026, 10, 13, 16, 40, tzinfo=ZoneInfo("Asia/Seoul"))
OPENING = "<opening>Hi Matt, it's Bee Call. What's on your mind?</opening>"
NOTE = "<note>\nMatt is weighing a job offer with a 15% bonus; fact 7 is unconfirmed.\n</note>"


def glm(client) -> Role:
    """The prep on GLM 5 through Bedrock's Converse API, answered by `client` (a stand-in): the prep role's standby
    as the bedrock mode runs it, its adapter given the stand-in."""
    providers.register("converse-stand-in", Bedrock(client=client))
    standby = model_catalog.role("prep").standby
    return Role("prep", choices=(replace(standby, default=True, offering=replace(standby.offering,
                                                                                    provider="converse-stand-in")),))
SPEND = SpendLog(Path(tempfile.mkdtemp(prefix="prep-spend-")))  # where these preps record their model use


def text(t: str) -> list[dict]:
    return [{"text": t}]


def tool(n: int, name: str, args: dict) -> dict:
    return {"toolUse": {"toolUseId": f"t{n}", "name": name, "input": args}}


class Converse:
    """Streams each scripted reply as Converse events, with `gap` seconds between events, and keeps the requests.
    With `usage` (input, output tokens), each reply ends with Bedrock's usage metadata."""

    def __init__(self, *replies: list[dict], gap: float = 0.0, usage: tuple[int, int] | None = None) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.gap = gap
        self.usage = usage

    def converse_stream(self, **request):
        self.requests.append(json.loads(json.dumps(request)))
        return {"stream": self._events(self.replies.pop(0))}

    def _events(self, blocks: list[dict]):
        yield {"messageStart": {"role": "assistant"}}
        for i, block in enumerate(blocks):
            if "text" in block:
                for k in range(0, len(block["text"]), 12):  # text arrives in small pieces
                    time.sleep(self.gap)
                    yield {"contentBlockDelta": {"contentBlockIndex": i, "delta": {"text": block["text"][k:k + 12]}}}
            else:
                use = block["toolUse"]
                yield {"contentBlockStart": {"contentBlockIndex": i,
                                             "start": {"toolUse": {"toolUseId": use["toolUseId"], "name": use["name"]}}}}
                yield {"contentBlockDelta": {"contentBlockIndex": i, "delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
            yield {"contentBlockStop": {"contentBlockIndex": i}}
        stop = "tool_use" if any("toolUse" in b for b in blocks) else "end_turn"
        yield {"messageStop": {"stopReason": stop}}
        if self.usage:
            yield {"metadata": {"usage": {"inputTokens": self.usage[0], "outputTokens": self.usage[1]}}}


def inputs(fake_bee) -> CallInputs:
    return asyncio.run(fetch_call_inputs(conversation_ids=[5]))


CHANGES = {"since": datetime(2026, 10, 13, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp(), "batches": 1,
           "conversations": [{"id": 4, "short_summary": "Planned a Saturday hike with Dana", "utterances_count": 12}],
           "journals": [], "facts": [{"id": 7, "text": "Bonus is 15%", "confirmed": False}], "todos": [],
           "dailies": []}
NOTES = "## Agenda for the next call\n- Offer B's bonus\n## Worth getting to know next\n- How they decide on jobs"


def test_the_seed_says_who_calls_why_the_meta_agent_s_notes_and_what_changed_in_bee(fake_bee):
    """2026-10-17: no fixed bundle of Bee reads: the seed holds who calls whom and why, the meta agent's notes, the
    conversation behind the call, and what changed in Bee since the last call."""
    found = inputs(fake_bee)
    found.changes = CHANGES
    seed = seed_text(found, {"direction": "inbound", "meta_notes": NOTES}, NOW, bee=True)
    assert seed.startswith("It is now Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul). The user is calling you")
    assert f"In Bee's epoch milliseconds, now is {int(NOW.timestamp() * 1000)}." in seed.splitlines()[0]
    assert "The meta agent's notes, written between calls" in seed and "How they decide on jobs" in seed
    assert "They offered 15 percent bonus." in seed  # the conversation behind the call
    assert "[conversation 4]" in seed and "[fact 7, unconfirmed] Bonus is 15%" in seed  # what changed since
    assert "first name" not in seed and "Calendar:" not in seed  # no bundle of today's Bee
    assert seed.endswith("they called, so let them bring their own topic. The call is in English: write the opening in English, whatever language the notes, the earlier calls or Bee are in.")
    out = seed_text(CallInputs(), {"direction": "outbound", "reason": "A Bee conversation just ended.",
                                   "topic": "Planned a hike"}, NOW, bee=True)
    assert "You are about to call the user. Why: A Bee conversation just ended. (topic: Planned a hike)." in out
    assert out.endswith("and ask if they have a minute. The call is in English: write the opening in English, whatever language the notes, the earlier calls or Bee are in.")


def test_opening_then_note_in_one_answer(fake_bee):
    client = Converse(text(OPENING + "\n" + NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, glm(client), spend=SPEND, bee=True, now=NOW))
    # What it read, as data (contract 2.3): the conversation behind the call, then no more.
    (behind,) = prep.reads
    assert (behind["type"], behind["id"], behind["summary"]) == ("conversation", 5, "Talked through a job offer")
    assert datetime.fromisoformat(behind["at"]).utcoffset().total_seconds() == 9 * 3600
    assert prep.error is None and prep.bee is True and prep.model == "zai.glm-5"
    assert prep.opening == "Hi Matt, it's Bee Call. What's on your mind?"
    assert prep.note == "Matt is weighing a job offer with a 15% bonus; fact 7 is unconfirmed."
    (request,) = client.requests
    assert request["modelId"] == "zai.glm-5" and "toolConfig" in request
    assert request["messages"][0]["content"][0]["text"].startswith("It is now Tuesday")


def test_the_opening_is_ready_before_the_note_is_written():
    async def main():
        client = Converse(text(OPENING + " " + NOTE * 4), gap=0.02)  # the note streams for about a second
        preparation = Preparation(CallInputs(), {}, glm(client), spend=SPEND, bee=True, now=NOW).start()
        started = time.monotonic()
        opening = await preparation.opening(timeout=5)
        ready = time.monotonic() - started
        note = await preparation.done
        return opening, ready, note, time.monotonic() - started

    opening, ready, note, finished = asyncio.run(main())
    assert opening.startswith("Hi Matt") and note and ready < finished - 0.5


def test_opening_then_one_round_of_reads_then_the_note(fake_bee):
    """One round of reads with the Bee read toolkit (beelink/toolkit.py), the same tools the call AI and the meta agent
    have; each recorded as data, and a read past MAX_READS refused."""
    client = Converse(
        [{"text": OPENING}, tool(0, "bee_search", {"query": "hike with Dana", "mode": "neural"}),
         tool(1, "bee_daily", {"date": "2026-10-12"}), tool(2, "bee_conversation", {"id": 4}),
         tool(3, "bee_conversation", {"id": 5})],
        text(NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, glm(client), spend=SPEND, bee=True, now=NOW))
    assert prep.note and prep.opening.startswith("Hi Matt")
    extra = prep.reads[1:]  # after the conversation behind the call, each read the prep chose, as data
    assert len(extra) == MAX_READS and extra[:2] == [{"type": "search", "query": "hike with Dana"},
                                                     {"type": "day", "date": "2026-10-12"}]
    assert (extra[2]["type"], extra[2]["id"], extra[2]["summary"]) == ("conversation", 4, "Talked through a job offer")
    assert [t["toolSpec"]["name"] for t in client.requests[0]["toolConfig"]["tools"]] == list(toolkit.NAMES)
    second = client.requests[1]["messages"]
    assert second[1]["content"][0] == {"text": OPENING}  # the first answer goes back as it was
    results = [b["toolResult"] for b in second[-1]["content"] if "toolResult" in b]
    assert [r["toolUseId"] for r in results] == ["t0", "t1", "t2", "t3"]  # every tool use gets a result
    assert "Planned a Saturday hike with Dana" in results[0]["content"][0]["text"]
    assert "Hiked with Dana in the morning" in results[1]["content"][0]["text"]
    assert results[3]["status"] == "error" and "at most 3 reads" in results[3]["content"][0]["text"]
    assert second[-1]["content"][-1] == {"text": "Now write the note."}


def test_a_person_without_bee_has_a_prep_that_read_nothing_of_bee(fake_bee):
    client = Converse(text(OPENING + NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, glm(client), spend=SPEND, bee=False,
                                    now=NOW))
    assert (prep.reads, prep.bee) == ([], False)


def test_with_the_server_s_mode_the_prep_runs_on_claude_haiku_and_glm_5_stands_by(fake_bee, monkeypatch):
    """Decision 10 of the app contract: the prep on Claude Haiku 5.5 (no thinking: the opening is due within
    seconds), GLM 5 answering when its first request fails."""
    from tests.test_providers import Messages
    from voice.prep import prep_role
    from voice.providers.anthropic import Anthropic

    said = {"content": [{"type": "text", "text": OPENING + NOTE}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 2000, "output_tokens": 120}}
    messages = Messages((said, [OPENING, NOTE]))
    monkeypatch.setitem(providers._ADAPTERS, "anthropic", Anthropic(client=SimpleNamespace(messages=messages)))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, prep_role("anthropic"), spend=SPEND,
                                    bee=True, now=NOW))
    (sent,) = messages.sent
    assert (sent["model"], sent["thinking"], sent["max_tokens"]) == ("claude-haiku-5-5", {"type": "disabled"}, 700)
    assert prep.note and (prep.model, prep.input_tokens, prep.standby_reason) == ("claude-haiku-5-5", 2000, None)

    class Down:
        async def reply(self, *args, **kwargs):
            raise RuntimeError("Anthropic is overloaded")

    monkeypatch.setitem(providers._ADAPTERS, "anthropic", Down())
    converse = Converse(text(OPENING + NOTE))
    monkeypatch.setitem(providers._ADAPTERS, "bedrock", Bedrock(client=converse))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, prep_role("anthropic"), spend=SPEND,
                                    bee=True, now=NOW))
    assert prep.note and prep.model == "zai.glm-5" and "overloaded" in prep.standby_reason
    assert converse.requests[0]["modelId"] == "zai.glm-5"


def test_reads_before_the_opening_still_give_an_opening(fake_bee):
    client = Converse([tool(0, "bee_conversation", {"id": 5})], text(OPENING + NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {}, glm(client), spend=SPEND, bee=True, now=NOW))
    assert prep.opening and prep.note
    assert client.requests[1]["messages"][-1]["content"][-1] == {"text": "Now write the opening and the note."}


@pytest.mark.parametrize("reply,opening", [(text("Sure, I'm ready."), None), (text(OPENING), "Hi Matt")])
def test_an_answer_without_a_note_gives_no_note(reply, opening):
    prep = asyncio.run(prepare_call(CallInputs(), {}, glm(Converse(reply)), spend=SPEND, bee=True, now=NOW))
    assert prep.note is None and prep.error == "ValueError: the answer had no <note>"
    assert (prep.opening or "").startswith(opening or "")


def test_a_slow_model_is_given_up_at_the_deadline():
    async def main():
        started = time.monotonic()
        prep = await prepare_call(CallInputs(), {}, glm(Converse(text(OPENING + NOTE * 20), gap=0.05)), spend=SPEND,
                                  bee=True, deadline_s=1.0, now=NOW)
        return prep, time.monotonic() - started

    prep, waited = asyncio.run(main())
    assert waited < 1.5 and prep.note is None and prep.error == "no note within 1.0 s"
    assert prep.opening.startswith("Hi Matt")  # it came before the deadline


def test_no_model_in_this_mode_means_no_note_and_no_opening():
    prep = asyncio.run(prepare_call(CallInputs(), {}, None, spend=SPEND, bee=True, now=NOW))
    assert prep.error == "no model to prepare with in this mode" and prep.opening is None


def test_the_opening_is_spoken_text_only():
    prep = asyncio.run(prepare_call(CallInputs(), {}, glm(Converse(
        text("<opening>Hi <b>Matt</b>,\nwhat's up?</opening><note>n</note>"))), spend=SPEND, bee=True, now=NOW))
    assert prep.opening == "Hi Matt , what's up?"


def test_prep_travels_in_the_dispatch_and_lands_in_the_instructions(tmp_path):
    prep = Prep(note="Matt planned a hike with Dana.", opening="Hi Matt!", reads=[{"type": "search", "query": "hike"}],
                seconds=4.2, opening_s=1.1, model="zai.glm-5", bee=True)
    assert Prep.from_meta(json.loads(json.dumps(prep.to_meta()))) == prep
    assert Prep.from_meta(None) is None
    agent = BeeCoordinator(CallRecorder(tmp_path, room="r", now=NOW), SimpleNamespace(), tmp_path, clock=lambda: NOW,
                           prep_note=prep.note)
    assert agent.instructions.endswith("Call prep note (from Bee, just before the call):\nMatt planned a hike with Dana.\n")


def late_note_turn(tmp_path, arrives_after: float):
    """Picked up with the plain context; the note arrives `arrives_after` seconds in; the caller's first turn ends
    at once. Returns the instructions in that turn's context, the agent's instructions after the note's arrival,
    and how long the turn waited. (Text-mode runs skip on_user_turn_completed, so the hook is called directly.)"""

    async def main():
        loop = asyncio.get_running_loop()
        note: asyncio.Future = loop.create_future()
        if not arrives_after:  # it landed before the first turn
            note.set_result("Matt is weighing a job offer.")
        agent = Coordinator(CallRecorder(tmp_path, room="r", now=NOW), SimpleNamespace(), tmp_path, clock=lambda: NOW,
                            prep_note="The user called you from the app.", pending_note=note)
        async with AgentSession(llm=EchoLLM()) as session:
            await session.start(agent)
            turn_ctx = agent.chat_ctx.copy()
            # The note's clock starts with the turn, as `waited` does: a session start slowed by a loaded machine
            # (three suites and an Xcode build at once) cannot eat into the time the turn waits.
            if arrives_after:
                loop.call_later(arrives_after, note.set_result, "Matt is weighing a job offer.")
            started = time.monotonic()
            await agent.on_user_turn_completed(turn_ctx, turn_ctx.add_message(role="user", content="Hey."))
            waited = time.monotonic() - started
            await asyncio.sleep(max(0.0, arrives_after - waited) + 0.05)  # let a late note arrive
            return turn_ctx.items[0].text_content, agent.instructions, waited

    return asyncio.run(main())


def test_a_note_that_lands_before_the_first_turn_is_used(tmp_path):
    turn, later, waited = late_note_turn(tmp_path, arrives_after=0.0)
    assert turn.endswith("Matt is weighing a job offer.\n") and later == turn and waited < 0.1


def test_the_first_turn_waits_a_little_for_the_note(tmp_path):
    turn, later, waited = late_note_turn(tmp_path, arrives_after=0.4)
    assert turn.endswith("Matt is weighing a job offer.\n") and later == turn and 0.35 < waited < 1.0


def test_a_note_after_the_first_turn_changes_nothing(tmp_path):
    turn, later, waited = late_note_turn(tmp_path, arrives_after=2.5)  # past the 2 s wait
    assert turn.endswith("The user called you from the app.\n") and later == turn and 1.9 < waited < 2.3


def test_the_prep_of_a_call_a_memo_asked_for_reads_the_memo_and_bee_s_reply(fake_bee, tmp_path, monkeypatch):
    """2026-10-17: the call's prep and opening use the voice memo and Bee's reply to it (bee journals get), so the AI
    opens with what the memo was about; the memo comes before the rest of Bee in the seed."""
    memo = {"id": "j-41", "text": "오늘 밤 9시에 전화해서 이사 준비 같이 정리하자.", "state": "COMPLETED",
            "created_at": 1792150000000, "updated_at": 1792150030000,
            "aiResponse": {"message": "이사 준비 목록을 정리해 드릴게요.", "todos": ["이사 업체 알아보기"]}}
    (tmp_path / "memo.json").write_text(json.dumps(memo, ensure_ascii=False))
    monkeypatch.setenv("FAKE_BEE_JOURNAL", str(tmp_path / "memo.json"))
    got = asyncio.run(fetch_call_inputs(memo_ids=["j-41"]))
    assert [m["id"] for m in got.memos] == ["j-41"]
    seed = seed_text(got, {"direction": "outbound", "reason": "메모에서 부탁하셨어요.", "topic": "이사 준비 정리",
                           "agreed": "the user asked for this call in a voice memo"}, NOW, bee=True)
    assert "The voice memo behind this call (recorded" in seed and memo["text"] in seed
    assert "Bee's reply to it: 이사 준비 목록을 정리해 드릴게요." in seed and "Bee's to-dos from it: 이사 업체 알아보기" in seed
    assert seed.index("The voice memo behind this call") < seed.index("What changed in Bee")  # before the rest
    assert "They agreed to this call: the user asked for this call in a voice memo" in seed


# ---------- the user's amendment of 2026-10-18: the last calls, and a note that compacts what came before ----------

def a_record(workspace, folder: str, started: str, lines: list[tuple[str, str]], *, title: str | None = None,
             ended: str = "goodbye") -> None:
    """One earlier call's record: its events (started, its end) and its transcript."""
    path = workspace / folder
    path.mkdir(parents=True)
    events = [{"t": 0, "type": "call_started", "room": f"call-{folder}", "started_at": started}]
    if ended == "goodbye":
        events += [{"t": 50, "type": "end_call_requested"},
                   {"t": 51, "type": "session_closed", "reason": "CloseReason.USER_INITIATED"}]
    elif ended == "hung up":
        events.append({"t": 51, "type": "session_closed", "reason": "CloseReason.PARTICIPANT_DISCONNECTED"})
    elif ended == "dropped":
        events.append({"t": 51, "type": "session_closed", "reason": "CloseReason.JOB_SHUTDOWN"})
    events.append({"t": 52, "type": "call_ended"})
    if title:
        events.append({"t": 55, "type": "call_titled", "title": title})
    (path / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    talk = "".join(f"- 00:{n:02d}.0 **{who}:** {text}\n" for n, (who, text) in enumerate(lines))
    (path / "transcript.md").write_text("# Call\n\n" + talk)


def test_the_prep_reads_the_last_three_calls_built_from_their_records(tmp_path):
    """Each with its title, when it was, how it ended and its last few lines, from the call records (no model call);
    at most three, the newest last."""
    from voice.continuation import last_calls

    ws = tmp_path / "workspace"
    a_record(ws, "2026-10-16/0900", "2026-10-16T09:00:00+09:00", [("User", "Old news.")], title="Oldest call")
    a_record(ws, "2026-10-18/2000", "2026-10-18T20:00:00+09:00", [("User", "Saturday hike?"), ("AI", "Sounds fun.")],
             title="Planning the hike", ended="dropped")
    a_record(ws, "2026-10-19/1200", "2026-10-19T12:00:00+09:00", [("AI", "Lunch?"), ("User", "Gimbap again.")],
             title=None, ended="hung up")
    a_record(ws, "2026-10-19/2130", "2026-10-19T21:30:00+09:00",
             [("User", f"Line {n}") for n in range(8)] + [("User", "Let's pick the offer up tomorrow. Bye!"),
                                                          ("AI", "Talk tomorrow. Bye!")], title="The two job offers")
    text = last_calls(ws, NOW.tzinfo)
    assert "Oldest call" not in text  # three at most
    assert text.index("Planning the hike") < text.index("Gimbap again.") < text.index("The two job offers")
    assert '"Planning the hike"' in text and "Sun Oct 18, 8:00 PM" in text and "it dropped" in text
    assert "the user hung up" in text and "no title" in text
    assert "it ended with a goodbye, and you ended the call" in text
    assert "- The last call, Mon Oct 19, 9:30 PM, \"The two job offers\"" in text  # named as the last one
    assert "User: Let's pick the offer up tomorrow. Bye!" in text and "AI (you): Talk tomorrow. Bye!" in text
    assert "Line 1" not in text  # only its last few lines
    assert last_calls(tmp_path / "nobody", NOW.tzinfo) is None
    assert "The two job offers" not in last_calls(ws, NOW.tzinfo, skip="2026-10-19/2130")  # the call continued


def test_the_seed_carries_the_last_calls_for_a_person_with_bee_or_without(tmp_path):
    for bee in (True, False):
        seed = seed_text(CallInputs(), {"direction": "inbound", "last_calls": "Your last calls with the user ..."},
                         NOW, bee=bee)
        assert "Your last calls with the user ..." in seed


def test_the_prep_is_told_to_compact_what_came_before_into_what_this_call_must_know():
    """The user's texts (2026-10-18) for "What you are doing" and "Your note"; the opening and "What to read" stay."""
    from voice.prep import PREP_INSTRUCTIONS

    assert ("## What you are doing\nYou are the voice of Bee Call, which the user talks with on the phone. A call is "
            "about to start, and the phone is ringing on their side while you get ready. In these seconds you compact "
            "what came before into what this call must know: from the meta agent's notes, the last few calls and what "
            "changed in Bee since the last call, pick the context this call cannot do without, the way a good "
            "assistant glances at their notes before answering.") in PREP_INSTRUCTIONS
    assert ("## Your note\nThen write <note>...</note>: under 100 words, for yourself during the call. It is the context "
            "this call must know, compacted from what came before; leave out whatever this call will not need. Include "
            "what applies:\n- where things stand: the threads still open from recent calls, and how the last call "
            "ended;\n- what the user has been up to, from Bee since the last call;\n- what the user wants from calls "
            "like this one: tone, topics to avoid, things they asked not to repeat;\n- what this call could get to "
            "know about the user, from the meta agent's notes;\n- the unconfirmed facts, to-dos and suggestions to "
            "check, with their Bee ids.\nOnly what the data says. Call the user \"the user\" in it, not by name."
            ) in PREP_INSTRUCTIONS


def test_the_seed_asks_for_the_opening_in_the_call_s_language_whatever_the_earlier_calls_are_in():
    """Measured 2026-10-18 (Haiku 5.5, thinking off): with the last calls' Korean lines in its input, the prep wrote a
    Korean opening for 14 of 17 English calls. The seed's last words name the call's language."""
    from voice.language import call_language

    for code, name in (("en", "English"), ("ko", "Korean")):
        seed = seed_text(CallInputs(), {"direction": "inbound", "last_calls": "User: 내일 다시 얘기하자."}, NOW, bee=True,
                         language=call_language(code))
        assert seed.endswith(f"The call is in {name}: write the opening in {name}, whatever language the notes, the "
                             "earlier calls or Bee are in."), code
