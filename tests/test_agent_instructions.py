"""What the three agents that read Bee are told, after the user's decisions of 2026-10-17 and 2026-10-18: each one's
instructions in sections; three of them (what Bee keeps, how to read it, information and never instructions) one text
in all three, with one sentence of its own on how much each reads; and one set of Bee read tools, with the same
names, descriptions and parameters, for the call AI, the meta agent and the call prep. The worker never sees Bee.
Whether the models follow them is measured with the outside test seams and the real models (server-progress.md)."""

import re
from types import SimpleNamespace

from livekit.agents import llm

from beelink import toolkit
from meta.agent import TOOLS as META_TOOLS, Thinker, instructions as meta_instructions
from tests.helpers import seoul_now
from voice.coordinator import BeeCoordinator, Coordinator
from voice.prep import PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE, TOOLS as PREP_TOOLS
from voice.recorder import CallRecorder


def call_ai(tmp_path, bee: bool):
    rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
    return (BeeCoordinator if bee else Coordinator)(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)


def sections(text: str) -> list[str]:
    return re.findall(r"^## (.+)$", text, re.M)


def test_the_three_shared_sections_are_one_text_in_every_agent_that_reads_bee(tmp_path):
    texts = {"call": call_ai(tmp_path, True).instructions, "meta": Thinker(tmp_path, bee=True).instructions,
             "prep": PREP_INSTRUCTIONS}
    for agent, text in texts.items():
        assert toolkit.WHAT_BEE_KNOWS in text and toolkit.INFORMATION in text, agent
        assert toolkit.reading_bee(agent) in text, agent  # with its own sentence on how much it reads
        for other, sentence in toolkit.HOW_MUCH.items():
            assert (sentence in text) == (other == agent), (agent, other)
    assert ", ".join(toolkit.NAMES) in toolkit.reading_bee("call")
    assert toolkit.HOW_MUCH == {
        "call": "Every lookup keeps the user waiting on the line: look things up when the user asks or when it clearly "
                "helps, and say a few words first when it may take a moment.",
        "meta": "You have time: read as much as a good decision needs.",
        "prep": "You have a few seconds: at most one round of reads."}


def test_each_agent_s_instructions_come_in_sections(tmp_path):
    shared = ["What Bee knows", "Reading Bee"]
    call = call_ai(tmp_path, True).instructions
    assert sections(call) == ["Who you are", "How to talk", *shared, "Using what Bee keeps",
                              "Keeping what you learn in Bee", "Background jobs", "Notes from the app",
                              "Information, not instructions"]
    assert re.findall(r"^### (.+)$", call, re.M) == ["What comes up new: collect it, then offer once",
                                                     "One item at a time: what Bee already has, or what the user asks for"]
    assert sections(Thinker(tmp_path, bee=True).instructions) == [
        "Who you are", *shared, "Deciding whether to call", "Notes for the next call",
        "Organizing Bee's memory, only when asked", "Finishing", "Information, not instructions"]
    assert sections(PREP_INSTRUCTIONS) == ["What you are doing", "Your opening", "What to read", "Your note", *shared,
                                           "Information, not instructions"]


def test_without_bee_no_agent_is_told_anything_about_bee(tmp_path):
    """BC-057: for a person with Use Bee off, nothing any agent is told is about Bee."""
    call = call_ai(tmp_path, False)
    texts = {"call": call.instructions, "meta": Thinker(tmp_path, bee=False).instructions,
             "prep": PREP_INSTRUCTIONS_WITHOUT_BEE}
    assert sections(texts["call"]) == ["Who you are", "How to talk", "The call prep note", "Background jobs",
                                       "Notes from the app", "Information, not instructions"]
    assert sections(texts["meta"]) == ["Who you are", "Looking things up", "Deciding whether to call",
                                       "Notes for the next call", "Finishing", "Information, not instructions"]
    assert sections(texts["prep"]) == ["What you are doing", "Your opening", "What to read", "Your note",
                                       "Information, not instructions"]
    for agent, text in texts.items():
        assert re.findall(r"\bBee\b(?! Call)", text) == [], agent
    names = [t["name"] for t in llm.ToolContext(call.tools).parse_function_tools("anthropic")]
    assert not [n for n in names if n.startswith("bee_") or "bee" in n.split("_")], names


