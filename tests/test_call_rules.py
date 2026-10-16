"""What the call AI is told about the conversation itself, after the user's calls of 2026-10-16 (build 5): it said "민승
님" in 15 of 24 turns, offered to end a call he made because he was bored, and wrapped up right after a research job.
Whether the model follows these was measured with calls in text against the real model (2026-10-16); these pin the
words that carry each rule."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from beelink.context import CallInputs
from voice.coordinator import INSTRUCTIONS, instructions, user_is_done
from voice.language import call_language
from voice.prep import PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE, seed_text

NOW = datetime(2026, 10, 16, 13, 5, tzinfo=ZoneInfo("Asia/Seoul"))


def test_a_call_needs_no_task_and_chatting_is_a_reason_for_one():
    first = INSTRUCTIONS.split("\n\n")[0]
    assert "to chat" in first and "a call needs no task" in first
    assert "one concrete thing" not in INSTRUCTIONS and "Lead with the conclusion" not in INSTRUCTIONS
    assert "be curious" in INSTRUCTIONS and "a light opinion of your own" in INSTRUCTIONS
    assert "or answers in a word, be curious" in INSTRUCTIONS  # "Cool.", "응.": where it handed the call back


def test_the_ai_never_steers_toward_ending_and_a_goodbye_still_ends_the_call():
    for text in (instructions(False), instructions(True)):
        assert "Never end the call or suggest ending it" in text and "unless the user says they are done" in text
        assert "When the user says goodbye or that they are done, say a short goodbye and call end_call." in text


def test_the_name_is_said_at_most_once_near_the_start():
    """After the first change Haiku 5.5 still said "Take care, Minseung" at the goodbye, and in Korean put "민승 씨"
    where English says "you" (4 and 8 times in a call)."""
    assert "name at most once in the whole call, near its start" in INSTRUCTIONS
    assert "not even in a goodbye" in INSTRUCTIONS and 'never as a word for "you"' in INSTRUCTIONS
    korean = call_language("ko").rule  # the rule alone did not do it: Haiku 5.5 still wrote "민승 씨는 ..." (2 to 8 a call)
    assert 'Leave "you" unsaid' in korean and '"주말엔 뭐 하세요?", never "OO 님은 주말엔 뭐 하세요?"' in korean


def test_end_call_says_when_it_may_be_called():
    from voice.coordinator import Coordinator

    assert "only after the user said goodbye or that they are done" in Coordinator.end_call.info.description


def test_after_a_job_s_result_the_conversation_goes_on_and_a_job_outlives_the_call():
    """A job keeps running after its call (tests/test_jobs_after_call.py). The friend's "조사 끝나는 대로 다시 알려줘" was
    taken for a goodbye and the job died with the call; now the AI says where the result will come, and ends the call
    only if the user wants to."""
    assert "then keep talking about it, or about whatever the user wants" in INSTRUCTIONS
    assert "A job keeps running if the call ends" in INSTRUCTIONS
    assert "say the result will come to the app with a notification" in INSTRUCTIONS
    assert "end the call only if they want to" in INSTRUCTIONS
    assert "A job runs only while the call lasts" not in INSTRUCTIONS


def test_the_prep_frames_a_call_the_same_way():
    for text in (PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE):
        assert "to chat or to get something done" in text and "an assistant that talks" not in text
    seed = seed_text(CallInputs(first_name="Minseung"), {"direction": "inbound"}, NOW, bee=True)
    assert "they may just want to chat" in seed


# ---------- end_call goes through only on the user's word ----------

@pytest.mark.parametrize("turns", [
    ["이제 끊을게."], ["I'm done, bye."], ["그럼 끊을게."], ["Alright, bye."],  # the scripted calls' goodbyes
    ["네 오케이. 어 통화 어 2분 뒤에 아니 그 걸어주고. 끊어줘도 돼."],  # real calls, as transcribed (2026-10-16)
    ["네. 어, 고마워. 어 통화 끊어줄래? 이제 뭐 조사가 아직 진행 중이었나?"],
    ["네 그때 네 얘기를 해보자."],  # after booking a call back (2026-10-15)
    ["안녕히 계세요."], ["오늘은 이만할게."], ["나 이제 가봐야 돼."], ["나중에 통화하자."], ["수고하세요."],
    ["Okay, talk to you later."], ["Gotta go, thanks!"], ["That's all for now."], ["I’m done for today."],
    ["See you at seven then."], ["Sorry, I'm driving right now."],
    ["나 이제 가봐야 돼.", "응."],  # the AI asked whether to end, and heard a yes
])
def test_end_call_goes_through_when_the_user_said_goodbye_or_that_they_are_done(turns):
    assert user_is_done(turns)


@pytest.mark.parametrize("turns", [
    ["이번 주말에 서울에서 볼 만한 전시 좀 찾아봐 줄래?"],  # a call in text: ended right after the job started
    ["Nice."], ["응."], ["음, 괜찮네."],  # calls in text before rule 10: ended on the user's answer in a word
    ["음, 알겠어.", "아니야, 조사 끝나는 대로 다시 알려줘."],  # the friend's real call (2026-10-16)
    ["그래, 고마워."],  # thanks are no goodbye (a real call, 2026-10-16)
    ["Just let me know when it's done."], ["Okay, got it."], ["Cool."], ["그렇구나."],
    ["나중에 결과만 알려줘."], ["조사 끝내고 알려줘."], ["조사 마무리되면 말해 줘."],
    ["그 전시는 꼭 가봐야겠다."], ["이만큼이나 많아?"], ["그만큼 비싸?"], ["담배는 작년에 끊었어."], ["소리가 좀 끊겨."],
    ["바이러스 때문에 못 갔어."], ["Tell me more about it later."],
])
def test_end_call_is_refused_when_the_user_said_no_goodbye(turns):
    assert not user_is_done(turns)


def end_call_after(tmp_path, *turns: str):
    """end_call after the user said `turns` in a call (the AI answers each in a word): what it returned, whether
    the call is ending, what the AI heard of the user, and the record."""
    import asyncio
    import json
    from types import SimpleNamespace

    from livekit.agents import AgentSession
    from livekit.agents.llm import StopResponse

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        async with AgentSession(llm=ScriptedLLM([["Mm."]] * (len(turns) + 1))) as session:
            await session.start(coordinator)
            for turn in turns:
                await session.run(user_input=turn)
            try:
                result = await coordinator.end_call(None)
            except StopResponse:
                result = None
            ending = coordinator.ending
            if result is not None:  # the call goes on
                await session.run(user_input="And one more thing.")
            heard = [i.text_content for i in session.history.items if getattr(i, "role", None) == "user"]
        rec.close()
        return result, ending, heard, [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]

    return asyncio.run(main())


def test_a_refused_end_call_leaves_the_call_open_and_asks_nothing_more_of_the_ai(tmp_path):
    """Claude Haiku 5.5 ended a call right after starting the job the user asked for (1 call in text of 27 after rule
    10, 2026-10-16); in a real call it took "아니야, 조사 끝나는 대로 다시 알려줘." for a goodbye. The goodbye it said is
    said; the line stays open, and the AI waits for the user (the refusal goes into the conversation, asking no
    reply)."""
    result, ending, heard, events = end_call_after(tmp_path, "이번 주말에 서울에서 볼 만한 전시 좀 찾아봐 줄래?")
    assert not ending and not result.reply_required and result.output.startswith("Not ended:")
    (refused,) = [e for e in events if e["type"] == "end_call_refused"]
    assert refused["heard"] == "이번 주말에 서울에서 볼 만한 전시 좀 찾아봐 줄래?"
    assert not [e for e in events if e["type"] == "end_call_requested"]
    assert heard[-1] == "And one more thing."  # the AI heard the user's next words


def test_after_a_goodbye_end_call_ends_the_call(tmp_path):
    result, ending, _, events = end_call_after(tmp_path, "음, 알겠어.", "그럼 끊을게.")
    assert result is None and ending
    assert [e["type"] for e in events if e["type"].startswith("end_call")] == ["end_call_requested"]
