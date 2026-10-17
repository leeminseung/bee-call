"""When the user turns Use Bee off (the app's Bee disconnect; the user's decision of 2026-10-18): the meta agent is woken
to rewrite its notes without anything that came from Bee, and until it has, no call's prep gets them; after the
rewrite, a call's prep for that person holds nothing from Bee (BC-057). The rewrite itself is the model's work, told in
its instructions; whether the real model leaves Bee out is measured (server-progress.md)."""

import asyncio
import re
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from beelink.context import CallInputs
from meta.agent import Thinker, instructions
from meta.session import MetaSession, add_event, bee_turned_off, meta_notes
from tests.helpers import SEOUL, signed_in
from tests.test_beelink import fake_bee  # noqa: F401  (pytest fixture)
from tests.test_meta import ScriptedModel, on_glm, tool_use
from tests.test_people import app_for
from voice.prep import call_brief, seed_text

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=SEOUL)
BEE_NOTES = """# State

## Open threads
- Two job offers, the answer due Friday. From Bee conversation 501 and voice memo j-71.

## Agenda for the next call
- Check unconfirmed fact 312 "Offer B's bonus is 15%".

## What the user prefers
- Short, casual calls.

## Worth getting to know next
- Insight 601: late nights after work talk.
"""
CLEAN = """# State

## Open threads
- Two job offers, the answer due Friday (the user brought them up in the last call).

## What the user prefers
- Short, casual calls.
"""
FROM_BEE = re.compile(r"\bBee\b(?! Call)|501|j-71|312|601|15%")


def said(model, n: int = 0) -> tuple[str, str]:
    """The instructions and the first message of the model's `n`-th request."""
    request = model.sent[n]
    return request["system"][0]["text"], request["messages"][0]["content"][0]["text"]


def done(text: str = "Rewrote my notes.") -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def test_turning_bee_off_in_the_app_wakes_the_meta_agent_and_holds_its_notes_back(tmp_path, fake_bee):
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    MetaSession(me.workspace).write_state(BEE_NOTES)

    async def main():
        async with TestClient(TestServer(app_for(me.people)), headers=me.headers) as client:
            first = (await client.post("/bee/disconnect")).status
            again = (await client.post("/bee/disconnect")).status  # Bee is off already: nothing more to rewrite
            return first, again

    assert asyncio.run(main()) == (204, 204)
    (event,) = MetaSession(me.workspace).take_events()
    assert event["kind"] == "bee_disconnected"
    assert meta_notes(me.workspace, bee=False) is None  # no call's prep gets them until they are rewritten
    assert meta_notes(me.workspace, bee=True) == BEE_NOTES.strip()  # Bee back on: they are about the user's Bee


def test_the_meta_agent_rewrites_its_notes_without_bee_and_then_a_prep_gets_nothing_from_bee(tmp_path):
    ws = tmp_path / "workspace"
    session = MetaSession(ws)
    session.write_state(BEE_NOTES)
    session.log({"at": (NOW - timedelta(days=1)).isoformat(timespec="seconds"), "events": [{"kind": "bee_change"}],
                 "decision": "Bee recorded conversation 501 about the offers; I put fact 312 on the agenda.",
                 "actions": [], "tools": []})
    add_event(ws, "bee_change", now=NOW - timedelta(minutes=5), inbox="inbox/2026-10-18-195500",
              conversation_ids=[502])  # came before the switch, not looked at yet
    bee_turned_off(ws, NOW)
    model = ScriptedModel([{"role": "assistant", "content": [tool_use(1, "write_state", text=CLEAN)]}, done()])
    asyncio.run(Thinker(ws, on_glm(model), bee=False).think(session.take_events(), NOW + timedelta(minutes=1)))

    told, first = said(model)
    assert "## Bee was turned off" in told and "write_state" in told
    assert BEE_NOTES.strip() in first  # the notes to rewrite
    assert "conversation 501 about the offers" not in first  # its entries from before the switch stay out
    assert "inbox/2026-10-18-195500" not in first and "The user turned Bee off" in first
    assert session.state() == CLEAN
    notes = meta_notes(ws, bee=False)
    assert notes == CLEAN.strip()
    meta = {"direction": "inbound", "meta_notes": notes}
    for text in (seed_text(CallInputs(), meta, NOW + timedelta(hours=1), bee=False), call_brief(CallInputs(), meta)):
        assert "Two job offers" in text and not FROM_BEE.search(text), text

    # The next think is an ordinary one, told nothing about Bee.
    add_event(ws, "call_ended", now=NOW + timedelta(hours=2), call="2026-10-18/2100", direction="inbound")
    model = ScriptedModel([done("Nothing to do.")])
    asyncio.run(Thinker(ws, on_glm(model), bee=False).think(session.take_events(), NOW + timedelta(hours=2)))
    told, first = said(model)
    assert told == instructions(bee=False) and not FROM_BEE.search(told)
    # nor the entry of the think that rewrote them, whose words may name what it took out
    assert "conversation 501 about the offers" not in first and "Rewrote my notes." not in first


