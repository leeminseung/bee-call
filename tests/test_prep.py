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
    assert seed.endswith("they called, so let them bring their own topic. Keep the note to about 70 words, never more than 100. The call is in English: write the opening in English, whatever language the notes, the earlier calls or Bee are in.")
    out = seed_text(CallInputs(), {"direction": "outbound", "reason": "A Bee conversation just ended.",
                                   "topic": "Planned a hike"}, NOW, bee=True)
    assert "You are about to call the user. Why: A Bee conversation just ended. (topic: Planned a hike)." in out
    assert out.endswith("and ask if they have a minute. Keep the note to about 70 words, never more than 100. The call is in English: write the opening in English, whatever language the notes, the earlier calls or Bee are in.")


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
    assert second[-1]["content"][-1] == {"text": "Now write the note, about 70 words."}


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
    assert client.requests[1]["messages"][-1]["content"][-1] == {
        "text": "Now write the opening and the note, about 70 words."}


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


def test_what_to_read_names_the_last_few_calls_the_next_message_holds():
    """2026-10-18: the code passes the last few calls in the seed and "What you are doing" names them; "What to read",
    the list of what the next message holds, now names them too, with Bee or without."""
    from voice.prep import PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE, WHAT_TO_READ

    assert WHAT_TO_READ in PREP_INSTRUCTIONS
    assert WHAT_TO_READ.startswith(
        "## What to read\nThe next message holds who is calling whom and why, the meta agent's notes (open threads and "
        "the agenda for this call), your last few calls with the user, and what changed in Bee since the last call. "
        "Start from those.")
    assert ("## What to read\nThe next message holds who is calling whom and why, the notes kept between calls and your "
            "last few calls with the user.") in PREP_INSTRUCTIONS_WITHOUT_BEE


def test_the_prep_is_told_to_compact_what_came_before_into_what_this_call_must_know():
    """The user's texts (2026-10-18) for "What you are doing" and "Your note"; the opening and "What to read" stay.
    Measured that day, 1 of 8 notes kept under 100 words: "Your note" now asks for one short line each, shows a note of
    the right length for another user and asks for a count before finishing, and the seed (before the call's language,
    which stays last) and the ask after the reads say the limit too."""
    from voice.prep import PREP_INSTRUCTIONS

    assert ("## What you are doing\nYou are the voice of Bee Call, which the user talks with on the phone. A call is "
            "about to start, and the phone is ringing on their side while you get ready. In these seconds you compact "
            "what came before into what this call must know: from the meta agent's notes, the last few calls and what "
            "changed in Bee since the last call, pick the context this call cannot do without, the way a good "
            "assistant glances at their notes before answering.") in PREP_INSTRUCTIONS
    assert ("## Your note\nThen write <note>...</note>: about 70 words and never more than 100, for yourself during the call. It "
            "is the context "
            "this call must know, compacted from what came before; leave out whatever this call will not need. Include "
            "what applies, one short line each:\n- where things stand: the threads still open from recent calls, and "
            "how the last call ended;\n- what the user has been up to, from Bee since the last call, only what this "
            "call may touch;\n- what the user wants from calls like this one: tone, topics to avoid, things they asked "
            "not to repeat;\n- what this call could get to know about the user, from the meta agent's notes;\n- the "
            "unconfirmed facts, to-dos and suggestions to check, with their Bee ids.\nOnly what the data says. Call "
            "the user \"the user\" in it, not by name and never he, she, his or her. A note of the right length, for "
            "another user:\n<note>- Open: moving to Busan in March") in PREP_INSTRUCTIONS
    assert "</note>\nCount before you finish: past 100 words, cut what this call needs least." in PREP_INSTRUCTIONS
    from voice.prep import PREP_INSTRUCTIONS_WITHOUT_BEE, seed_text
    from beelink.context import CallInputs

    example = PREP_INSTRUCTIONS[PREP_INSTRUCTIONS.index("<note>- Open"):PREP_INSTRUCTIONS.index("</note>\nCount")]
    assert len(example.split()) < 70  # the example is well under the limit
    assert "past 100 words, cut what this call needs least" in PREP_INSTRUCTIONS_WITHOUT_BEE
    for bee in (True, False):
        assert "own topic. Keep the note to about 70 words, never more than 100. The call is in English" in seed_text(
            CallInputs(), {"direction": "inbound"}, NOW, bee=bee)


def test_the_seed_asks_for_the_opening_in_the_call_s_language_whatever_the_earlier_calls_are_in():
    """Measured 2026-10-18 (Haiku 5.5, thinking off): with the last calls' Korean lines in its input, the prep wrote a
    Korean opening for 14 of 17 English calls. The seed's last words name the call's language."""
    from voice.language import call_language

    for code, name in (("en", "English"), ("ko", "Korean")):
        seed = seed_text(CallInputs(), {"direction": "inbound", "last_calls": "User: 내일 다시 얘기하자."}, NOW, bee=True,
                         language=call_language(code))
        assert seed.endswith(f"The call is in {name}: write the opening in {name}, whatever language the notes, the "
                             "earlier calls or Bee are in."), code