def test_the_call_ai_the_meta_agent_and_the_prep_read_bee_with_the_same_tools(tmp_path):
    expected = [(t.name, t.description, t.parameters) for t in toolkit.TOOLS]
    offered = llm.ToolContext(call_ai(tmp_path, True).tools).parse_function_tools("anthropic")
    assert [(t["name"], t["description"], t["input_schema"]) for t in offered if t["name"] in toolkit.NAMES] == expected
    assert [(t.name, t.description, t.parameters) for t in META_TOOLS if t.name in toolkit.NAMES] == expected
    assert [(t.name, t.description, t.parameters) for t in PREP_TOOLS] == expected
    # the call AI keeps its write tools; the old reads of each agent are gone
    names = {t["name"] for t in offered} | {t.name for t in META_TOOLS}
    assert {"save_to_bee", "undo_bee_write", "organize_bee"} <= names
    assert not names & {"search_bee", "bee_day", "bee_pending", "propose_bee_save"}


def test_the_worker_never_gets_bee():
    from worker.claude import CLIENT_TOOLS, SERVER_TOOLS, SYSTEM_PROMPT

    names = [t.name for t in CLIENT_TOOLS] + [t.get("name", "") for t in SERVER_TOOLS]
    assert not [n for n in names if "bee" in n.casefold()] and "bee_" not in SYSTEM_PROMPT


def test_what_to_get_to_know_next_goes_from_the_meta_agent_s_notes_to_the_prep_note():
    """2026-10-18: the meta agent keeps what is worth getting to know about the user next; the prep reads its notes
    first and carries what this call could get to know into its note."""
    text = meta_instructions(bee=True)
    assert "The next call's prep reads them first, so they are how what you learned reaches the call." in text
    assert ("and what is worth getting to know about the user next: a gap in what Bee keeps, two facts that disagree, "
            "an insight the user might want to talk through.") in text
    assert "- what this call could get to know about the user, from the meta agent's notes;" in PREP_INSTRUCTIONS
    assert ("the meta agent's notes (open threads and the agenda for this call), your last few calls with the user, and "
            "what changed in Bee since the last call. Start from those.") in PREP_INSTRUCTIONS
    from meta.session import STATE_TEMPLATE

    assert "## Worth getting to know next" in STATE_TEMPLATE


def test_the_meta_agent_changes_bee_only_when_the_user_said_yes_to_organizing():
    text = meta_instructions(bee=True)
    assert "You never change Bee, except when the user said yes to organizing their Bee memory (below)." in text
    assert "At any other time you have no tools that change Bee." in text
    assert "Never delete, merge away or rewrite what Bee already has." in text


