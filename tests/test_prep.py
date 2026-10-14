"""The call prep step: the coordinator's model reads Bee before it picks up and writes itself a note.

A scripted stand-in replaces the Bedrock Converse stream, and the fake `bee` answers the reads.
"""

import asyncio
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import AgentSession

from beelink.context import CallInputs, fetch_call_inputs
from voice.coordinator import BeeCoordinator, Coordinator
from voice.echo_llm import EchoLLM
from voice.costs import SpendLog
from voice.prep import MAX_READS, Prep, Preparation, prepare_call, seed_text
from voice.recorder import CallRecorder
from tests.test_beelink import fake_bee  # noqa: F401  (pytest fixture)

NOW = datetime(2026, 10, 13, 16, 40, tzinfo=ZoneInfo("Asia/Seoul"))
OPENING = "<opening>Hi Matt, it's Bee Call. What's on your mind?</opening>"
NOTE = "<note>\nMatt is weighing a job offer with a 15% bonus; fact 7 is unconfirmed.\n</note>"
MODEL = ("zai.glm-5", "ap-southeast-2")
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


def test_the_seed_says_who_calls_why_and_what_bee_has(fake_bee):
    seed = seed_text(inputs(fake_bee), {"direction": "inbound"}, NOW, bee=True)
    assert seed.startswith("It is now Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul). The user is calling you")
    assert "The user's first name is Matt." in seed and "They offered 15 percent bonus." in seed
    assert "[todo 51] Reply to the recruiter" in seed and "Calendar: Team offsite planning" in seed
    assert "[conversation 4]" in seed and "[unconfirmed fact 7] Bonus is 15%" in seed
    assert seed.endswith("they called, so let them bring their own topic.")
    out = seed_text(CallInputs(), {"direction": "outbound", "reason": "A Bee conversation just ended.",
                                   "topic": "Planned a hike"}, NOW, bee=True)
    assert "You are about to call the user. Why: A Bee conversation just ended. (topic: Planned a hike)." in out
    assert out.endswith("and ask if they have a minute.")


def test_opening_then_note_in_one_answer(fake_bee):
    client = Converse(text(OPENING + "\n" + NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, MODEL, spend=SPEND, bee=True, client=client, now=NOW))
    assert prep.error is None and prep.reads == [] and prep.model == "zai.glm-5"
    assert prep.opening == "Hi Matt, it's Bee Call. What's on your mind?"
    assert prep.note == "Matt is weighing a job offer with a 15% bonus; fact 7 is unconfirmed."
    (request,) = client.requests
    assert request["modelId"] == "zai.glm-5" and "toolConfig" in request
    assert request["messages"][0]["content"][0]["text"].startswith("It is now Tuesday")


def test_the_opening_is_ready_before_the_note_is_written():
    async def main():
        client = Converse(text(OPENING + " " + NOTE * 4), gap=0.02)  # the note streams for about a second
        preparation = Preparation(CallInputs(), {}, MODEL, spend=SPEND, bee=True, client=client, now=NOW).start()
        started = time.monotonic()
        opening = await preparation.opening(timeout=5)
        ready = time.monotonic() - started
        note = await preparation.done
        return opening, ready, note, time.monotonic() - started

    opening, ready, note, finished = asyncio.run(main())
    assert opening.startswith("Hi Matt") and note and ready < finished - 0.5


def test_opening_then_one_round_of_reads_then_the_note(fake_bee):
    client = Converse(
        [{"text": OPENING}, tool(0, "search_bee", {"query": "hike with Dana"}), tool(1, "bee_day", {"date": "2026-10-12"}),
         tool(2, "bee_conversation", {"conversation_id": 4}), tool(3, "bee_conversation", {"conversation_id": 5})],
        text(NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {"direction": "inbound"}, MODEL, spend=SPEND, bee=True, client=client, now=NOW))
    assert prep.note and prep.opening.startswith("Hi Matt") and len(prep.reads) == MAX_READS
    second = client.requests[1]["messages"]
    assert second[1]["content"][0] == {"text": OPENING}  # the first answer goes back as it was
    results = [b["toolResult"] for b in second[-1]["content"] if "toolResult" in b]
    assert [r["toolUseId"] for r in results] == ["t0", "t1", "t2", "t3"]  # every tool use gets a result
    assert "Planned a Saturday hike with Dana" in results[0]["content"][0]["text"]
    assert "Hiked with Dana in the morning" in results[1]["content"][0]["text"]
    assert results[3]["status"] == "error" and "at most 3 reads" in results[3]["content"][0]["text"]
    assert second[-1]["content"][-1] == {"text": "Now write the note."}


def test_reads_before_the_opening_still_give_an_opening(fake_bee):
    client = Converse([tool(0, "bee_conversation", {"conversation_id": 5})], text(OPENING + NOTE))
    prep = asyncio.run(prepare_call(inputs(fake_bee), {}, MODEL, spend=SPEND, bee=True, client=client, now=NOW))
    assert prep.opening and prep.note
    assert client.requests[1]["messages"][-1]["content"][-1] == {"text": "Now write the opening and the note."}


@pytest.mark.parametrize("reply,opening", [(text("Sure, I'm ready."), None), (text(OPENING), "Hi Matt")])
def test_an_answer_without_a_note_gives_no_note(reply, opening):
    prep = asyncio.run(prepare_call(CallInputs(), {}, MODEL, spend=SPEND, bee=True, client=Converse(reply), now=NOW))
    assert prep.note is None and prep.error == "ValueError: the answer had no <note>"
    assert (prep.opening or "").startswith(opening or "")


def test_a_slow_model_is_given_up_at_the_deadline():
    async def main():
        started = time.monotonic()
        prep = await prepare_call(CallInputs(), {}, MODEL, spend=SPEND, bee=True, client=Converse(text(OPENING + NOTE * 20), gap=0.05),
                                  deadline_s=1.0, now=NOW)
        return prep, time.monotonic() - started

    prep, waited = asyncio.run(main())
    assert waited < 1.5 and prep.note is None and prep.error == "no note within 1.0 s"
    assert prep.opening.startswith("Hi Matt")  # it came before the deadline


def test_no_model_in_this_mode_means_no_note_and_no_opening():
    prep = asyncio.run(prepare_call(CallInputs(), {}, None, spend=SPEND, bee=True, now=NOW))
    assert prep.error == "no model to prepare with in this mode" and prep.opening is None


def test_the_opening_is_spoken_text_only():
    prep = asyncio.run(prepare_call(CallInputs(), {}, MODEL, spend=SPEND, bee=True, client=Converse(
        text("<opening>Hi <b>Matt</b>,\nwhat's up?</opening><note>n</note>")), now=NOW))
    assert prep.opening == "Hi Matt , what's up?"


def test_prep_travels_in_the_dispatch_and_lands_in_the_instructions(tmp_path):
    prep = Prep(note="Matt planned a hike with Dana.", opening="Hi Matt!", reads=["search_bee {}"], seconds=4.2,
                opening_s=1.1, model="zai.glm-5")
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
        loop.call_later(arrives_after, note.set_result, "Matt is weighing a job offer.")
        agent = Coordinator(CallRecorder(tmp_path, room="r", now=NOW), SimpleNamespace(), tmp_path, clock=lambda: NOW,
                            prep_note="The user called you from the app.", pending_note=note)
        async with AgentSession(llm=EchoLLM()) as session:
            await session.start(agent)
            turn_ctx = agent.chat_ctx.copy()
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
