"""What the call AI is told about the conversation itself, after the user's calls of 2026-10-16 (build 5): it said "민승
님" in 15 of 24 turns, offered to end a call he made because he was bored, and wrapped up right after a research job.
Whether the model follows these was measured with calls in text against the real model (2026-10-16); these pin the
words that carry each rule."""

from datetime import datetime
from zoneinfo import ZoneInfo

from beelink.context import CallInputs
from voice.coordinator import INSTRUCTIONS, instructions
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


def test_after_a_job_s_result_the_conversation_goes_on_and_a_job_lasts_as_long_as_the_call():
    assert "then keep talking about it, or about whatever the user wants" in INSTRUCTIONS
    assert "A job runs only while the call lasts" in INSTRUCTIONS


def test_the_prep_frames_a_call_the_same_way():
    for text in (PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE):
        assert "to chat or to get something done" in text and "an assistant that talks" not in text
    seed = seed_text(CallInputs(first_name="Minseung"), {"direction": "inbound"}, NOW, bee=True)
    assert "they may just want to chat" in seed
