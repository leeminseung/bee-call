"""Bee writes happen during the call, right after the user's spoken yes (contract section 6, the user's decision of
2026-10-15): the question that named Bee and the answer are kept with each write, the user can take the last one
back, a failed write is retried after the call, and one notification after the call says what went into Bee.

A yes counts only when it answers a question asked after the item was proposed (propose_bee_save), so no save
rides on an older yes or on a question asked in the same breath as the save (review of this round's fixes,
finding 1).

The fake `bee` (tests/fakebee/bee) stands in for the Bee CLI; nothing reaches a real Bee account.
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from beelink import cli, writer
from beelink.writer import BeeWrites
from tests.helpers import seoul_now
from tests.test_beelink import FAKE_BEE, WRITE_COMMANDS, calls
from voice.coordinator import BeeCoordinator
from voice.language import call_language
from voice.recorder import CallRecorder


@pytest.fixture
def fake_bee(tmp_path, monkeypatch):
    log = tmp_path / "bee-calls.jsonl"
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(log))
    return log


def fail_once(tmp_path, monkeypatch, command: str) -> None:
    flag = tmp_path / "fail-once"
    flag.write_text(command)
    monkeypatch.setenv("FAKE_BEE_FAIL_ONCE", str(flag))


def writes_to_bee(log: Path) -> list[list[str]]:
    return [c[:2] for c in calls(log) if tuple(c[:2]) in WRITE_COMMANDS | {("todos", "delete")}]


def events(rec: CallRecorder) -> list[dict]:
    return [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]


RECORDERS: list[CallRecorder] = []  # the calls a test opened, closed when it ends


@pytest.fixture(autouse=True)
def close_the_calls():
    yield
    while RECORDERS:
        RECORDERS.pop().close()


async def in_a_call(tmp_path, said: list[tuple[str, str]] = (), language: str = "en"):
    """A Bee person's call after these turns (role, text)."""
    rec = CallRecorder(tmp_path / "workspace", room="call-matt-20261015-210000", now=seoul_now())
    RECORDERS.append(rec)
    agent = BeeCoordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now,
                           language=call_language(language))
    await say(agent, *said)
    return agent, rec


async def say(agent, *turns: tuple[str, str]) -> None:
    """Turns of the conversation, as LiveKit adds them to the AI's chat."""
    chat = agent.chat_ctx.copy()
    for role, text in turns:
        chat.add_message(role=role, content=text)
    await agent.update_chat_ctx(chat)


async def proposed(agent, **item) -> int:
    """The AI fixes an item it means to ask about (propose_bee_save); its number."""
    said = await agent.propose_bee_save(None, **item)
    return int(re.match(r"Proposal (\d+):", said).group(1))


async def saved(agent, question: str, answer: str, **item) -> str:
    """The AI proposes the item, asks `question`, hears `answer`, and saves it; what save_to_bee told the AI."""
    number = await proposed(agent, **item)
    await say(agent, ("assistant", question), ("user", answer))
    return await agent.save_to_bee(None, proposal=number)


ASKED = ("Want me to add 'note the bug' to your Bee todos?", "Yes, please.")


def test_a_yes_to_a_question_naming_bee_writes_at_once_and_keeps_the_question_and_answer(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "I found a bug in the app.")])
        said = await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        return said, rec

    said, rec = asyncio.run(main())
    assert said.startswith("Saved to Bee: todo 'Note the bug'.") and writes_to_bee(fake_bee) == [["todos", "create"]]
    (event,) = [e for e in events(rec) if e["type"] == "bee_write"]
    assert (event["kind"], event["text"], event["result"]) == ("todo_create", "Note the bug", "ok")
    assert (event["question"], event["answer"]) == ASKED
    (write,) = BeeWrites(rec.dir).all()
    assert (write.status, write.question, write.answer) == ("ok", *ASKED)


def test_a_yes_to_any_other_question_writes_nothing(tmp_path, fake_bee):
    """The call of 2026-10-15 01:23: "네, 맞아요" to "제가 이해한 게 맞나요?" was taken as a yes to saving."""
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "버그를 찾았어요.")], "ko")
        return await saved(agent, "제가 이해한 게 맞나요?", "네, 맞아요.", kind="todo_create", text="버그 기록"), rec

    said, rec = asyncio.run(main())
    assert said.startswith("Not saved") and "names Bee" in said
    assert writes_to_bee(fake_bee) == [] and BeeWrites(rec.dir).all() == []
    assert [e for e in events(rec) if e["type"].startswith("bee_write")] == []


def test_a_turn_that_only_mentions_bee_is_no_yes(tmp_path, fake_bee):
    """Review of this round's fixes, finding 1: the AI's last turn named Bee, and the user went on to something else."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("assistant", "Done, I added 'call mom' to Bee."),
                                              ("user", "Thanks. By the way I might also email Jun tomorrow.")])
        number = await proposed(agent, kind="todo_create", text="Email Jun")
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_a_save_in_the_reply_that_asks_is_no_yes(tmp_path, fake_bee):
    """The AI saves one item after a yes, then asks about the next and saves it in the same reply."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("user", "Remind me to call mom.")])
        await saved(agent, "Shall I add 'call mom' to your Bee todos?", "Yes.", kind="todo_create", text="Call mom")
        number = await proposed(agent, kind="todo_create", text="Email Jun")
        await say(agent, ("assistant", "Saved. Shall I also add 'email Jun' to Bee?"))
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved")
    assert writes_to_bee(fake_bee) == [["todos", "create"]]  # call mom only


