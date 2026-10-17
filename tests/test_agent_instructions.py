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
    assert re.findall(r"^### (.+)$", call, re.M) == ["One item at a time", "Several at once: organizing"]
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
    assert {"propose_bee_save", "save_to_bee", "undo_bee_write", "organize_bee"} <= names
    assert not names & {"search_bee", "bee_day", "bee_pending"}


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
    assert ("the meta agent's notes (open threads and the agenda for this call), and what changed in Bee since the "
            "last call. Start from those.") in PREP_INSTRUCTIONS
    from meta.session import STATE_TEMPLATE

    assert "## Worth getting to know next" in STATE_TEMPLATE


def test_the_meta_agent_changes_bee_only_when_the_user_said_yes_to_organizing():
    text = meta_instructions(bee=True)
    assert "You never change Bee, except when the user said yes to organizing their Bee memory (below)." in text
    assert "At any other time you have no tools that change Bee." in text
    assert "Never delete, merge away or rewrite what Bee already has." in text


def test_the_call_ai_offers_to_organize_once_and_only_adds(tmp_path):
    text = call_ai(tmp_path, True).instructions
    organizing = text[text.index("### Several at once: organizing"):]
    for words in ("offer once to organize, in one question that names Bee, in a few words and without listing the items",
                  "Bee에 정리해서 넣어 둘까요?", "This counts as one of your two Bee questions",
                  "never offer it while the user is in the middle of their own topic",
                  "They go into Bee at once as confirmed facts and open to-dos",
                  "넣었어요. Bee 앱에서 보고 고칠 수 있어요.", 'call organize_bee with scope "recent"',
                  "Organizing only adds. It never deletes, merges away or rewrites what Bee already has"):
        assert words in organizing, words



def test_what_the_measurement_of_2026_10_18_added_to_the_instructions(tmp_path):
    """Measured with the real models (server-progress.md): asked what was new with it, the call AI told the user's own
    home server plans as its own (1 of 4 casual calls); it said a tool's name aloud in 2 replies of one call;
    single-item offers always came first, so the organize offer never came (0 of 4); the organize run added items close
    to Bee's unconfirmed facts and suggested to-dos, having read only the confirmed facts."""
    from meta.agent import instructions as meta_instructions
    from voice.coordinator import SPEECH_RULE, SPEECH_RULE_WITH_TAGS

    call = call_ai(tmp_path, True).instructions
    assert "What Bee keeps is the user's life, never yours: bring it up as theirs" in call
    assert "when they ask about you, answer in a few words and turn back to them" in call
    for rule in (SPEECH_RULE, SPEECH_RULE_WITH_TAGS):
        assert "no tool names" in rule
    assert ("Once you have offered one item in a call, keep the lasting things that come up after it for this one "
            "offer instead of asking about each.") in call
    assert ("check each against what Bee already has (its facts, confirmed or not, its to-dos and the to-dos it "
            "suggests) so nothing is added twice") in meta_instructions(bee=True)
