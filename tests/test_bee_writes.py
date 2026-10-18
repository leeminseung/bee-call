"""Bee writes happen during the call, right after the user's spoken yes (contract section 6, the user's decision of
2026-10-15): the AI's last turn before the user's last words and those words are kept with each write, the user can
take the last one back, a failed write is retried after the call, and one notification after the call says what went
into Bee.

Whether the user said yes is the call AI's to judge, from its instructions (the user's decision of 2026-10-18): the
code reads no meaning. It keeps the kind and the ids, reads what Bee has under an id before changing it, writes an
item once a call, and keeps the exchange by position. A deleted fact or a dismissed suggestion can be taken back too:
it is made again.

The fake `bee` (tests/fakebee/bee) stands in for the Bee CLI; nothing reaches a real Bee account.
"""

import asyncio
import json
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


async def saved(agent, question: str, answer: str, **item) -> str:
    """The AI asks `question`, hears `answer`, and saves the item; what save_to_bee told the AI."""
    await say(agent, ("assistant", question), ("user", answer))
    return await agent.save_to_bee(None, **item)


ASKED = ("Want me to add 'note the bug' to your Bee todos?", "Yes, please.")


def test_a_save_writes_at_once_and_keeps_the_ai_s_last_turn_and_the_user_s_answer(tmp_path, fake_bee):
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


def test_an_answer_said_in_two_breaths_is_kept_whole_and_by_position_only(tmp_path, fake_bee):
    """Speech is sometimes split into two user turns; the answer kept is both, not only the last part. What the AI
    says after the answer, in the reply that saves, is not the question: the question is the AI's turn before the
    user's last words, whatever it says."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        await say(agent, ("assistant", ASKED[0]), ("user", "Yes."), ("user", "Please add it."),
                  ("assistant", "Okay, adding it."))
        return await agent.save_to_bee(None, kind="todo_create", text="Note the bug"), rec

    said, rec = asyncio.run(main())
    assert said.startswith("Saved to Bee")
    assert [(w.question, w.answer) for w in BeeWrites(rec.dir).all()] == [(ASKED[0], "Yes. Please add it.")]


def test_a_save_before_the_user_said_anything_keeps_no_exchange(tmp_path, fake_bee):
    """The code does not judge whether the user said yes: that is the AI's, from its instructions. The record shows
    what came before the save as it was, here nothing."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        return await agent.save_to_bee(None, kind="todo_create", text="Note the bug"), rec

    said, rec = asyncio.run(main())
    assert said.startswith("Saved to Bee")
    assert [(w.question, w.answer) for w in BeeWrites(rec.dir).all()] == [(None, None)]


def test_an_item_is_written_once_a_call_until_it_is_taken_back(tmp_path, fake_bee):
    """Measured 2026-10-17 (Haiku 5.5, a Korean call): the AI saved the same fact twice for one yes. The same item
    (the kind, the text compared loosely, what it changes) is written once a call; taken back, it may be saved
    again."""
    async def main():
        agent, _ = await in_a_call(tmp_path, [("user", "나 땅콩 알레르기 있어.")], "ko")
        await say(agent, ("assistant", "Bee에 'Has a peanut allergy'로 기억해 둘까요?"), ("user", "응."))
        both = await asyncio.gather(agent.save_to_bee(None, kind="fact_create", text="Has a peanut allergy."),
                                    agent.save_to_bee(None, kind="fact_create", text="Has a peanut allergy"))
        undone = await agent.undo_bee_write(None)
        await say(agent, ("assistant", "다시 Bee에 'Has a peanut allergy'로 기억해 둘까요?"), ("user", "응, 넣어 줘."))
        return both, undone, await agent.save_to_bee(None, kind="fact_create", text="Has a peanut allergy")

    (saved, again), undone, after_undo = asyncio.run(main())
    assert saved.startswith("Saved to Bee") and again.startswith("Not saved again")
    assert undone.startswith("Took it back") and after_undo.startswith("Saved to Bee")  # taken back, then wanted
    assert [c for c in writes_to_bee(fake_bee) if c == ["facts", "create"]] == [["facts", "create"]] * 2