def test_a_yes_to_the_question_just_before_the_proposal_counts_when_it_quoted_the_item(tmp_path, fake_bee):
    """Measured 2026-10-17 (Haiku 5.5, Korean and English calls): in 4 of 10 saves it asked first and proposed in
    its reply to the yes; the yes did not count, so it asked again, up to four times. A yes counts when it answers
    the question just before it, which named Bee and quoted the item whole, and the item was proposed right after
    that answer. The quote is what ties the yes to the item; anything looser still saves nothing (below)."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("assistant", "Shall I add 'call mom' to your Bee todos?"),
                                              ("user", "Yes.")])
        number = await proposed(agent, kind="todo_create", text="Call mom")
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Saved to Bee") and writes_to_bee(fake_bee) == [["todos", "create"]]


@pytest.mark.parametrize("said", [
    [("assistant", "Shall I add 'call mom' to your Bee todos?"), ("user", "Yes.")],  # another item than proposed
    [("assistant", "Shall I add 'email Jun' to your Bee todos?"), ("assistant", "Also, how was your day?"),
     ("user", "Yes.")],  # not the question just before the answer
    [("assistant", "Shall I add 'email Jun' to your Bee todos?"), ("user", "Hmm, let me think."),
     ("assistant", "Sure."), ("user", "Yes.")],  # the answer to it was no yes, and the talk went on
    [("assistant", "Want me to remember that in Bee?"), ("user", "Yes.")],  # no item quoted
])
def test_a_yes_before_the_proposal_counts_only_for_the_quoted_item_just_asked_about(tmp_path, fake_bee, said):
    async def main():
        agent, _ = await in_a_call(tmp_path, said)
        number = await proposed(agent, kind="todo_create", text="Email Jun")
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_a_word_like_been_does_not_name_bee(tmp_path, fake_bee):
    """Second review of this round's fixes, finding 1: "bee" was looked for anywhere, so "been" named Bee."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        return await saved(agent, "Have you been busy? Shall I add 'Note the bug' to your todos?", "Yes.",
                           kind="todo_create", text="Note the bug")

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_a_yes_to_a_bee_question_about_something_else_saves_nothing_earlier(tmp_path, fake_bee):
    """The user turned a todo down; a later yes, to a question about a Bee fact, does not save that todo."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        todo = await proposed(agent, kind="todo_create", text="Call mom")
        await say(agent, ("assistant", "Shall I add 'Call mom' to your Bee todos?"), ("user", "Not now."))
        await proposed(agent, kind="fact_confirm", fact_id=7, text="Bonus is 15%")
        await say(agent, ("assistant", "Bee heard 'Bonus is 15%'. Is that right?"), ("user", "Yes."))
        return await agent.save_to_bee(None, proposal=todo)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


@pytest.mark.parametrize("answer", ["No, don't.", "아니요, 넣지 마세요.", "Yes.\nWait, no, don't."])
def test_a_no_saves_nothing(tmp_path, fake_bee, answer):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), *[("user", breath) for breath in answer.split("\n")])
        return await agent.save_to_bee(None, proposal=number)

    said = asyncio.run(main())
    assert said.startswith("Not saved") and writes_to_bee(fake_bee) == []
    assert "propose it again" in said  # it may be a partial yes: the AI asks about the item alone


@pytest.mark.parametrize("answer", ["네, 넣어주세요. 알람은 필요 없어요.", "Yes, but don't set an alarm."])
def test_a_yes_that_says_no_to_something_else_is_a_yes(tmp_path, fake_bee, answer):
    async def main():
        agent, _ = await in_a_call(tmp_path, language="ko" if "네" in answer else "en")
        text = "버그 기록" if "네" in answer else "Note the bug"
        question = "Bee에 할 일로 '버그 기록'을 넣을까요?" if "네" in answer else ASKED[0]
        return await saved(agent, question, answer, kind="todo_create", text=text)

    assert asyncio.run(main()).startswith("Saved to Bee")


def test_a_statement_naming_bee_and_the_item_is_no_question(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        return await saved(agent, "I'll add 'Note the bug' to your Bee todos.", "Great.",
                           kind="todo_create", text="Note the bug")

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_a_yes_said_in_two_breaths_is_kept_whole(tmp_path, fake_bee):
    """Speech is sometimes split into two user turns; the answer kept is both, not only the last part."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), ("user", "Yes."), ("user", "Please add it."))
        return await agent.save_to_bee(None, proposal=number), rec

    said, rec = asyncio.run(main())
    assert said.startswith("Saved to Bee")
    assert [w.answer for w in BeeWrites(rec.dir).all()] == ["Yes. Please add it."]


