"""What the call AI is told about the conversation itself, after the user's calls of 2026-10-16 (build 5): it said "민승
님" in 15 of 24 turns, offered to end a call he made because he was bored, and wrapped up right after a research job.
The user's decision (2026-10-16): one principle, that everything the AI writes is spoken in a real phone call, so it
talks the way a person talks on the phone, with what the calls showed as its examples; conversational behavior is
set in the instructions, never kept in code. Whether the model follows them was measured with calls in text against
the real model; these pin the words that carry each example."""

from datetime import datetime
from zoneinfo import ZoneInfo

from beelink.context import CallInputs
from voice.coordinator import BEE_INSTRUCTIONS, INSTRUCTIONS, instructions
from voice.language import call_language
from voice.prep import PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE, seed_text

NOW = datetime(2026, 10, 16, 13, 5, tzinfo=ZoneInfo("Asia/Seoul"))


def test_one_principle_with_its_examples():
    principle = INSTRUCTIONS.split("\n\n")[1]
    assert principle.startswith("## How to talk\nEverything you write is spoken aloud in a real phone call")
    assert "talk the way a person talks on the phone" in principle and "For example:" in principle
    for example in ("Keep turns short", "A casual line gets a casual answer, not a summary",
                    "People on the phone do not keep saying each other's names", "no reading out lists",
                    "Nobody wraps up a call the other person still wants", "Write only words to say"):
        assert example in principle, example
    assert "How to speak:" not in INSTRUCTIONS and "\n1. " not in INSTRUCTIONS  # no list of stand-alone rules


def test_a_call_needs_no_task_and_chatting_is_a_reason_for_one():
    """2026-10-18 (the user's decision): "Who you are" says what every call is for, to come to know the user and help
    them know themselves, so a call needs no task; being curious and bringing up something new moved there from the
    casual-line example."""
    for text in (INSTRUCTIONS, BEE_INSTRUCTIONS):
        first = text.split("\n\n")[0]
        assert first.startswith("## Who you are\n") and "to chat" in first and "A call needs no task." in first
        assert "Your purpose in every call is to come to know the user better" in first
        assert "help the user come to know themselves better" in first
        for words in ("follow what they say with interest", "add a light thought of your own",
                      "When a topic runs thin, open one that helps you know them", "You are not an interviewer",
                      "One question at a time, never a questionnaire", "the user's own topic always comes first",
                      "on personal or painful topics, follow their lead"):
            assert words in first, words
        assert "one concrete thing" not in text and "Lead with the conclusion" not in text
    assert "A casual line gets a casual answer, not a summary." in INSTRUCTIONS
    assert "Bee is where that knowledge is kept" in BEE_INSTRUCTIONS.split("\n\n")[0]
    assert "Bee" not in INSTRUCTIONS.split("\n\n")[0].replace("Bee Call", "")


def test_the_ai_never_steers_toward_ending_and_a_goodbye_still_ends_the_call():
    for text in (instructions(False), instructions(True)):
        assert "never end the call or suggest ending it" in text and "unless the user says they are done" in text
        assert "When they say goodbye, in whatever words" in text and "say a short goodbye and call end_call." in text
    korean = call_language("ko").rule  # Haiku 5.5 said goodbye to "좋은 하루 되세요" and "응, 들어가" but kept the line open
    assert '"좋은 하루 되세요", "들어가세요" or "수고하세요"; it ends the call as any goodbye does' in korean


def test_the_name_is_said_at_most_once_and_the_prep_greets_without_it():
    """Haiku 5.5 repeated the name the opening greeted with, as "you" in Korean ("민승 씨는 ...") and in goodbyes, in
    about a third of its replies at the personal moments of the real call's replay, whatever the wording (six tried);
    with no name in the opening and the note, in none (2026-10-16)."""
    assert "Say the user's name at most once, in your first words" in INSTRUCTIONS
    assert 'not as a word for "you", not in the goodbye' in INSTRUCTIONS
    korean = call_language("ko").rule
    assert 'Korean leaves "you" unsaid' in korean and '"주말엔 뭐 하세요?", not "OO 님은 주말엔 뭐 하세요?"' in korean
    for text in (PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE):
        assert "without the user's name" in text
        assert 'Call the user "the user" in it, not by name and never he, she, his or her.' in text
    # Told so only in the instructions, every Korean opening still greeted by name: the seed's last words ask too.
    for meta in ({"direction": "inbound"}, {"direction": "outbound"}, {"direction": "outbound", "agreed": "yes"},
                 {"direction": "inbound", "earlier_call": "..."}):
        seed = seed_text(CallInputs(), meta, NOW, bee=True)
        assert "greet the user without their name" in seed, meta