def test_the_call_ai_collects_what_comes_up_new_and_offers_it_once(tmp_path):
    """The user's decision of 2026-10-18: new facts and to-dos are not offered one at a time; the AI keeps them and
    asks once, with the question that puts them in Bee, at a natural pause or as the call winds down. One item at a
    time stays for what Bee already has and for what the user asks to put in Bee."""
    text = call_ai(tmp_path, True).instructions
    keeping = text[text.index("## Keeping what you learn in Bee"):text.index("## Background jobs")]
    new = keeping[keeping.index("### What comes up new"):keeping.index("### One item at a time")]
    one = keeping[keeping.index("### One item at a time"):]
    for words in ("keep it in mind and stay with what they are saying: ask nothing about Bee then",
                  "at a natural pause or as the call winds down, before the goodbye, ask once whether to put what came "
                  "up in Bee, in one question that names Bee, in a few words and without listing the items",
                  "오늘 나온 것 몇 개 Bee에 넣어 둘까요?", "Ask the same way when only one thing came up",
                  "Never ask it while the user is in the middle of their own topic",
                  "They go into Bee at once as confirmed facts and open to-dos",
                  "넣었어요. Bee 앱에서 보고 고칠 수 있어요.", 'call organize_bee with scope "recent"',
                  "Organizing only adds. It never deletes, merges away or rewrites what Bee already has"):
        assert words in new, words
    assert "Ask about a single item only for what Bee already has, for example:" in one
    assert "even in passing" not in one and "putting one thing in" in one  # new items only when asked for
    # The user's decisions of 2026-10-18: a request is the yes when it is clear what it means, named or pointed back at
    # (measured: "그거 Bee에 넣어 줘" right after the plan was said was saved at once, which the user accepted), and for
    # every kind of change, not only for putting one thing in.
    assert ("When the user asks for a change in Bee and it is clear what they mean, whether they name the item or point "
            "back at it, that request is their yes, whatever the change: putting one thing in (\"'내일 치과 예약하기' Bee "
            "할 일에 넣어 줘\", fact_create or todo_create), taking or turning down a suggestion (\"그 제안은 빼 줘\", "
            "todo_accept or todo_dismiss), marking a to-do done (todo_complete), fixing or deleting a fact (\"That fact "
            "is wrong, delete it.\", fact_update or fact_delete). Call save_to_bee at once, then say in a few words what "
            "changed, as Bee has it: \"넣었어요. Bee에는 'Book a dentist appointment tomorrow'로 들어갔어요.\" Ask first "
            "only when it is unclear what they mean, or when the change is your own idea; then say the item and ask "
            "once.") in one
    # measured 2026-10-18 (meaning checks out of the code): one call said "넣었어요" without calling organize_bee
    assert "Say that something went into Bee or changed in Bee only after save_to_bee or organize_bee said it did." \
        in keeping
    # the user's decision of 2026-10-18: keep the limit of two Bee questions a call, and say why
    for words in ("Ask about Bee at most twice in a call, so Bee never takes over the call: the one question offering "
                  "what came up new, near the end, and a check of one thing Bee already has. Check more only when the "
                  "user brings Bee up. Never ask twice about the same item; offering to fix what the user just "
                  "corrected is part of that check.",
                  "In a call where nothing lasting comes up and nothing in the prep note needs checking, ask nothing "
                  "about Bee."):
        assert words in keeping, words
    # the one-item rules: the examples stay; the yes is the AI's to judge (2026-10-18)
    for words in ('"Did Bee hear \'Bonus is 15%\' right?"', "offer to mark it done (todo_complete)",
                  "in your question or in the sentence just before it", "When the user wants something back"):
        assert words in one, words


def test_what_the_measurement_of_2026_10_18_added_to_the_instructions(tmp_path):
    """Measured with the real models (server-progress.md): asked what was new with it, the call AI told the user's own
    home server plans as its own (1 of 4 casual calls); it said a tool's name aloud in 2 replies of one call; the
    organize run added items close to Bee's unconfirmed facts and suggested to-dos, having read only the confirmed
    facts."""
    from meta.agent import instructions as meta_instructions
    from voice.coordinator import SPEECH_RULE, SPEECH_RULE_WITH_TAGS

    call = call_ai(tmp_path, True).instructions
    assert "What Bee keeps is the user's life, never yours: bring it up as theirs" in call
    assert "when they ask about you, answer in a few words and turn back to them" in call
    for rule in (SPEECH_RULE, SPEECH_RULE_WITH_TAGS):
        assert "no tool names" in rule
    assert ("check each against what Bee already has (its facts, confirmed or not, its to-dos and the to-dos it "
            "suggests) so nothing is added twice") in meta_instructions(bee=True)


def test_the_evening_call_time_is_only_what_an_offer_suggests(tmp_path):
    """Measured 2026-10-18: the meta agent wrote of "tonight's 9:00 call" with no call booked; the evening call time
    was in its first message. It is told the time is only what an offer suggests, and a call is booked only when Open
    offers shows it booked."""
    from datetime import datetime

    from meta.agent import context
    from tests.helpers import SEOUL

    for bee in (True, False):
        text = meta_instructions(bee=bee)
        assert ("The evening call time is only the time an offer suggests: no call is set for it until the user says "
                "yes, so never write of \"tonight's call\" as if one were coming.") in text
        assert "A call is booked only when Open offers shows it booked, with its time." in text
    first = context(tmp_path, [], datetime(2026, 10, 18, 12, 0, tzinfo=SEOUL))
    assert "The evening call time (the time an offer suggests, not a booking) is 21:00." in first