def test_an_answer_the_conversation_moved_past_needs_the_item_proposed_again(tmp_path, fake_bee):
    """Only the answer to the first question about a proposal counts; once the conversation went past it, the AI
    proposes the item again and asks again (third review of this round's fixes, finding 1)."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), ("user", "Let me think."), ("assistant", "Sure, take your time."),
                  ("user", "Okay."))
        moved_on = await agent.save_to_bee(None, proposal=number)
        again = await agent.save_to_bee(None, proposal=number)
        renewed = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", "So, shall I add 'Note the bug' to your Bee todos?"), ("user", "Yes."),
                  ("assistant", "Adding it now."))  # the AI's own words while it saves do not count against it
        return moved_on, again, await agent.save_to_bee(None, proposal=renewed)

    moved_on, again, renewed = asyncio.run(main())
    assert moved_on.startswith("Not saved") and "propose it again" in moved_on and "propose it again" in again
    assert renewed.startswith("Saved to Bee") and writes_to_bee(fake_bee) == [["todos", "create"]]


def test_an_item_turned_down_is_not_saved_on_a_yes_to_anything_else(tmp_path, fake_bee):
    """Third review of this round's fixes, finding 1: the AI's next turn quoted the item and asked something else."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Buy milk")
        await say(agent, ("assistant", "Shall I add 'Buy milk' to your Bee todos?"), ("user", "No."),
                  ("assistant", "Okay, I won't add 'Buy milk' to Bee. Anything else?"),
                  ("user", "Yes, remind me to call mom."))
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_an_item_announced_rather_than_asked_about_is_not_saved(tmp_path, fake_bee):
    """The quote has to sit in the question itself: "I'll add 'Buy milk' to your Bee todos. Anything else?" asks
    about something else."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Buy milk")
        await say(agent, ("assistant", "I'll add 'Buy milk' to your Bee todos. Anything else?"),
                  ("user", "Yes, also call mom."))
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_an_item_the_user_changed_is_not_saved_on_the_yes_to_the_new_one(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        old = await proposed(agent, kind="todo_create", text="Buy milk")
        await say(agent, ("assistant", "Shall I add 'Buy milk' to your Bee todos?"),
                  ("user", "Make it milk and eggs."))
        new = await proposed(agent, kind="todo_create", text="Buy milk and eggs")
        await say(agent, ("assistant", "Shall I add 'Buy milk and eggs' to your Bee todos?"), ("user", "Yes."))
        return await agent.save_to_bee(None, proposal=old), await agent.save_to_bee(None, proposal=new)

    old, new = asyncio.run(main())
    assert old.startswith("Not saved") and new.startswith("Saved to Bee")
    assert [c[3] for c in calls(fake_bee) if c[:2] == ["todos", "create"]] == ["Buy milk and eggs"]


def test_a_short_item_inside_a_longer_quote_is_not_quoted(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path, language="ko")
        number = await proposed(agent, kind="todo_create", text="운동")
        await say(agent, ("assistant", "Bee가 '주말마다 운동을 함'이라고 들었어요. 맞나요?"), ("user", "네."))
        return await agent.save_to_bee(None, proposal=number)

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


def test_a_proposal_is_saved_once(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), ("user", ASKED[1]))
        return await agent.save_to_bee(None, proposal=number), await agent.save_to_bee(None, proposal=number)

    first, again = asyncio.run(main())
    assert first.startswith("Saved to Bee") and "already saved" in again
    assert writes_to_bee(fake_bee) == [["todos", "create"]]


def test_two_saves_at_once_are_kept_apart(tmp_path, fake_bee):
    """LiveKit runs the tool calls of one reply at the same time (livekit/agents/voice/generation.py); each write
    keeps its own record, so one cannot take the other's result or be undone in its place."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        one = await proposed(agent, kind="fact_update", fact_id=7, text="Bonus is 10%")
        two = await proposed(agent, kind="fact_update", fact_id=8, text="Starts in May")
        await say(agent, ("assistant", "Shall I fix both in Bee: 'Bonus is 10%' and 'Starts in May'?"),
                  ("user", "Yes."))
        await asyncio.gather(agent.save_to_bee(None, proposal=one), agent.save_to_bee(None, proposal=two))
        return rec

    rec = asyncio.run(main())
    assert sorted(w.item["fact_id"] for w in BeeWrites(rec.dir).all()) == [7, 8]
    assert len({e["key"] for e in events(rec) if e["type"] == "bee_write"}) == 2