def test_notes_not_rewritten_stay_held_back_and_the_next_think_is_told_again(tmp_path):
    ws = tmp_path / "workspace"
    MetaSession(ws).write_state(BEE_NOTES)
    bee_turned_off(ws, NOW)
    model = ScriptedModel([done("Nothing changed.")])
    asyncio.run(Thinker(ws, on_glm(model), bee=False).think(MetaSession(ws).take_events(), NOW))
    assert meta_notes(ws, bee=False) is None
    add_event(ws, "call_ended", now=NOW + timedelta(hours=1), call="2026-10-18/2100", direction="inbound")
    model = ScriptedModel([done()])
    asyncio.run(Thinker(ws, on_glm(model), bee=False).think(MetaSession(ws).take_events(), NOW + timedelta(hours=1)))
    assert "## Bee was turned off" in said(model)[0]


def test_only_a_person_who_turned_bee_off_is_told_about_it(tmp_path):
    """BC-057: a person who never connected Bee is told nothing about Bee, and their notes are never held back."""
    ws = tmp_path / "workspace"
    MetaSession(ws).write_state(CLEAN)
    assert meta_notes(ws, bee=False) == CLEAN.strip()
    add_event(ws, "call_ended", now=NOW, call="2026-10-18/2100", direction="inbound")
    model = ScriptedModel([done()])
    asyncio.run(Thinker(ws, on_glm(model), bee=False).think(MetaSession(ws).take_events(), NOW))
    assert said(model)[0] == instructions(bee=False) and "Bee was turned off" not in instructions(bee=False)
    assert "## Bee was turned off" in instructions(bee=False, bee_turned_off=True)


def test_bee_connected_again_before_the_rewrite_reads_the_notes_as_they_are(tmp_path):
    """Bee back on before the meta agent rewrote its notes: what came from Bee is about the user's Bee again, so the
    notes reach the calls, and the stale event that Bee was turned off is not looked at."""
    ws = tmp_path / "workspace"
    MetaSession(ws).write_state(BEE_NOTES)
    bee_turned_off(ws, NOW)
    assert meta_notes(ws, bee=True) == BEE_NOTES.strip() and meta_notes(ws, bee=False) is None
    add_event(ws, "call_ended", now=NOW + timedelta(minutes=5), call="2026-10-18/2005", direction="inbound")
    model = ScriptedModel([done("Nothing to do.")])
    asyncio.run(Thinker(ws, on_glm(model), bee=True).think(MetaSession(ws).take_events(), NOW + timedelta(minutes=6)))
    told, first = said(model)
    assert "Bee was turned off" not in told and "The user turned Bee off" not in first


# ---------- the second review of 2026-10-18 ----------

def test_a_loop_started_after_bee_was_turned_off_rewrites_the_notes_once_without_waiting_for_an_event(tmp_path,
                                                                                                    monkeypatch):
    """Finding 2: the loop that ran while Bee was on (restarted up to 10 s later) took the bee_disconnected event and
    dropped it, so the notes stayed held back until something else happened. The new loop thinks once as it starts."""
    from voice import outbound

    me = signed_in(tmp_path)
    MetaSession(me.workspace).write_state(BEE_NOTES)
    bee_turned_off(me.workspace, me.person.now())
    MetaSession(me.workspace).take_events()  # the old loop took the event
    monkeypatch.setattr(outbound, "META_CHECK_S", 0.01)
    model = ScriptedModel([{"role": "assistant", "content": [tool_use(1, "write_state", text=CLEAN)]}, done(),
                           done("again")])

    async def main():
        loop = asyncio.create_task(outbound.serve_meta(me.person, Thinker(me.workspace, on_glm(model), bee=False)))
        await asyncio.sleep(0.3)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(main())
    assert len(model.sent) == 2  # one think (a tool round and its answer), and no more while nothing new comes
    assert "## Bee was turned off" in said(model)[0] and meta_notes(me.workspace, bee=False) == CLEAN.strip()


