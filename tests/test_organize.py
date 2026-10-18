"""Organizing Bee's memory (the user's decision, 2026-10-17): after a clear yes to one question that offers it, the
call AI adds what came up in the call at once (confirmed facts, open to-dos), or hands the past days to the meta
agent, which reads them in Bee after the call, adds what Bee does not have yet, and sends one notification. Either
way organizing only adds. Whether the user said yes is the call AI's to judge, from its instructions (the user's
decision of 2026-10-18); the code checks what reads no meaning: counts, lengths, items already written in the call,
one organize per answer, and each add of the meta agent's run.

The fake `bee` (tests/fakebee/bee) stands in for the Bee CLI; nothing reaches a real Bee account.
"""

import asyncio
import json

import pytest

from beelink.writer import BeeWrites
from meta.agent import Thinker
from meta.session import MetaSession
from tests.helpers import SEOUL, seoul_now
from tests.test_bee_writes import close_the_calls, events, fake_bee, in_a_call, say, saved  # noqa: F401
from tests.test_beelink import calls
from tests.test_meta import ScriptedModel, on_glm, tool_use

OFFER = "A few things from today are worth keeping. Shall I put them in Bee?"
OFFER_KO = "오늘 이야기 중에 오래 갈 내용이 몇 개 있었는데, Bee에 정리해서 넣어 둘까요?"


def bee_commands(log) -> list[list[str]]:
    return [c[:2] for c in calls(log) if c[:2] in (["facts", "create"], ["facts", "confirm"], ["todos", "create"],
                                                    ["facts", "delete"], ["facts", "update"], ["todos", "delete"])]


def organized(tmp_path, turns, **args) -> tuple[str, object, object]:
    async def main():
        agent, rec = await in_a_call(tmp_path, turns)
        return await agent.organize_bee(None, **args), agent, rec

    return asyncio.run(main())


def test_after_a_clear_yes_the_call_s_facts_and_to_dos_go_into_bee_at_once(tmp_path, fake_bee):
    said, agent, rec = organized(tmp_path, [("user", "I drink coffee first thing, and I should order beans."),
                                            ("assistant", OFFER), ("user", "Yes, please.")],
                                 facts=["Drinks coffee first thing in the morning", "Lives in Seoul"],
                                 todos=["Order coffee beans"])
    assert sorted(map(tuple, bee_commands(fake_bee))) == [("facts", "confirm"), ("facts", "confirm"),
                                                          ("facts", "create"), ("facts", "create"),
                                                          ("todos", "create")]  # made at once, each fact confirmed
    # each with the key of its change, to take one back by (third review of 2026-10-18)
    assert said.startswith("Added to Bee: fact 'Drinks coffee first thing in the morning' (change w1), fact 'Lives in "
                           "Seoul' (change w2), todo 'Order coffee beans' (change w3).")
    assert "they can see and edit them in the Bee app" in said
    writes = BeeWrites(rec.dir).all()  # the app shows each as a Bee write card, where the call says it
    assert [(w.kind, w.text, w.status) for w in writes] == [
        ("fact_create", "Drinks coffee first thing in the morning", "ok"), ("fact_create", "Lives in Seoul", "ok"),
        ("todo_create", "Order coffee beans", "ok")]
    assert {(w.question, w.answer) for w in writes} == {(OFFER, "Yes, please.")}
    assert sorted(e["text"] for e in events(rec) if e["type"] == "bee_write") == sorted(w.text for w in writes)