def test_a_korean_call_writes_bee_items_in_english_and_asks_in_korean_quoting_them(tmp_path, fake_bee):
    """2026-10-17, the user's decision: English is Bee's language (Bee writes its own summaries in English for a
    Korean speaker), so the item is English whatever the call's language, and the Korean question quotes it whole.
    The language is the instructions' to set; no check in code turns an item away for its language."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("user", "홈서버는 조용해야 해. 그리고 버그도 하나 찾았어.")], "ko")
        fact = await saved(agent, "Bee에 'My home server has to be quiet'로 기억해 둘까요?", "응.",
                           kind="fact_create", text="My home server has to be quiet")
        todo = await saved(agent, "Bee 할 일에 'Note the bug'을 넣을까요?", "네, 넣어 줘.",
                           kind="todo_create", text="Note the bug")
        return fact, todo

    fact, todo = asyncio.run(main())
    assert fact.startswith("Saved to Bee: fact 'My home server has to be quiet'")
    assert todo.startswith("Saved to Bee: todo 'Note the bug'")
    assert ["facts", "create", "--text", "My home server has to be quiet", "--json"] in calls(fake_bee)
    assert ["todos", "create", "--text", "Note the bug", "--json"] in calls(fake_bee)


def test_take_it_out_undoes_the_last_write_of_the_call(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        undone = await agent.undo_bee_write(None)
        again = await agent.undo_bee_write(None)
        return undone, again, rec

    undone, again, rec = asyncio.run(main())
    assert undone.startswith("Took it out of Bee: todo 'Note the bug'.") and again.startswith("Nothing")
    assert writes_to_bee(fake_bee) == [["todos", "create"], ["todos", "delete"]]
    assert ["todos", "delete", "99", "--json"] in calls(fake_bee)  # the todo the write created
    assert [e["type"] for e in events(rec) if e["type"].startswith("bee_write")] == ["bee_write", "bee_write_undone"]
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["undone"]


def test_take_it_out_works_when_bee_answers_with_the_bare_todo(tmp_path, fake_bee, monkeypatch):
    """The Bee CLI prints the API's answer as it came, and it may be the new todo itself rather than {"todo": ...}
    (the CLI's own parser takes both); the undo needs its id either way."""
    monkeypatch.setenv("FAKE_BEE_BARE_TODO", "1")

    async def main():
        agent, _ = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        return await agent.undo_bee_write(None)

    assert asyncio.run(main()).startswith("Took it out of Bee: todo 'Note the bug'.")
    assert ["todos", "delete", "99", "--json"] in calls(fake_bee)


def test_take_it_out_never_deletes_by_an_id_that_is_not_a_todo_s(tmp_path, fake_bee, monkeypatch):
    """Third review of this round's fixes, finding 4: an answer that is not a todo (here the suggestion, id 34) must
    not be read as one; deleting todo 34 would take out some other todo."""
    monkeypatch.setenv("FAKE_BEE_ACCEPT_ANSWER", "suggestion")

    async def main():
        agent, _ = await in_a_call(tmp_path)
        await saved(agent, "Shall I add Bee's suggested todo 'Ask about bonus terms'?", "Yes.",
                    kind="todo_accept", suggestion_id=34, text="Ask about bonus terms")
        return await agent.undo_bee_write(None)

    assert asyncio.run(main()).startswith("Could not take") and writes_to_bee(fake_bee) == [
        ["todos", "accept-suggestion"]]


def test_undoing_a_fact_fix_works_when_bee_answers_with_the_bare_fact(tmp_path, fake_bee, monkeypatch):
    """Round 2 (pre-deploy review): the fact's earlier text was read only from {"fact": ...}; the Bee CLI 0.7.3's own
    parser also takes the bare fact, and then the undo said the earlier text was not known."""
    monkeypatch.setenv("FAKE_BEE_BARE_FACT", "1")

    async def main():
        agent, _ = await in_a_call(tmp_path)
        await saved(agent, "Shall I fix it in Bee to 'Bonus is 10%'?", "Yes.",
                    kind="fact_update", fact_id=7, text="Bonus is 10%")
        return await agent.undo_bee_write(None)

    assert asyncio.run(main()).startswith("Took it out of Bee")
    updates = [c for c in calls(fake_bee) if c[:2] == ["facts", "update"]]
    assert updates[1][3:7] == ["--text", "Bonus is 15%", "--confirmed", "false"]


def test_undoing_a_fact_fix_puts_the_fact_back_as_it_was(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        await saved(agent, "Shall I fix it in Bee to 'Bonus is 10%'?", "Yes.",
                    kind="fact_update", fact_id=7, text="Bonus is 10%")
        await agent.undo_bee_write(None)

    asyncio.run(main())
    updates = [c for c in calls(fake_bee) if c[:2] == ["facts", "update"]]
    assert updates[0][3:5] == ["--text", "Bonus is 10%"] and updates[1][3:7] == ["--text", "Bonus is 15%",
                                                                                "--confirmed", "false"]


def test_a_failed_write_is_said_and_retried_after_the_call(tmp_path, fake_bee, monkeypatch):
    fail_once(tmp_path, monkeypatch, "todos create")

    async def main():
        agent, rec = await in_a_call(tmp_path)
        said = await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        retried = await BeeWrites(rec.dir).retry_failed()
        return said, retried, rec

    said, retried, rec = asyncio.run(main())
    assert "did not go through" in said and "tried again after the call" in said
    assert [w.status for w in retried] == ["ok"] and [w.status for w in BeeWrites(rec.dir).all()] == ["ok"]
    assert writes_to_bee(fake_bee) == [["todos", "create"], ["todos", "create"]]  # one failed, one went in


def test_a_todo_bee_stored_before_its_error_is_not_written_again(tmp_path, fake_bee, monkeypatch):
    """Second review of this round's fixes, finding 2: the Bee CLI exits with an error for any answer that is not a
    success, also a gateway error after Bee stored the todo. The retry after the call looks for it first."""
    monkeypatch.setenv("FAKE_BEE_TODOS", str(tmp_path / "todos.json"))
    (tmp_path / "fail-after").write_text("todos create")
    monkeypatch.setenv("FAKE_BEE_FAIL_AFTER_ONCE", str(tmp_path / "fail-after"))

    async def main():
        agent, rec = await in_a_call(tmp_path)
        said = await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        return said, await BeeWrites(rec.dir).retry_failed(), rec

    said, retried, rec = asyncio.run(main())
    assert "did not go through" in said
    assert [w.status for w in retried] == ["ok"] and [w.status for w in BeeWrites(rec.dir).all()] == ["ok"]
    assert writes_to_bee(fake_bee) == [["todos", "create"]]  # written once
    assert [t["text"] for t in json.loads((tmp_path / "todos.json").read_text())] == ["Note the bug"]


def test_the_todo_bee_stored_is_found_past_the_first_page(tmp_path, fake_bee, monkeypatch):
    """Third review of this round's fixes, finding 2: Bee lists todos a page at a time and promises no order."""
    import time

    todos = tmp_path / "todos.json"
    old = int(time.time() * 1000) - 10 * 86_400_000
    todos.write_text(json.dumps([{"id": i, "text": f"Old {i}", "created_at": old, "completed": False}
                                 for i in range(50)]))
    monkeypatch.setenv("FAKE_BEE_TODOS", str(todos))
    (tmp_path / "fail-after").write_text("todos create")
    monkeypatch.setenv("FAKE_BEE_FAIL_AFTER_ONCE", str(tmp_path / "fail-after"))

    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        await BeeWrites(rec.dir).retry_failed()
        return rec

    rec = asyncio.run(main())
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["ok"] and writes_to_bee(fake_bee) == [["todos", "create"]]
    assert [c for c in calls(fake_bee) if c[:2] == ["todos", "list"]][-1][-3:] == ["--cursor", "50", "--json"]


def test_a_retry_cut_off_while_asking_bee_is_left_to_the_user(tmp_path, fake_bee, monkeypatch):
    """The retry after the call has a few seconds; cut off while it asks Bee, the write may be there."""
    fail_once(tmp_path, monkeypatch, "todos create")
    monkeypatch.setenv("FAKE_BEE_SLOW", "todos list 3")

    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(BeeWrites(rec.dir).retry_failed(), 0.5)
        return rec

    rec = asyncio.run(main())
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["unknown"]
    assert writes_to_bee(fake_bee) == [["todos", "create"]]


def test_a_refused_todo_bee_cannot_be_asked_about_is_left_to_the_user(tmp_path, fake_bee, monkeypatch):
    fail_once(tmp_path, monkeypatch, "todos create")
    monkeypatch.setenv("FAKE_BEE_DOWN", "todos list")

    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        return await BeeWrites(rec.dir).retry_failed(), rec

    retried, rec = asyncio.run(main())
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["unknown"]
    assert writes_to_bee(fake_bee) == [["todos", "create"]]  # not written again
    body = writer.summary_notification(BeeWrites(rec.dir).all(), call_language("en"))["aps"]["alert"]["body"]
    assert body == "Not sure it went in, check Bee: todo 'Note the bug'"


@pytest.mark.parametrize("suggestion,tries", [(34, 2), (35, 1)])  # still suggested; no longer suggested
def test_a_refused_accept_is_tried_again_only_while_bee_still_suggests_it(tmp_path, fake_bee, monkeypatch,
                                                                         suggestion, tries):
    fail_once(tmp_path, monkeypatch, "todos accept-suggestion")

    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, "Shall I add Bee's suggested todo 'Ask about bonus terms'?", "Yes.",
                    kind="todo_accept", suggestion_id=suggestion, text="Ask about bonus terms")
        await BeeWrites(rec.dir).retry_failed()
        return rec

    rec = asyncio.run(main())
    assert writes_to_bee(fake_bee) == [["todos", "accept-suggestion"]] * tries
    assert [w.status for w in BeeWrites(rec.dir).all()] == (["ok"] if tries == 2 else ["unknown"])


def test_a_write_bee_did_not_confirm_is_never_written_again(tmp_path, fake_bee, monkeypatch):
    """Review of this round's fixes, finding 5: Bee took the todo but answered after the write stopped waiting.
    Retrying it would make the same todo twice, so it is said as maybe written and left to the user."""
    monkeypatch.setenv("FAKE_BEE_SLOW", "todos create 3")
    monkeypatch.setattr(cli, "WRITE_TIMEOUT", 0.5)

    async def main():
        agent, rec = await in_a_call(tmp_path)
        said = await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        retried = await BeeWrites(rec.dir).retry_failed()
        return said, retried, rec

    said, retried, rec = asyncio.run(main())
    assert "may or may not be in Bee" in said and "tried again" not in said
    assert retried == [] and writes_to_bee(fake_bee) == [["todos", "create"]]
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["unknown"]
    (event,) = [e for e in events(rec) if e["type"] == "bee_write"]
    assert (event["result"], event["may_have_gone_in"]) == ("failed", True)  # contract section 8 knows ok and failed
    body = writer.summary_notification(BeeWrites(rec.dir).all(), call_language("en"))["aps"]["alert"]["body"]
    assert body == "Not sure it went in, check Bee: todo 'Note the bug'"


def test_a_write_cut_off_by_the_end_of_the_call_is_kept_as_maybe_written(tmp_path, fake_bee, monkeypatch):
    """The tool was cancelled while Bee was writing (LiveKit cancels a session's tools when it tears the session
    down; a session that drains waits for them instead). Bee may have it, so it is not written again."""
    monkeypatch.setenv("FAKE_BEE_SLOW", "todos create 3")

    async def main():
        agent, rec = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), ("user", ASKED[1]))
        saving = asyncio.create_task(agent.save_to_bee(None, proposal=number))
        for _ in range(100):  # until Bee has the todo
            await asyncio.sleep(0.05)
            if writes_to_bee(fake_bee):
                break
        saving.cancel()
        await asyncio.gather(saving, return_exceptions=True)
        return rec, await BeeWrites(rec.dir).retry_failed()

    rec, retried = asyncio.run(main())
    assert retried == [] and writes_to_bee(fake_bee) == [["todos", "create"]]
    assert [w.status for w in BeeWrites(rec.dir).all()] == ["unknown"]
    (event,) = [e for e in events(rec) if e["type"] == "bee_write"]
    assert (event["result"], event["may_have_gone_in"]) == ("failed", True)