def test_two_saves_at_once_are_kept_apart(tmp_path, fake_bee):
    """LiveKit runs the tool calls of one reply at the same time (livekit/agents/voice/generation.py); each write
    keeps its own record, so one cannot take the other's result or be undone in its place."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        await say(agent, ("assistant", "Shall I fix both in Bee: 'Bonus is 10%' and 'Starts in May'?"),
                  ("user", "Yes."))
        await asyncio.gather(agent.save_to_bee(None, kind="fact_update", fact_id=7, text="Bonus is 10%"),
                             agent.save_to_bee(None, kind="fact_update", fact_id=8, text="Starts in May"))
        return rec

    rec = asyncio.run(main())
    assert sorted(w.item["fact_id"] for w in BeeWrites(rec.dir).all()) == [7, 8]
    assert len({e["key"] for e in events(rec) if e["type"] == "bee_write"}) == 2


def test_take_it_out_undoes_the_last_write_of_the_call(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, *ASKED, kind="todo_create", text="Note the bug")
        undone = await agent.undo_bee_write(None)
        again = await agent.undo_bee_write(None)
        return undone, again, rec

    undone, again, rec = asyncio.run(main())
    assert undone.startswith("Took it back: todo 'Note the bug'.") and again.startswith("No change to Bee")
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

    assert asyncio.run(main()).startswith("Took it back: todo 'Note the bug'.")
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

    assert asyncio.run(main()).startswith("Could not take back") and writes_to_bee(fake_bee) == [
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

    assert asyncio.run(main()).startswith("Took it back")
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
        await say(agent, ("assistant", ASKED[0]), ("user", ASKED[1]))
        saving = asyncio.create_task(agent.save_to_bee(None, kind="todo_create", text="Note the bug"))
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
        await say(agent, ("assistant", question), ("user", "Yes." if language == "en" else "네."))
        await agent.save_to_bee(None, kind="todo_create", text=first)
        await agent.save_to_bee(None, kind="todo_create", text=second)
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
        reads = {tool.info.name: tool for tool in agent.tools if tool.info.name.startswith("bee_")}
        for name, use in (("bee_search", lambda c: reads["bee_search"](raw_arguments={"query": "등산"}, context=c)),
                          ("bee_today", lambda c: reads["bee_today"](raw_arguments={}, context=c)),
                          ("bee_conversation", lambda c: reads["bee_conversation"](raw_arguments={"id": 1},
                                                                                     context=c))):
            context = Context()
            await use(context)
            fillers[name] = context.fillers
        await say(agent, ("assistant", "Bee에 할 일로 '버그 기록'을 넣을까요?"), ("user", "네."))
        for name, use in (("save_to_bee", lambda c: agent.save_to_bee(c, kind="todo_create", text="버그 기록")),
                          ("undo_bee_write", lambda c: agent.undo_bee_write(c))):
            context = Context()
            await use(context)
            fillers[name] = context.fillers
        return fillers

    fillers = asyncio.run(main())
    line = call_language("ko").waiting
    assert line == "잠깐만요, 확인하고 있어요." and call_language("en").waiting == "One moment, I'm checking."
    for name in ("bee_search", "bee_today", "bee_conversation", "save_to_bee", "undo_bee_write"):
        assert fillers[name] == [(line, 2.0, None)], name


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
    assert undone.startswith("Took it back: fact 'Allergic to peanuts'.")
    (event,) = [e for e in events(rec) if e["type"] == "bee_write"]
    assert (event["kind"], event["result"]) == ("fact_create", "ok")


def test_a_deleted_fact_can_be_brought_back_confirmed(tmp_path, fake_bee):
    """The user's decision of 2026-10-18: Bee cannot restore a deleted fact (bee facts has no undelete, CLI 0.7.3), so
    the write keeps it as Bee had it, and taking it back makes it again with the same text, confirmed (a new id)."""
    async def main():
        agent, rec = await in_a_call(tmp_path)
        said = await saved(agent, "Shall I delete 'Bonus is 15%' from Bee?", "Yes, delete it.",
                           kind="fact_delete", text="Bonus is 15%", fact_id=7)
        undone = await agent.undo_bee_write(None)
        return said, undone, rec

    said, undone, rec = asyncio.run(main())
    assert said.startswith("Deleted from Bee: fact 'Bonus is 15%'.")
    assert undone.startswith("Brought it back: fact 'Bonus is 15%' is in Bee again, confirmed.")
    assert writes_to_bee(fake_bee) == [["facts", "delete"], ["facts", "create"], ["facts", "confirm"]]
    assert ["facts", "create", "--text", "Bonus is 15%", "--json"] in calls(fake_bee)
    (write,) = BeeWrites(rec.dir).all()
    assert write.status == "undone" and write.undo["fact"]["id"] == 200  # the fact made again
    assert [e["type"] for e in events(rec) if e["type"].startswith("bee_write")] == ["bee_write", "bee_write_undone"]


def test_a_todo_the_user_did_is_marked_done_and_can_be_reopened(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "그거 했어.")], language="ko")
        said = await saved(agent, "Bee에서 'Reply to the recruiter' 끝난 걸로 할까요?", "응, 그렇게 해 줘.",
                           kind="todo_complete", text="Reply to the recruiter", todo_id=51)
        undone = await agent.undo_bee_write(None)
        return said, undone

    said, undone = asyncio.run(main())
    assert said.startswith("Marked done in Bee: todo 'Reply to the recruiter'") and undone.startswith("Took it back")
    assert ["todos", "complete", "51", "--json"] in calls(fake_bee)
    assert ["todos", "update", "51", "--completed", "false", "--json"] in calls(fake_bee)


def test_a_suggestion_turned_down_comes_back_as_an_open_to_do(tmp_path, fake_bee):
    """The user's decision of 2026-10-18: Bee has no way to bring a dismissed suggestion back, so taking the dismissal
    back makes an open to-do with the suggestion's text, and the AI says it came back as a to-do."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        said = await saved(agent, "Shall I remove Bee's suggestion 'Ask about bonus terms'?", "Yes.",
                           kind="todo_dismiss", text="Ask about bonus terms", suggestion_id=34)
        undone = await agent.undo_bee_write(None)
        return said, undone

    said, undone = asyncio.run(main())
    assert said.startswith("Turned down in Bee: todo 'Ask about bonus terms'.")
    assert undone.startswith("Brought it back as a to-do: todo 'Ask about bonus terms'")
    assert writes_to_bee(fake_bee) == [["todos", "dismiss-suggestion"], ["todos", "create"]]
    assert ["todos", "create", "--text", "Ask about bonus terms", "--json"] in calls(fake_bee)