def test_organizing_reads_no_meaning_but_counts_lengths_and_the_user_s_answer(tmp_path, fake_bee):
    """The code does not judge the question or the answer (the user's decision of 2026-10-18): it organizes after
    the user said something, at most ORGANIZE_MOST items of under 200 characters each, and keeps the AI's last turn
    and the user's answer by position."""
    nothing, _, _ = organized(tmp_path / "none", [], facts=["Drinks coffee"])
    assert nothing.startswith("Not done: the user has said nothing yet")
    many, _, _ = organized(tmp_path / "many", [("assistant", OFFER), ("user", "Sure.")],
                           facts=[f"Fact {n}" for n in range(9)])
    assert many.startswith("Not added: at most 8 items")
    long, _, _ = organized(tmp_path / "long", [("assistant", OFFER), ("user", "Sure.")], facts=["x" * 201])
    assert long.startswith("Not added: each item is one short statement")
    said, _, rec = organized(tmp_path / "any", [("assistant", "그럼 그렇게 할까요?"), ("user", "응, 좋아."),
                                                 ("user", "넣어 줘.")], facts=["Drinks coffee"])
    assert said.startswith("Added to Bee: fact 'Drinks coffee' (change w1).")
    assert [(w.question, w.answer) for w in BeeWrites(rec.dir).all()] == [("그럼 그렇게 할까요?", "응, 좋아. 넣어 줘.")]


def test_one_yes_organizes_once_and_what_the_call_saved_already_is_not_added_again(tmp_path, fake_bee):
    async def main():
        agent, rec = await in_a_call(tmp_path)
        first = await saved(agent, "Bee에 'Drinks coffee'로 기억해 둘까요?", "네.", kind="fact_create",
                            text="Drinks coffee")
        await say(agent, ("assistant", OFFER), ("user", "Yes."))
        second = await agent.organize_bee(None, facts=["Drinks coffee", "Lives in Seoul"])
        third = await agent.organize_bee(None, facts=["Likes hiking"])
        return first, second, third, rec

    first, second, third, rec = asyncio.run(main())
    assert first.startswith("Saved to Bee")
    assert second.startswith("Added to Bee: fact 'Lives in Seoul' (change w2).")
    assert "Already in Bee from this call, so not added again: fact 'Drinks coffee'." in second
    assert third.startswith("Already done for that answer.")
    assert [w.text for w in BeeWrites(rec.dir).all()] == ["Drinks coffee", "Lives in Seoul"]


def test_organizing_the_past_days_is_handed_to_the_meta_agent_when_the_call_ends(tmp_path, fake_bee):
    said, agent, rec = organized(tmp_path, [("assistant", OFFER), ("user", "Yes, and tidy up this week too.")],
                                 scope="recent", days=5)
    assert said.startswith("Started: what Bee recorded over the past 5 days is organized in the background")
    assert agent.organize_request == {"days": 5, "question": OFFER, "answer": "Yes, and tidy up this week too."}
    assert bee_commands(fake_bee) == []  # nothing is written in the call
    (asked,) = [e for e in events(rec) if e["type"] == "bee_organize_requested"]
    assert asked["days"] == 5
    assert organized(tmp_path / "again", [("assistant", OFFER), ("user", "Yes.")], scope="recent",
                     days=30)[1].organize_request["days"] == 7


def test_the_voice_server_wakes_the_meta_agent_with_the_request_after_the_call(tmp_path, monkeypatch):
    from tests.fake_call import FakeCall, use_fakes
    from tests.helpers import signed_in
    from voice import agent

    monkeypatch.setattr(agent, "RING_SECONDS", 0.3)
    monkeypatch.setattr(agent, "OPENING_BY_S", 0.5)
    monkeypatch.setattr(agent, "RINGING_AT_MOST_S", 1.0)
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.session.started["agent"].organize_request = {"days": 3, "question": OFFER, "answer": "Yes."}
        await call.end()

    asyncio.run(main())
    ended, organize = MetaSession(me.workspace).take_events()
    assert (ended["kind"], organize["kind"]) == ("call_ended", "organize_bee")
    assert (organize["call"], organize["days"], organize["question"], organize["answer"], organize["language"]) == (
        ended["call"], 3, OFFER, "Yes.", "en")


# ---------- the meta agent's organize run ----------

REQUEST = {"at": "2026-10-18T15:00:00+09:00", "kind": "organize_bee", "call": "2026-10-18/1450", "days": 3,
           "question": OFFER, "answer": "Yes, please.", "language": "en"}