def test_a_write_cut_off_mid_way_is_never_repeated(tmp_path, fake_bee):
    """Server review finding 18: "applying" is recorded before the Bee call, so a rerun cannot create the same todo
    twice when the process stopped between the Bee call and its result."""
    folder = tmp_path / "workspace" / "2026-10-15" / "2100"
    folder.mkdir(parents=True)
    (folder / "bee-writes.jsonl").write_text(json.dumps(
        {"key": "w1", "status": "applying", "item": {"kind": "todo_create", "text": "Note the bug"},
         "question": ASKED[0], "answer": ASKED[1], "at": "2026-10-15T21:05:00"}) + "\n")
    assert asyncio.run(BeeWrites(folder).retry_failed()) == [] and writes_to_bee(fake_bee) == []
    assert [w.status for w in BeeWrites(folder).all()] == ["applying"]


def test_items_queued_before_the_spoken_yes_was_kept_are_never_written(tmp_path, fake_bee, capsys):
    """The old queue (bee-queue.jsonl) kept no record of the user's yes; the writer refuses its items."""
    from tests.test_beelink import write_queue

    folder = tmp_path / "workspace" / "2026-10-12" / "1530"
    write_queue(folder)
    assert asyncio.run(writer.run(folder, apply=True)) == []
    assert writes_to_bee(fake_bee) == [] and "no spoken yes was recorded" in capsys.readouterr().out


