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


async def in_a_call(tmp_path, said: list[tuple[str, str]] = (), language: str = "en"):
    """A Bee person's call after these turns (role, text)."""
    rec = CallRecorder(tmp_path / "workspace", room="call-matt-20261015-210000", now=seoul_now())
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


def test_a_yes_given_before_the_item_was_proposed_is_no_yes(tmp_path, fake_bee):
    """The AI asked first and proposed after the yes: it has to ask again, about the item it fixed."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("assistant", "Shall I add 'call mom' to your Bee todos?"),
                                              ("user", "Yes.")])
        number = await proposed(agent, kind="todo_create", text="Call mom")
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


@pytest.mark.parametrize("answer", ["No, don't.", "아니요, 넣지 마세요."])
def test_a_no_saves_nothing(tmp_path, fake_bee, answer):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        return await saved(agent, *ASKED[:1], answer, kind="todo_create", text="Note the bug")

    assert asyncio.run(main()).startswith("Not saved") and writes_to_bee(fake_bee) == []


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


def test_an_answer_the_conversation_moved_past_needs_the_question_again(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        number = await proposed(agent, kind="todo_create", text="Note the bug")
        await say(agent, ("assistant", ASKED[0]), ("user", "Let me think."), ("assistant", "Sure, take your time."),
                  ("user", "Okay."))
        moved_on = await agent.save_to_bee(None, proposal=number)
        await say(agent, ("assistant", "So, shall I add 'Note the bug' to your Bee todos?"), ("user", "Yes."),
                  ("assistant", "Adding it now."))  # the AI's own words while it saves do not count against it
        return moved_on, await agent.save_to_bee(None, proposal=number)

    moved_on, asked_again = asyncio.run(main())
    assert moved_on.startswith("Not saved") and asked_again.startswith("Saved to Bee")
    assert writes_to_bee(fake_bee) == [["todos", "create"]]


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


def test_the_text_written_to_bee_is_in_the_person_s_language(tmp_path, fake_bee):
    async def main():
        agent, _ = await in_a_call(tmp_path, [("user", "버그를 찾았어요.")], "ko")
        english = await agent.propose_bee_save(None, kind="todo_create", text="Note the bug")
        korean = await saved(agent, "Bee에 할 일로 '버그 기록'을 넣을까요?", "네.", kind="todo_create", text="버그 기록")
        return english, korean

    english, korean = asyncio.run(main())
    assert "Korean" in english and korean.startswith("Saved to Bee")
    assert writes_to_bee(fake_bee) == [["todos", "create"]]


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
        await saved(agent, "Bee suggested the todo 'Ask about bonus terms'. Shall I add it?", "Yes.",
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