def done(text: str = "I added two things to Bee: your morning coffee and the dentist."):
    return {"role": "assistant", "content": [{"text": text}]}


def organize(tmp_path, replies, request=None, notified=None, language="en") -> tuple[ScriptedModel, dict]:
    ws = tmp_path / "workspace"
    model = ScriptedModel(replies)
    sent = notified if notified is not None else []

    async def notify(payload):
        sent.append(payload)
        return "notification sent"

    thinker = Thinker(ws, on_glm(model), bee=True, language=language, notify=notify)
    entry = asyncio.run(thinker.think([request or REQUEST], seoul_now()))
    return model, entry


def test_an_organize_run_adds_what_bee_does_not_have_and_sends_one_notification(tmp_path, fake_bee, monkeypatch):
    (tmp_path / "facts.json").write_text(json.dumps([{"id": 300, "text": "Lives in Seoul.", "created_at": 1,
                                                      "confirmed": True, "tags": []}]))
    monkeypatch.setenv("FAKE_BEE_FACTS", str(tmp_path / "facts.json"))  # what Bee keeps already
    sent = []
    model, entry = organize(tmp_path, [
        {"role": "assistant", "content": [tool_use(1, "bee_daily"), tool_use(2, "bee_facts", status="unconfirmed")]},
        {"role": "assistant", "content": [
            tool_use(3, "bee_add_fact", text="Drinks coffee every morning"),
            tool_use(4, "bee_add_fact", text="lives in Seoul"),  # Bee has it in the same words: not added twice
            tool_use(5, "bee_add_todo", text="Book the dentist")]},
        done()], notified=sent)
    offered = [t["toolSpec"]["name"] for t in model.sent[0]["toolConfig"]["tools"]]
    assert "bee_add_fact" in offered and "bee_add_todo" in offered and "bee_daily" in offered
    assert not {"offer_call", "book_call", "write_state"} & set(offered)
    first = model.sent[0]["messages"][0]["content"][0]["text"]
    assert "said yes to organizing what Bee recorded over the past 3 days" in first and OFFER in first
    results = [t["result"] for t in entry["tools"]]
    assert results[2] == "Added to Bee: fact 'Drinks coffee every morning'."
    assert results[3] == "Not added: Bee already has fact 'lives in Seoul'."
    assert results[4] == "Added to Bee: to-do 'Book the dentist'."
    writes = BeeWrites(tmp_path / "workspace" / "meta").all()
    assert [(w.kind, w.text, w.status, w.question, w.answer) for w in writes] == [
        ("fact_create", "Drinks coffee every morning", "ok", OFFER, "Yes, please."),
        ("todo_create", "Book the dentist", "ok", OFFER, "Yes, please.")]
    assert bee_commands(fake_bee) == [["facts", "create"], ["facts", "confirm"], ["todos", "create"]]
    (payload,) = sent
    assert payload["aps"]["alert"]["body"] == "Added 2 memories to Bee. You can see and edit them in the Bee app."
    (logged,) = MetaSession(tmp_path / "workspace").entries()  # the between-calls timeline shows it
    assert logged["kind"] == "organize" and logged["decision"].startswith("I added two things")
    assert [a["tool"] for a in logged["actions"]] == ["bee_add_fact", "bee_add_fact", "bee_add_todo", "notification"]


def test_the_notification_is_in_the_person_s_language_and_counts_what_went_in(tmp_path, fake_bee):
    sent = []
    organize(tmp_path, [{"role": "assistant", "content": [tool_use(1, "bee_add_fact", text="Drinks coffee")]}, done()],
             request={**REQUEST, "language": "ko"}, notified=sent)
    assert sent[0]["aps"]["alert"]["body"] == "Bee에 기억 1개를 넣었어요. Bee 앱에서 보고 고칠 수 있어요."
    nothing = []
    organize(tmp_path / "none", [done("Bee had it all.")], notified=nothing)
    assert nothing[0]["aps"]["alert"]["body"] == "Nothing new to add to Bee."