@pytest.mark.parametrize("language,body", [
    ("en", "Added to Bee: todo 'Note the bug' and 1 more"),
    ("ko", "Bee에 넣었어요: 할 일 '버그 기록' 외 1개")])
def test_one_notification_after_the_call_lists_what_went_into_bee(tmp_path, fake_bee, language, body):
    from beelink.writer import summary_notification

    async def main():
        question = ("Want me to add 'Note the bug' and 'Call Dana' to your Bee todos?" if language == "en"
                    else "Bee에 할 일로 '버그 기록'과 '다나에게 전화'를 넣을까요?")
        agent, rec = await in_a_call(tmp_path, language=language)
        first, second = ("Note the bug", "Call Dana") if language == "en" else ("버그 기록", "다나에게 전화")
        one = await proposed(agent, kind="todo_create", text=first)
        two = await proposed(agent, kind="todo_create", text=second)
        await say(agent, ("assistant", question), ("user", "Yes." if language == "en" else "네."))
        await agent.save_to_bee(None, proposal=one)
        await agent.save_to_bee(None, proposal=two)
        return rec

    rec = asyncio.run(main())
    payload = summary_notification(BeeWrites(rec.dir).all(), call_language(language))
    assert payload["aps"]["alert"]["body"] == body
    assert payload["aps"]["thread-id"] == "bee" and "category" not in payload["aps"]
    assert summary_notification([], call_language(language)) is None  # nothing went in: no notification


@pytest.mark.parametrize("language,body", [
    ("en", "Added to Bee: todo 'Note the bug'. Not sure it went in, check Bee: todo 'Call Dana'"),
    ("ko", "Bee에 넣었어요: 할 일 '버그 기록'. 들어갔는지 모르니 Bee에서 확인해 주세요: 할 일 '다나에게 전화'")])
def test_the_notification_names_what_bee_may_have_in_the_person_s_language(language, body):
    from beelink.writer import BeeWrite, summary_notification

    first, second = ("Note the bug", "Call Dana") if language == "en" else ("버그 기록", "다나에게 전화")
    writes = [BeeWrite("w1", {"kind": "todo_create", "text": first}, "q", "a", "ok"),
              BeeWrite("w2", {"kind": "todo_create", "text": second}, "q", "a", "unknown")]
    assert summary_notification(writes, call_language(language))["aps"]["alert"]["body"] == body


@pytest.mark.parametrize("answer", ["나중에요.", "글쎄요...", "잠깐만요, 다시 생각해 볼게요.",
                                    "Bee에 할 일로 '버그 기록'을 넣을까요?", "Hmm, let me think.", "Later, maybe."])
def test_an_answer_that_is_not_a_yes_saves_nothing(tmp_path, fake_bee, answer):
    """The pre-deploy review (2026-10-15): a hedge, a "later", or the AI's own question heard back was let through
    because only a no was refused. Saving needs a yes."""
    async def main():
        korean = any("가" <= c <= "힣" for c in answer)
        agent, _ = await in_a_call(tmp_path, language="ko" if korean else "en")
        question = "Bee에 할 일로 '버그 기록'을 넣을까요?" if korean else ASKED[0]
        return await saved(agent, question, answer, kind="todo_create", text="버그 기록" if korean else "Note the bug")

    said = asyncio.run(main())
    assert said.startswith("Not saved") and writes_to_bee(fake_bee) == []


@pytest.mark.parametrize("answer", ["네.", "응, 넣어줘.", "음, 네 넣어 주세요.", "넣어 줘.", "좋아요!", "맞아요.",
                                    "그래, 부탁해.", "Yes, please.", "Sure.", "Okay, go ahead.", "Please do.",
                                    "Yeah."])
def test_the_usual_ways_of_saying_yes_save(tmp_path, fake_bee, answer):
    async def main():
        korean = any("가" <= c <= "힣" for c in answer)
        agent, _ = await in_a_call(tmp_path, language="ko" if korean else "en")
        question = "Bee에 할 일로 '버그 기록'을 넣을까요?" if korean else ASKED[0]
        return await saved(agent, question, answer, kind="todo_create", text="버그 기록" if korean else "Note the bug")

    assert asyncio.run(main()).startswith("Saved to Bee")