# ---------- the user's decision of 2026-10-18: what Bee already has, listed after the prep note ----------

def bee_data(tmp_path, monkeypatch, facts: int = 3, todos: int = 2, suggestions: int = 2, more: bool = False,
             unconfirmed: int = 2) -> None:
    """Bee's lists as the fake answers them: confirmed and unconfirmed facts made a minute apart (the newest has the
    highest id), open and done to-dos, and suggestions."""
    base = 1792150000000
    answers = {
        "facts list": {"facts": [{"id": 300 + n, "text": f"Fact {n}", "created_at": base + n * 60000, "confirmed": True,
                                  "tags": []} for n in range(facts)], "next_cursor": "50" if more else None},
        "facts list --unconfirmed": {"facts": [{"id": 700 + n, "text": f"Guess {n}", "created_at": base + n * 60000,
                                                "confirmed": False, "tags": []} for n in range(unconfirmed)],
                                     "next_cursor": None},
        "todos list": {"todos": [{"id": 400 + n, "text": f"To-do {n}", "created_at": base + n * 60000,
                                  "completed": n % 2 == 1} for n in range(2 * todos)], "next_cursor": None},
        "todos suggestions": {"todoSuggestions": [{"id": 900 + n, "text": f"Suggestion {n}", "created_at": base + n,
                                                   "emoji": "", "details": ""} for n in range(suggestions)]},
    }
    (tmp_path / "bee-data.json").write_text(json.dumps(answers))
    monkeypatch.setenv("FAKE_BEE_DATA", str(tmp_path / "bee-data.json"))


def test_what_bee_has_lists_its_facts_open_to_dos_and_suggestions_with_their_ids(fake_bee, tmp_path, monkeypatch):
    from voice.prep import what_bee_has

    bee_data(tmp_path, monkeypatch)
    has = asyncio.run(what_bee_has(None, NOW.tzinfo, wait_s=5))
    assert has.text == ("What Bee already has, as read when the call began, each with its Bee id:\n"
                        "Confirmed facts:\n- 302: Fact 2\n- 301: Fact 1\n- 300: Fact 0\n"
                        "Unconfirmed facts:\n- 701: Guess 1\n- 700: Guess 0\n"
                        "Open to-dos:\n- 402: To-do 2\n- 400: To-do 0\n"
                        "To-dos Bee suggests:\n- 901: Suggestion 1\n- 900: Suggestion 0")
    assert has.listed == {"facts": 3, "unconfirmed": 2, "todos": 2, "suggestions": 2} and has.missing == {}
    assert sorted([a for a in c if a != "--json"] for c in calls_of(fake_bee)) == [
        ["facts", "list", "--limit", "50"], ["facts", "list", "--unconfirmed", "--limit", "50"],
        ["todos", "list", "--limit", "50"], ["todos", "suggestions", "--limit", "50"]]


def test_what_bee_has_keeps_the_newest_up_to_its_caps_and_says_bee_has_more(fake_bee, tmp_path, monkeypatch):
    from voice.prep import BEE_HAS_MOST, what_bee_has

    bee_data(tmp_path, monkeypatch, facts=40, todos=35, suggestions=20, more=True, unconfirmed=20)
    has = asyncio.run(what_bee_has(None, NOW.tzinfo, wait_s=5))
    assert has.listed == BEE_HAS_MOST == {"facts": 30, "unconfirmed": 15, "todos": 30, "suggestions": 15}
    assert "Confirmed facts (the newest; Bee has more):\n- 339: Fact 39\n" in has.text and "- 309: Fact 9" not in has.text
    assert "Unconfirmed facts (the newest; Bee has more):\n- 719: Guess 19\n" in has.text
    assert "- 705: Guess 5\n" in has.text and "- 704: Guess 4" not in has.text  # the newest 15 of 20
    assert "Open to-dos (the newest; Bee has more):\n- 468: To-do 68\n" in has.text
    assert "- 469:" not in has.text  # done to-dos are not open
    assert "To-dos Bee suggests (the newest; Bee has more):\n- 919: Suggestion 19\n" in has.text