def test_bee_is_changed_only_in_an_organize_run_and_only_by_adding(tmp_path, fake_bee):
    ws = tmp_path / "workspace"
    model = ScriptedModel([{"role": "assistant", "content": [tool_use(1, "bee_add_fact", text="Drinks coffee")]},
                           done("Nothing to do.")])
    entry = asyncio.run(Thinker(ws, on_glm(model), bee=True).think([{"kind": "call_ended", "at": "x"}], seoul_now()))
    assert entry["tools"][0]["result"] == "There is no tool named bee_add_fact."
    assert bee_commands(fake_bee) == []
    offered = {t["toolSpec"]["name"] for t in model.sent[0]["toolConfig"]["tools"]}
    assert not offered & {"bee_add_fact", "bee_add_todo"}
    from meta.agent import ADD_TOOLS

    assert all("add" in t.name and "delete" not in t.description.casefold() for t in ADD_TOOLS)


def test_an_organize_request_runs_once_even_when_its_events_come_back(tmp_path, fake_bee):
    """The organize run worked but the think for the call's end failed: the same events come back (BC-061), and the
    run is not done again, so no second notification goes out."""
    from tests.test_meta import Streams

    ws = tmp_path / "workspace"
    sent = []

    async def notify(payload):
        sent.append(payload)
        return "notification sent"

    class FailsTheSecondThink(ScriptedModel):
        def converse(self, **request):
            if len(self.sent) >= 2:
                raise RuntimeError("Bedrock throttled")
            return super().converse(**request)

    model = FailsTheSecondThink([{"role": "assistant", "content": [tool_use(1, "bee_add_fact", text="Drinks coffee")]},
                                 done()])
    ended = {"at": "2026-10-18T15:00:00+09:00", "kind": "call_ended", "call": "2026-10-18/1450"}
    first = asyncio.run(Thinker(ws, on_glm(model), bee=True, notify=notify).think([REQUEST, ended], seoul_now()))
    assert "throttled" in first["error"] and len(sent) == 1
    again = ScriptedModel([done("The call went fine.")])
    second = asyncio.run(Thinker(ws, on_glm(again), bee=True, notify=notify).think([REQUEST, ended], seoul_now()))
    assert "error" not in second and len(sent) == 1 and len(again.sent) == 1  # only the call's end was thought about
    assert [w.text for w in BeeWrites(ws / "meta").all()] == ["Drinks coffee"]
    assert Streams  # the stand-in streams as Bedrock does


def test_without_the_model_nothing_is_organized(tmp_path, fake_bee, monkeypatch):
    from meta.agent import thinker_for

    monkeypatch.setenv("META_AGENT", "rules")
    ws = tmp_path / "workspace"
    thinker = thinker_for(ws, send=None, call_time=None, bee=True)
    asyncio.run(thinker.think([REQUEST], seoul_now()))
    (logged, _) = MetaSession(ws).entries()
    assert logged["kind"] == "organize" and logged["model"] == "rules" and bee_commands(fake_bee) == []


def test_what_an_organize_run_added_is_the_user_s_own_for_every_later_read(tmp_path, fake_bee):
    """A fact made right after a call would read as one Bee made of that call (BC-060) and be left out of every read;
    those an organize run added are the user's, as those a call wrote on their yes."""
    from datetime import datetime

    from beelink.calls import OurCalls
    from tests.test_bee_toolkit import a_call

    ws = tmp_path / "workspace"
    a_call(ws, "2026-10-18T14:50", "2026-10-18T14:58")
    writes = BeeWrites(ws / "meta")
    writes.dir.mkdir(parents=True)
    write = asyncio.run(writes.begin({"kind": "fact_create", "text": "Drinks coffee", "request": "r"}, OFFER, "Yes."))
    asyncio.run(writes.apply(write))
    made = BeeWrites(ws / "meta").all()[0].response["fact"]
    just_after = int(datetime(2026, 10, 18, 15, 1, tzinfo=SEOUL).timestamp() * 1000)
    fact = {**made, "created_at": just_after}
    assert OurCalls.of(ws).derived(fact) and OurCalls.of(ws).kept_facts([fact]) == [fact]
    assert json.dumps(fact)