def test_a_change_to_what_bee_has_goes_by_the_id_and_says_what_bee_had(tmp_path, fake_bee):
    """Review of 2026-10-17, finding 1: a delete went by the id the model named, whatever text the user heard (a
    measured call said 'The user lives in Busan' for Bee's 'Lives in Busan'). The item is read from Bee by its id
    first and the write keeps it, so it can be taken back; other words from the AI do not stop the change (the code
    reads no meaning), and the answer says what Bee had, so the AI can tell the user, who can have it back."""
    async def main():
        agent, rec = await in_a_call(tmp_path, [("user", "That bonus thing in Bee is wrong. Delete it.")])
        said = await saved(agent, "Shall I delete 'My bonus is 15 percent' from Bee?", "Yes.",
                           kind="fact_delete", text="My bonus is 15 percent", fact_id=7)
        return said, rec

    said, rec = asyncio.run(main())
    assert said.startswith("Deleted from Bee: fact 'Bonus is 15%'.")
    assert "Bee had it as 'Bonus is 15%', not 'My bonus is 15 percent'" in said
    assert writes_to_bee(fake_bee) == [["facts", "delete"]]
    (write,) = BeeWrites(rec.dir).all()
    assert write.before["text"] == "Bonus is 15%" and write.before["id"] == 7 and write.text == "Bonus is 15%"