def test_what_bee_has_leaves_out_a_part_that_fails_or_is_late_and_says_so(fake_bee, tmp_path, monkeypatch):
    from voice.prep import what_bee_has

    bee_data(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_BEE_DOWN", "todos suggestions")
    monkeypatch.setenv("FAKE_BEE_SLOW", "facts list --limit 3")  # the confirmed facts; the unconfirmed come in time
    started = time.monotonic()
    has = asyncio.run(what_bee_has(None, NOW.tzinfo, wait_s=0.8))
    assert time.monotonic() - started < 2.0  # the late read is not waited for
    assert "Confirmed facts: not read in time, so not listed." in has.text
    assert "To-dos Bee suggests: Bee could not be read, so not listed." in has.text
    assert "Unconfirmed facts:\n- 701: Guess 1" in has.text and "Open to-dos:\n- 402: To-do 2" in has.text
    assert has.missing == {"facts": "not read in time", "suggestions": "Bee could not be read"}
    monkeypatch.setenv("FAKE_BEE_DOWN", "facts list --unconfirmed")  # the unconfirmed facts alone fail
    monkeypatch.delenv("FAKE_BEE_SLOW")
    has = asyncio.run(what_bee_has(None, NOW.tzinfo, wait_s=5))
    assert "Unconfirmed facts: Bee could not be read, so not listed." in has.text
    assert "Confirmed facts:\n- 302: Fact 2" in has.text and has.missing == {"unconfirmed": "Bee could not be read"}


def test_what_bee_has_reads_its_four_parts_at_once_within_one_time_budget(monkeypatch):
    """The unconfirmed facts are read beside the other three, not after them: four reads of 0.3 s take about 0.3 s, and
    the prep's wait holds for all four together."""
    from voice.prep import what_bee_has

    asked = []

    async def read(name, args, *, ours, zone, timeout, max_chars):
        asked.append((name, args, timeout))
        await asyncio.sleep(0.3)
        key = "todoSuggestions" if args.get("status") == "suggested" else "todos" if name == "bee_todos" else "facts"
        return toolkit.Read("", {key: [{"id": len(asked), "text": f"{name} {args.get('status')}"}]})

    monkeypatch.setattr(toolkit, "read", read)
    started = time.monotonic()
    has = asyncio.run(what_bee_has(None, NOW.tzinfo, wait_s=5))
    assert time.monotonic() - started < 0.55
    assert has.listed == {"facts": 1, "unconfirmed": 1, "todos": 1, "suggestions": 1}
    assert ("bee_facts", {"status": "unconfirmed", "limit": 50}, 5) in asked and len(asked) == 4
    assert "Unconfirmed facts:\n- " in has.text and "bee_facts unconfirmed" in has.text


def test_what_bee_has_leaves_out_what_bee_made_of_our_calls_as_the_toolkit_does(fake_bee, tmp_path, monkeypatch):
    from voice.prep import what_bee_has

    bee_data(tmp_path, monkeypatch)
    ours = SimpleNamespace(kept_facts=lambda items: [i for i in items if i["id"] not in (302, 701)],
                           others=lambda items: [i for i in items if i["id"] != 901])
    has = asyncio.run(what_bee_has(ours, NOW.tzinfo, wait_s=5))
    assert "302" not in has.text and "701" not in has.text and "901" not in has.text
    assert "- 301: Fact 1" in has.text and "- 700: Guess 0" in has.text


def calls_of(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def listed_turn(tmp_path, has_after: float | None):
    """A Bee call picked up with its prep note; what Bee has lands `has_after` seconds into the first turn (0: before
    it; None: never). Returns the instructions in the first turn's context and how long the turn waited."""
    from voice.prep import BeeHas

    async def main():
        loop = asyncio.get_running_loop()
        has: asyncio.Future = loop.create_future()
        listed = BeeHas(text="What Bee already has, as read when the call began, each with its Bee id:\n"
                             "Open to-dos:\n- 401: Book a dentist appointment", listed={"todos": 1})
        if has_after == 0:
            has.set_result(listed)
        agent = BeeCoordinator(CallRecorder(tmp_path, room="r", now=NOW), SimpleNamespace(), tmp_path,
                               clock=lambda: NOW, prep_note="Matt is weighing a job offer.", bee_has=has)
        async with AgentSession(llm=EchoLLM()) as session:
            await session.start(agent)
            turn_ctx = agent.chat_ctx.copy()
            if has_after:
                loop.call_later(has_after, has.set_result, listed)
            started = time.monotonic()
            await agent.on_user_turn_completed(turn_ctx, turn_ctx.add_message(role="user", content="Hey."))
            waited = time.monotonic() - started
            await asyncio.sleep(0.05)
            return turn_ctx.items[0].text_content, agent.instructions, waited

    return asyncio.run(main())


def test_what_bee_has_is_listed_after_the_prep_note_never_in_the_fixed_prefix(tmp_path):
    from voice.coordinator import BEE_INSTRUCTIONS

    turn, later, waited = listed_turn(tmp_path, has_after=0)
    assert turn.endswith("Call prep note (from Bee, just before the call):\nMatt is weighing a job offer.\n\n"
                         "What Bee already has, as read when the call began, each with its Bee id:\n"
                         "Open to-dos:\n- 401: Book a dentist appointment\n")
    assert later == turn and waited < 0.1
    assert "401: Book a dentist appointment" not in BEE_INSTRUCTIONS  # the cached prefix is the same in every call


def test_the_first_turn_waits_a_little_for_what_bee_has(tmp_path):
    turn, _, waited = listed_turn(tmp_path, has_after=0.4)
    assert turn.endswith("- 401: Book a dentist appointment\n") and 0.35 < waited < 1.0


def test_what_bee_has_not_read_by_the_first_turn_is_said_in_one_line(tmp_path):
    from voice.coordinator import BEE_HAS_LATE

    turn, later, waited = listed_turn(tmp_path, has_after=None)
    assert turn.endswith(f"Matt is weighing a job offer.\n\n{BEE_HAS_LATE}\n") and later == turn and 1.9 < waited < 2.3
    assert BEE_HAS_LATE == "What Bee already has could not be read in time for this call, so it is not listed."