def test_after_the_switch_the_meta_agent_cannot_read_what_came_from_bee(tmp_path):
    """Finding 3: its file tools reached Bee's batches, its old session and the calls' prep notes, so what came from
    Bee could come back into the rewritten notes. For a person who turned Bee off they are hidden; transcripts stay."""
    import json

    ws = tmp_path / "workspace"
    (ws / "inbox" / "2026-10-18-100000").mkdir(parents=True)
    (ws / "inbox" / "2026-10-18-100000" / "changed.json").write_text(json.dumps({"facts": [{"id": 312, "text": "bonus"}]}))
    call = ws / "2026-10-18" / "0900"
    call.mkdir(parents=True)
    (call / "prep.md").write_text("Check fact 312: bonus is 15%\n")
    (call / "events.jsonl").write_text('{"type": "tool", "name": "bee_facts", "output": "bonus 15%"}\n')
    (call / "bee-writes.jsonl").write_text('{"text": "bonus 15%"}\n')
    (call / "transcript.md").write_text("- **User:** the bonus talk is boring\n")
    MetaSession(ws).log({"at": NOW.isoformat(), "events": [], "decision": "bonus fact 312", "tools": [], "actions": []})

    def found(bee: bool) -> str:
        thinker = Thinker(ws, on_glm(ScriptedModel([])), bee=bee)
        return asyncio.run(thinker._tool("grep", {"pattern": "bonus"}, NOW, []))

    assert all(where in found(bee=False) for where in ("inbox/", "prep.md", "events.jsonl", "transcript.md"))
    bee_turned_off(ws, NOW)
    after = found(bee=False)
    assert "transcript.md" in after and not any(where in after for where in (
        "inbox/", "prep.md", "events.jsonl", "bee-writes.jsonl", "session.jsonl")), after
    thinker = Thinker(ws, on_glm(ScriptedModel([])), bee=False)
    assert "inbox" not in asyncio.run(thinker._tool("list_files", {"path": "."}, NOW, []))
    assert "not a file that can be read" in asyncio.run(thinker._tool("read_file", {"path": "2026-10-18/0900/prep.md"},
                                                                       NOW, []))
    assert "prep.md" in found(bee=True)  # Bee on again: nothing more is hidden


def test_the_switch_is_marked_before_bee_is_off_so_a_retry_still_marks_it(tmp_path, fake_bee, monkeypatch):
    """Finding 4: Bee was turned off first, so a failure writing the marker left a retry that found Bee off and wrote
    nothing; the old notes then reached every call."""
    from meta import session as meta_session
    from voice import token_server

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    MetaSession(me.workspace).write_state(BEE_NOTES)
    real, failed = meta_session.bee_turned_off, []

    def once_broken(workspace, now):
        if not failed:
            failed.append(1)
            raise OSError("disk full")
        real(workspace, now)

    monkeypatch.setattr(token_server, "bee_turned_off", once_broken)

    async def main():
        async with TestClient(TestServer(app_for(me.people)), headers=me.headers) as client:
            return [(await client.post("/bee/disconnect")).status for _ in range(2)]

    first, second = asyncio.run(main())
    assert first == 500 and second == 204
    assert meta_notes(me.workspace, bee=False) is None and me.people.settings("matt").bee is False


def test_turning_bee_off_from_the_admin_command_marks_it_too(tmp_path, monkeypatch):
    from voice import people as people_module

    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"bee": True}, allowed=("bee",))
    MetaSession(me.workspace).write_state(BEE_NOTES)
    monkeypatch.setenv("BEE_CALL_DATA", str(tmp_path / "data"))
    monkeypatch.setattr("sys.argv", ["voice.people", "set", "matt", "bee", "false"])
    people_module.main()
    assert me.people.settings("matt").bee is False and meta_notes(me.workspace, bee=False) is None
    (event,) = MetaSession(me.workspace).take_events()
    assert event["kind"] == "bee_disconnected"