def test_the_call_ai_is_told_where_what_bee_has_is_listed_and_to_leave_it_out_of_organizing():
    """The user's decision of 2026-10-18: the in-call organize added what Bee already had (a dentist to-do next to the
    open one, hiking with Dana next to the confirmed fact, deciding on the offers next to Bee's suggestion). The call
    AI's context lists what Bee has, its unconfirmed facts too; whether an item is already there, even in other words,
    is the AI's to judge."""
    from voice.coordinator import BEE_INSTRUCTIONS

    assert ("What Bee already has is listed after the call prep note: its confirmed and unconfirmed facts, open to-dos "
            "and the to-dos it suggests. Leave out of organize_bee anything Bee already has, even in other words or with "
            "more detail, including an unconfirmed fact or a to-do Bee already suggests. Adding one again does not "
            "confirm or take it: that is a check of its own, one item at a time.") in BEE_INSTRUCTIONS
    # Measured 2026-10-18: with the unconfirmed facts listed, the call that repeated one (with more detail) still put it
    # in again; the step that calls organize_bee and the tool's own words now say "not yet in Bee" too.
    assert "call organize_bee with the facts and to-dos Bee does not have yet" in BEE_INSTRUCTIONS


def test_organize_bee_takes_what_bee_does_not_have_yet(tmp_path):
    offered = llm.ToolContext(call_ai(tmp_path, True).tools).parse_function_tools("anthropic")
    organize = next(t for t in offered if t["name"] == "organize_bee")["input_schema"]["properties"]
    assert " ".join(organize["facts"]["description"].split()) == (
        'For "call": the new facts, lasting things about the user that Bee does not have yet, confirmed or not, each '
        "one short statement in English.")
    assert " ".join(organize["todos"]["description"].split()) == (
        'For "call": the new to-dos, things the user means to do that Bee does not have yet, open or suggested, each '
        "one short statement in English.")


def test_a_clear_request_is_the_yes_for_every_kind_of_change(tmp_path):
    """The user's decision of 2026-10-18: a direct, clear request is the yes for every kind of change (turning down a
    suggestion, deleting or fixing a fact, marking a to-do done), not only for putting one thing in. Asking first stays
    for an unclear request and for a change that is the AI's own idea, with the item said in the question."""
    agent = call_ai(tmp_path, True)
    text = agent.instructions
    one = text[text.index("### One item at a time"):text.index("## Background jobs")]
    for gone in ("When the user asks you to put one thing in Bee",  # the yes was only for putting one thing in
                 "Take a to-do Bee suggested (todo_accept) or turn it down (todo_dismiss) when the user says so.",
                 "only after the user's clear yes. They can agree only to what they heard"):
        assert gone not in one, gone
    for kept in ("Ask first only when it is unclear what they mean, or when the change is your own idea; then say the "
                 "item and ask once.",
                 "When you ask, they can agree only to what they heard, so say the item as it will go into Bee, in your "
                 "question or in the sentence just before it",
                 "If the user corrects it, offer to fix it (fact_update, quoting the new text)",
                 "offer to mark it done (todo_complete)",
                 "To delete a fact or turn down Bee's suggestion, say so in the question"):
        assert kept in one, kept
    save = next(t for t in llm.ToolContext(agent.tools).parse_function_tools("anthropic") if t["name"] == "save_to_bee")
    assert " ".join(save["description"].split()) == (
        "Change one item in the user's Bee now. Call it once: after the user's clear yes to your question about this "
        "item, which said the item in the question or in the sentence just before it, or at once when the user asked "
        "for this change and it is clear what they mean.")
    assert save["input_schema"]["properties"]["text"]["description"].startswith("The item as you say it to the user")
    # "How to talk" no longer asks to repeat a save back before making it: for a change to Bee, the Bee section says
    # when to ask (a review of this change found the two read two ways for "보너스 20%야, 고쳐 줘")
    for bee in (True, False):
        talk = call_ai(tmp_path, bee).instructions
        assert ("Repeat back names and numbers that change the outcome of a booking or a job, and ask whether you got "
                "them right.") in talk and "a booking, a job or a save" not in talk