def test_the_call_ai_waits_at_most_15_s_on_bee_and_background_jobs_are_not_held_to_it():
    """The user's rule (2026-10-15): what the call AI waits on in silence has a 15 s ceiling; reads are shorter."""
    from beelink import reads

    assert cli.CALL_WAIT_S == 15.0 and cli.WRITE_TIMEOUT == cli.CALL_WAIT_S
    assert reads.READ_TIMEOUT_S <= cli.CALL_WAIT_S


def test_the_fact_read_before_a_fix_waits_no_longer_than_the_call_allows(tmp_path, fake_bee, monkeypatch):
    """Before a fact is fixed its old text is read (so the fix can be taken back); that read is held to the
    call's ceiling too, and a slow one only means the fix cannot be taken back."""
    import time

    monkeypatch.setenv("FAKE_BEE_SLOW", "facts get 3")
    monkeypatch.setattr(cli, "CALL_WAIT_S", 0.5)

    async def main():
        agent, rec = await in_a_call(tmp_path, language="en")
        started = time.monotonic()
        said = await saved(agent, "Shall I fix it in Bee to 'Bonus is 10%'?", "Yes.",
                           kind="fact_update", fact_id=7, text="Bonus is 10%")
        return said, time.monotonic() - started

    said, took = asyncio.run(main())
    assert said.startswith("Saved to Bee") and took < 2.5


class Context:
    """Stands in for LiveKit's RunContext: records the filler lines a tool asks for while it waits."""

    def __init__(self) -> None:
        self.fillers = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def with_filler(self, source, *, delay=0, interval=None, max_steps=None):
        self.fillers.append((source, delay, interval))
        yield


def test_every_tool_the_call_ai_waits_on_says_one_line_after_2_s(tmp_path, fake_bee):
    """The user's rule (2026-10-15): one general rule for every tool that waits on something outside the call,
    not a line for Bee alone. LiveKit plays it once the call has been silent for 2 s."""
    async def main():
        agent, _ = await in_a_call(tmp_path, language="ko")
        fillers = {}
        for name, use in (("search_bee", lambda c: agent.search_bee(c, query="등산")),
                          ("bee_today", lambda c: agent.bee_today(c)),
                          ("bee_conversation", lambda c: agent.bee_conversation(c, conversation_id=1))):
            context = Context()
            await use(context)
            fillers[name] = context.fillers
        context = Context()
        number = await agent.propose_bee_save(context, kind="todo_create", text="버그 기록")
        fillers["propose_bee_save"] = context.fillers  # waits on nothing outside the call
        number = int(re.search(r"Proposal (\d+)", number).group(1))
        await say(agent, ("assistant", "Bee에 할 일로 '버그 기록'을 넣을까요?"), ("user", "네."))
        for name, use in (("save_to_bee", lambda c: agent.save_to_bee(c, proposal=number)),
                          ("undo_bee_write", lambda c: agent.undo_bee_write(c))):
            context = Context()
            await use(context)
            fillers[name] = context.fillers
        return fillers

    fillers = asyncio.run(main())
    line = call_language("ko").waiting
    assert line == "잠깐만요, 확인하고 있어요." and call_language("en").waiting == "One moment, I'm checking."
    for name in ("search_bee", "bee_today", "bee_conversation", "save_to_bee", "undo_bee_write"):
        assert fillers[name] == [(line, 2.0, None)], name
    assert fillers["propose_bee_save"] == []


# ---------- 2026-10-17: Bee writes also keep and delete facts, complete to-dos and turn down suggestions ----------

def test_a_fact_the_user_wants_kept_is_made_and_confirmed_and_can_be_taken_back(tmp_path, fake_bee):
    """fact_create: `bee facts create` makes it unconfirmed, so it is confirmed at once (the user said yes to keeping
    it); taking it back deletes the fact Bee made."""
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "I'm allergic to peanuts, by the way.")])
        said = await saved(agent, "Shall I keep 'Allergic to peanuts' in Bee?", "Yes, please.",
                           kind="fact_create", text="Allergic to peanuts")
        undone = await agent.undo_bee_write(None)
        return said, undone, rec

    said, undone, rec = asyncio.run(main())
    assert said.startswith("Saved to Bee: fact 'Allergic to peanuts'.")
    assert ["facts", "create", "--text", "Allergic to peanuts", "--json"] in calls(fake_bee)
    assert ["facts", "confirm", "200", "--json"] in calls(fake_bee) and ["facts", "delete", "200", "--json"] in calls(fake_bee)
    assert undone.startswith("Took it out of Bee: fact 'Allergic to peanuts'.")
    (event,) = [e for e in events(rec) if e["type"] == "bee_write"]
    assert (event["kind"], event["result"]) == ("fact_create", "ok")


def test_a_wrong_fact_is_deleted_only_after_a_yes_and_cannot_be_undone(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path)
        number = await proposed(agent, kind="fact_delete", text="Bonus is 15%", fact_id=7)
        before = await agent.save_to_bee(None, proposal=number)  # nothing asked yet
        await say(agent, ("assistant", "Shall I delete 'Bonus is 15%' from Bee?"), ("user", "Yes, delete it."))
        said = await agent.save_to_bee(None, proposal=number)
        undone = await agent.undo_bee_write(None)
        return before, said, undone, rec

    before, said, undone, rec = asyncio.run(main())
    assert before.startswith("Not saved") and said.startswith("Saved to Bee: fact 'Bonus is 15%'")
    assert writes_to_bee(fake_bee) == [["facts", "delete"]]  # the undo touched nothing
    assert "cannot be undone" in undone