def test_a_to_do_bee_suggests_in_the_same_words_is_not_added_again(tmp_path, fake_bee):
    _, entry = organize(tmp_path, [{"role": "assistant", "content": [
        tool_use(1, "bee_add_todo", text="Ask about bonus terms")]}, done("Nothing new.")])  # the fake's suggestion 34
    assert entry["tools"][0]["result"] == "Not added: Bee already has to-do 'Ask about bonus terms'."
    assert bee_commands(fake_bee) == []


# ---------- review of 2026-10-18 ----------


def test_one_yes_covers_this_call_and_the_past_days_once_each(tmp_path, fake_bee):
    """Finding 4: "Yes, and tidy up this week too." answers one offer that covers both."""
    async def main():
        agent, rec = await in_a_call(tmp_path, [("assistant", OFFER), ("user", "Yes, and tidy up this week too.")])
        first = await agent.organize_bee(None, facts=["Drinks coffee"])
        second = await agent.organize_bee(None, scope="recent", days=7)
        again = await agent.organize_bee(None, scope="recent", days=3)
        return first, second, again, agent

    first, second, again, agent = asyncio.run(main())
    assert first.startswith("Added to Bee") and second.startswith("Started")
    assert again.startswith("Already done for that answer") and agent.organize_request["days"] == 7


def test_an_organize_run_that_keeps_failing_is_given_up_after_three_tries_with_one_notification(tmp_path, fake_bee):
    """Finding 5: a failing organize run held the rest of the inbox back for good."""
    from tests.test_meta import Streams

    class Broken(Streams):
        def converse(self, **request):
            raise RuntimeError("Bedrock throttled")

    ws = tmp_path / "workspace"
    sent = []

    async def notify(payload):
        sent.append(payload)
        return "notification sent"

    for _ in range(3):
        entry = asyncio.run(Thinker(ws, on_glm(Broken()), bee=True, notify=notify).think([REQUEST], seoul_now()))
        assert "throttled" in entry["error"] and sent == []
    model = ScriptedModel([done("The call went fine.")])
    ended = {"at": "2026-10-18T15:00:00+09:00", "kind": "call_ended", "call": "2026-10-18/1450"}
    entry = asyncio.run(Thinker(ws, on_glm(model), bee=True, notify=notify).think([REQUEST, ended], seoul_now()))
    assert "error" not in entry and entry["decision"] == "The call went fine."  # the rest goes on
    (payload,) = sent
    assert payload["aps"]["alert"]["body"] == "Couldn't finish organizing Bee. Nothing was changed."


def test_an_organize_that_adds_nothing_leaves_the_answer_for_one_that_does(tmp_path, fake_bee):
    """Review probes of 2026-10-18: a scope it does not know, no items or an item too long add nothing and take
    nothing from the user's answer; after a scope's organize, the same answer organizes that scope no more."""
    async def main():
        agent, _ = await in_a_call(tmp_path)
        await say(agent, ("assistant", "Shall I put what came up today in Bee?"), ("user", "Yes."))
        return [await agent.organize_bee(None, scope="today", facts=["Plays piano"]),
                await agent.organize_bee(None, scope="call", facts=[]),
                await agent.organize_bee(None, scope="call", facts=["x" * 201]),
                await agent.organize_bee(None, scope="call", facts=["Plays piano"]),
                await agent.organize_bee(None, scope=" Call ", facts=["Allergic to cats"]),
                await agent.organize_bee(None, scope="recent", days=3)]

    said = asyncio.run(main())
    assert said[0].startswith('scope is "call"') and said[1].startswith("Nothing added")
    assert said[2].startswith("Not added: each item") and said[3].startswith("Added to Bee: fact 'Plays piano' (change "
                                                                             "w1).")
    assert said[4].startswith("Already done for that answer") and said[5].startswith("Started")