def test_end_call_says_when_it_may_be_called():
    from voice.coordinator import Coordinator

    assert "only after the user said goodbye or that they are done" in Coordinator.end_call.info.description


def test_after_a_job_s_result_the_conversation_goes_on_and_a_job_outlives_the_call():
    """A job keeps running after its call (tests/test_jobs_after_call.py). The friend's "조사 끝나는 대로 다시 알려줘" was
    taken for a goodbye and the job died with the call; now the AI says where the result will come, and ends the call
    only if the user wants to."""
    assert "then keep talking about it, asking what they make of it, or about whatever the user wants" in INSTRUCTIONS
    assert "A job keeps running if the call ends" in INSTRUCTIONS
    assert "say the result will come to the app with a notification" in INSTRUCTIONS
    assert "end the call only if they want to" in INSTRUCTIONS
    assert "A job runs only while the call lasts" not in INSTRUCTIONS


def test_the_prep_frames_a_call_the_same_way():
    for text in (PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE):
        assert text.startswith("## What you are doing\n") and "an assistant that talks" not in text
        assert "the way a good assistant glances at their notes before answering" in text
    seed = seed_text(CallInputs(), {"direction": "inbound"}, NOW, bee=True)
    assert "they may just want to chat" in seed


# ---------- end_call goes through whenever the AI calls it ----------

def end_call_after(tmp_path, *turns: str):
    """end_call after the user said `turns` in a call (the AI answers each in a word): whether the call is ending, and
    the record."""
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
        async with AgentSession(llm=ScriptedLLM([["Mm."]] * len(turns))) as session:
            await session.start(coordinator)
            for turn in turns:
                await session.run(user_input=turn)
            try:
                await coordinator.end_call(None)
            except StopResponse:  # no reply to it: the goodbye was said
                pass
            ending = coordinator.ending
        rec.close()
        return ending, [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]

    return asyncio.run(main())


def test_after_a_goodbye_end_call_ends_the_call(tmp_path):
    ending, events = end_call_after(tmp_path, "음, 알겠어.", "그럼 끊을게.")
    assert ending and [e["type"] for e in events if e["type"].startswith("end_call")] == ["end_call_requested"]


def test_end_call_goes_through_whenever_the_ai_calls_it(tmp_path):
    """Whether the user is done is the call AI's reading, as the instructions ask. A check of the user's
    words in code (2026-10-16, taken out the same day) refused 30 of 50 polite goodbyes a review tried ("좋은 하루
    되세요", "응, 들어가", "That's it, thanks", "Okay, by.") and let "아직 끊지 마" through; a goodbye it refused left
    the line silent after the AI's own. A wrong hang-up loses no work: a job keeps running after its call
    (tests/test_jobs_after_call.py), as "조사 끝나는 대로 다시 알려줘" asks."""
    ending, events = end_call_after(tmp_path, "아니야, 조사 끝나는 대로 다시 알려줘.")
    assert ending and [e["type"] for e in events if e["type"].startswith("end_call")] == ["end_call_requested"]


def test_bee_is_the_user_s_memory_kept_accurate_and_tidy_with_a_light_limit():
    """2026-10-17: when to offer a Bee change is one principle with examples, and a light limit on Bee questions
    per call, in the instructions only (no rule in code decides it)."""

    assert "each call should leave the user's memory in Bee a little truer and more complete" in BEE_INSTRUCTIONS
    assert "Each call should leave Bee a little truer without turning into a review of Bee" in BEE_INSTRUCTIONS
    for example in ("something lasting about themselves", "fact_create", "fact_delete", "todo_complete",
                    '"그거 주문했어"', "todo_dismiss", "To-dos you added to Bee in earlier calls"):
        assert example in BEE_INSTRUCTIONS, example
    assert "Ask about Bee at most twice in a call" in BEE_INSTRUCTIONS and "Never ask twice about the same item" in BEE_INSTRUCTIONS
    for step in ("only after the user's clear yes", "save_to_bee", "undo_bee_write"):
        assert step in BEE_INSTRUCTIONS, step