def test_a_todo_the_user_did_is_marked_done_and_can_be_reopened(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "그거 주문했어.")], language="ko")
        said = await saved(agent, "Bee에서 '삼각대 주문하기'를 끝낸 걸로 할까요?", "응, 그렇게 해 줘.",
                           kind="todo_complete", text="삼각대 주문하기", todo_id=51)
        undone = await agent.undo_bee_write(None)
        return said, undone

    said, undone = asyncio.run(main())
    assert said.startswith("Saved to Bee: todo '삼각대 주문하기'") and undone.startswith("Took it out of Bee")
    assert ["todos", "complete", "51", "--json"] in calls(fake_bee)
    assert ["todos", "update", "51", "--completed", "false", "--json"] in calls(fake_bee)


def test_a_suggestion_turned_down_is_dismissed_and_cannot_be_undone(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        said = await saved(agent, "Shall I remove Bee's suggestion 'Ask about bonus terms'?", "Yes.",
                           kind="todo_dismiss", text="Ask about bonus terms", suggestion_id=34)
        undone = await agent.undo_bee_write(None)
        return said, undone

    said, undone = asyncio.run(main())
    assert said.startswith("Saved to Bee") and "cannot be undone" in undone
    assert writes_to_bee(fake_bee) == [["todos", "dismiss-suggestion"]]


@pytest.mark.parametrize("kind, item, missing", [("fact_delete", {}, "fact_id"), ("todo_complete", {}, "todo_id"),
                                                 ("todo_dismiss", {}, "suggestion_id")])
def test_each_new_kind_needs_the_id_of_what_it_changes(tmp_path, fake_bee, kind, item, missing):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        return await agent.propose_bee_save(None, kind=kind, text="Something", **item)

    assert asyncio.run(main()) == f"{missing} is required."


def test_a_fact_bee_made_before_its_error_is_not_made_again(tmp_path, fake_bee, monkeypatch):
    """As for a todo: the retry after the call looks for the fact by its text before making it again."""
    monkeypatch.setenv("FAKE_BEE_FACTS", str(tmp_path / "facts.json"))
    flag = tmp_path / "fail-after"
    flag.write_text("facts create")
    monkeypatch.setenv("FAKE_BEE_FAIL_AFTER_ONCE", str(flag))

    async def main():
        agent, rec = await in_a_call(tmp_path)
        said = await saved(agent, "Shall I keep 'Allergic to peanuts' in Bee?", "Yes.", kind="fact_create",
                           text="Allergic to peanuts")
        retried = await BeeWrites(rec.dir).retry_failed()
        return said, retried

    said, retried = asyncio.run(main())
    assert "did not go through" in said and [w.status for w in retried] == ["ok"]
    assert [c[:2] for c in calls(fake_bee)].count(["facts", "create"]) == 1


@pytest.mark.parametrize("language,body", [
    ("en", "Added to Bee: fact 'Allergic to peanuts'. Marked done in Bee: todo 'Order the tripod'. Removed from "
           "Bee: fact 'Bonus is 15%'"),
    ("ko", "Bee에 넣었어요: 사실 '땅콩 알레르기'. Bee에서 끝냈어요: 할 일 '삼각대 주문'. Bee에서 뺐어요: 사실 '보너스 15%'")])
def test_the_notification_says_what_was_done_and_what_was_taken_out(language, body):
    from beelink.writer import BeeWrite, summary_notification

    texts = ("Allergic to peanuts", "Order the tripod", "Bonus is 15%") if language == "en" else \
        ("땅콩 알레르기", "삼각대 주문", "보너스 15%")
    writes = [BeeWrite("w1", {"kind": "fact_create", "text": texts[0]}, "q", "a", "ok"),
              BeeWrite("w2", {"kind": "todo_complete", "text": texts[1], "todo_id": 51}, "q", "a", "ok"),
              BeeWrite("w3", {"kind": "fact_delete", "text": texts[2], "fact_id": 7}, "q", "a", "ok")]
    assert summary_notification(writes, call_language(language))["aps"]["alert"]["body"] == body


def test_in_a_korean_call_the_example_question_is_korean_with_the_english_item_inside_it(tmp_path, fake_bee):
    """Measured 2026-10-17 (Haiku 5.5, a Korean call): told only the English example, the AI asked "그 알레르기 Bee에
    기억해 둘까요? 'Has a peanut allergy'로 저장하려고 하는데..." with the item outside the question, so the user's yes
    saved nothing and it had to ask again. The example it is given is now a Korean question holding the item."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("user", "나 땅콩 알레르기 있어.")], "ko")
        said = await agent.propose_bee_save(None, kind="fact_create", text="Has a peanut allergy")
        english, _ = await in_a_call(tmp_path / "en", [("user", "I'm allergic to peanuts.")])
        return said, await english.propose_bee_save(None, kind="fact_create", text="Has a peanut allergy")

    korean, english = asyncio.run(main())
    assert "Bee에 'Has a peanut allergy' 기억해 둘까요?" in korean and "one sentence that ends with" in korean
    assert "Shall I keep 'Has a peanut allergy' in Bee?" in english