@pytest.mark.parametrize("kind, item, down, said", [
    ("fact_delete", {"fact_id": 7}, "facts get", "Could not read fact 7 from Bee"),
    ("todo_complete", {"todo_id": 51}, "todos get", "Could not read todo 51 from Bee"),
    ("todo_dismiss", {"suggestion_id": 99}, None, "Nothing changed: Bee has no todo suggestion 99"),
])
def test_a_change_to_something_bee_cannot_show_changes_nothing(tmp_path, fake_bee, monkeypatch, kind, item, down,
                                                              said):
    if down:
        monkeypatch.setenv("FAKE_BEE_DOWN", down)

    async def main():
        agent, _ = await in_a_call(tmp_path, [("assistant", "Shall I change it in Bee?"), ("user", "Yes.")])
        return await agent.save_to_bee(None, kind=kind, text="Something", **item)

    answer = asyncio.run(main())
    assert said in answer and writes_to_bee(fake_bee) == []


@pytest.mark.parametrize("kind, item, missing", [("fact_delete", {}, "fact_id"), ("todo_complete", {}, "todo_id"),
                                                 ("todo_dismiss", {}, "suggestion_id")])
def test_each_new_kind_needs_the_id_of_what_it_changes(tmp_path, fake_bee, kind, item, missing):
    async def main():
        agent, _ = await in_a_call(tmp_path)
        return await agent.save_to_bee(None, kind=kind, text="Something", **item)

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


@pytest.mark.parametrize("pages, status, creates", [(10, "ok", 1), (1, "unknown", 1)])
def test_the_fact_bee_made_is_looked_for_past_the_first_page(tmp_path, fake_bee, monkeypatch, pages, status,
                                                             creates):
    """Review of 2026-10-17, finding 5: the retry read one page of 50 facts and, not finding the fact there, made it
    again. It looks through the pages as for a todo; with more pages than it looks through, it cannot tell, so the
    fact is left to the user and never made twice."""
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([{"id": n, "text": f"Fact {n}", "created_at": 1, "confirmed": True, "tags": []}
                                 for n in range(1, 61)]))
    monkeypatch.setenv("FAKE_BEE_FACTS", str(facts))
    monkeypatch.setattr(writer, "FACT_PAGES", pages)
    flag = tmp_path / "fail-after"
    flag.write_text("facts create")
    monkeypatch.setenv("FAKE_BEE_FAIL_AFTER_ONCE", str(flag))

    async def main():
        agent, rec = await in_a_call(tmp_path)
        await saved(agent, "Shall I keep 'Allergic to peanuts' in Bee?", "Yes.", kind="fact_create",
                    text="Allergic to peanuts")
        return await BeeWrites(rec.dir).retry_failed()

    retried = asyncio.run(main())
    assert [w.status for w in retried] == [status]
    assert [c[:2] for c in calls(fake_bee)].count(["facts", "create"]) == creates


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




def test_taking_back_a_removal_needs_what_it_removed(tmp_path, fake_bee):
    """A delete or a dismissal is made again from what the write kept of it (`before`); a record without it, such as
    one written before 2026-10-18 kept nothing more, cannot be taken back, and Bee is not touched."""
    folder = tmp_path / "workspace" / "2026-10-15" / "2100"
    folder.mkdir(parents=True)
    (folder / "bee-writes.jsonl").write_text("".join(json.dumps(line) + "\n" for line in (
        {"key": "w1", "status": "applying", "item": {"kind": "fact_delete", "fact_id": 7, "text": "Bonus is 15%"},
         "question": "q", "answer": "a", "at": "2026-10-15T21:05:00"},
        {"key": "w1", "status": "ok", "response": {"deleted": True}, "at": "2026-10-15T21:05:01"})))
    write = asyncio.run(BeeWrites(folder).undo("w1"))
    assert write.status == "undo_failed" and "not known" in write.error and writes_to_bee(fake_bee) == []