def test_the_user_s_yes_is_the_call_ai_s_to_judge_from_a_principle_with_examples():
    """The user's decision of 2026-10-18: no word list in code decides whether the user said yes (they refused
    "좋죠, 넣어 주세요." and "Definitely." and took "네?" and "Yeah, no."); the instructions say what a clear yes is,
    with examples, and the item may be said in the question or in the sentence just before it."""
    for words in ("Changing Bee happens right away, in the call, and only after the user's clear yes.",
                  "say the item as it will go into Bee, in your question or in the sentence just before it",
                  "\"Bee has 'Bonus is 15%'. Is that right?\"", "Ask once, and again only when the answer leaves it unclear.",
                  "After a clear yes, call save_to_bee and say in a few words what you did.",
                  "A hedge, a \"later\", a yes taken back (\"Yes. Actually no, keep it.\", \"네, 잠깐만요, 지우지 마세요.\") or "
                  "a yes to another question is no yes. Change nothing then.",
                  "To delete a fact or turn down Bee's suggestion, say so in the question: \"Shall I delete 'Bonus is "
                  "15%' from Bee?\"",
                  # third review of 2026-10-18: the AI names the change by its key; the code checks only that it can
                  "When the user wants something back, call undo_bee_write with the key of the change they mean: every "
                  "answer that reports a change gives its key (\"w3\"), and the user's words tell you which change "
                  "they mean. Never say a key aloud."):
        assert words in BEE_INSTRUCTIONS, words
    for gone in ("propose_bee_save", "quotes the item exactly", "saves nothing", "cannot be brought back",
                 "only the answer to your first question"):
        assert gone not in BEE_INSTRUCTIONS, gone


def test_what_bee_keeps_is_brought_up_when_it_connects_and_never_recited():
    """2026-10-17 ("the next call starts from what Bee keeps"): the prep carries the kept facts that may connect to
    the call into its note, and the call AI mentions one when the topic connects, never as a list."""

    assert "What Bee keeps about the user" in BEE_INSTRUCTIONS and "never as a list" in BEE_INSTRUCTIONS
    # The prep note is what this call must know, compacted from what came before (the user's text, 2026-10-18).
    assert "It is the context this call must know, compacted from what came before" in PREP_INSTRUCTIONS


def test_bee_items_are_english_and_the_question_quotes_them_in_the_call_s_language():
    """2026-10-17: Bee's language is English; the question around the item stays in the call's language."""

    assert "Write every Bee item in English, Bee's language, whatever the call's language" in BEE_INSTRUCTIONS
    assert "\"Bee에 'Wants a quiet home server'로 기억해 둘까요?\"" in BEE_INSTRUCTIONS
    assert "keep its text as Bee has it" in BEE_INSTRUCTIONS and "a correction in English" in BEE_INSTRUCTIONS
    assert "written in the user's language" not in BEE_INSTRUCTIONS


def test_a_lasting_fact_is_kept_for_bee_not_kept_in_words():
    """Measured 2026-10-17 (Haiku 5.5, a Korean call): told "나 땅콩 알레르기 있어.", it said "기억해 둘게요" and
    offered nothing, though only Bee keeps it; the example became one said in passing, in Korean. Since the user's
    decision of 2026-10-18 it is not offered in the reply to it: the AI keeps it in mind and offers what came up once,
    later in the call (collect, then offer once). The examples are not the measured lines (a peanut allergy)."""

    assert "keep it in mind and stay with what they are saying: ask nothing about Bee then, and do not say you will " \
           "remember it" in BEE_INSTRUCTIONS
    assert "even in passing (\"참고로 나 매운 거 못 먹어\"" in BEE_INSTRUCTIONS and "in your reply to it" not in BEE_INSTRUCTIONS
    assert "peanut" not in BEE_INSTRUCTIONS.casefold()


def test_no_notes_to_itself_are_written_since_everything_is_said():
    """Measured 2026-10-17 (Haiku 5.5, a Korean call): one reply ended with "Also, the allergy is a lasting fact
    about the user. Offer once to keep it in Bee. Propose it first.", which the user would have heard."""
    assert "no notes to yourself" in instructions(audio_tags=False)
    assert "no notes to yourself" in instructions(audio_tags=True)
