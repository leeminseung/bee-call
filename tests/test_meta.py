"""The meta agent: its inbox and session, the read-only file tools, and one think with a scripted model.

No network: the model is a fake Converse client, and the notification sender is a fake.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from meta import files
from meta.agent import TOOLS, Thinker, context
from meta.session import MetaSession, add_event, quiet, quiet_ends
from voice.offers import OfferStore

NOW = datetime(2026, 10, 14, 18, 30)


def workspace_with_a_call(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    call = ws / "2026-10-14" / "1200"
    call.mkdir(parents=True)
    (call / "transcript.md").write_text("# Call\n\n- 00:04.1 **AI:** Hi!\n- 00:09.0 **User:** The bonus at Offer B is ten percent, not fifteen.\n")
    (call / "audio").mkdir()
    (call / "audio" / "user.wav").write_bytes(b"RIFF....")
    (ws / "state").mkdir()
    (ws / "state" / "voip_token.json").write_text('{"token": "secret"}')
    inbox = ws / "inbox" / "2026-10-14-182500"
    inbox.mkdir(parents=True)
    (inbox / "changed.json").write_text(json.dumps({"conversations": [
        {"id": 77, "state": "COMPLETED", "utterances_count": 14, "short_summary": "Weighing two job offers"}],
        "journals": []}))
    return ws


def test_events_are_taken_once_and_the_session_reads_back(tmp_path):
    ws = tmp_path / "workspace"
    session = MetaSession(ws)
    assert session.take_events() == []
    add_event(ws, "bee_change", now=NOW, inbox="inbox/x")
    add_event(ws, "call_ended", now=NOW, call="2026-10-14/1200")
    assert [e["kind"] for e in session.take_events()] == ["bee_change", "call_ended"]
    assert session.take_events() == []
    add_event(ws, "offer_answered", now=NOW, offer="o", choice="now")
    assert [e["kind"] for e in session.take_events()] == ["offer_answered"]
    session.log({"at": "2026-10-14T18:30:00", "events": [{"kind": "bee_change"}], "decision": "Nothing to do.",
                 "actions": []})
    assert "Decided: Nothing to do." in session.recent_text()
    assert "## Agenda for the next call" in session.state()
    session.write_state("# State\n\n## Agenda for the next call\n- the bonus\n")
    assert "- the bonus" in session.state()


def test_quiet_time_runs_from_eleven_to_eight():
    assert quiet(datetime(2026, 10, 14, 23, 0)) and quiet(datetime(2026, 10, 15, 7, 59))
    assert not quiet(datetime(2026, 10, 15, 8, 0)) and not quiet(NOW)
    assert quiet_ends(datetime(2026, 10, 14, 23, 30)) == datetime(2026, 10, 15, 8, 0)
    assert quiet_ends(datetime(2026, 10, 15, 2, 0)) == datetime(2026, 10, 15, 8, 0)


def test_the_file_tools_read_the_workspace_and_nothing_else(tmp_path):
    ws = workspace_with_a_call(tmp_path)
    listing = files.list_files(ws, "2026-10-14/1200")
    assert "transcript.md" in listing and "user.wav" not in files.list_files(ws, "2026-10-14/1200/audio")
    assert "2: " in files.read_file(ws, "2026-10-14/1200/transcript.md")
    assert "ten percent" in files.grep(ws, "bonus", ".", "transcript.md")
    assert "secret" not in files.grep(ws, "secret")  # tokens are never read
    for bad in ("../outside.txt", "/etc/passwd", "state/voip_token.json", "2026-10-14/1200/audio/user.wav"):
        with pytest.raises(files.FileError):
            files.read_file(ws, bad)


class ScriptedModel:
    """A Converse client that answers from a script and keeps what it was sent."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def converse(self, **request):
        self.sent.append(request)
        return {"output": {"message": self.replies.pop(0)}}


def tool_use(i, name, **args):
    return {"toolUse": {"toolUseId": f"t{i}", "name": name, "input": args}}


def test_one_think_looks_things_up_offers_a_call_and_logs_its_own_words(tmp_path):
    ws = workspace_with_a_call(tmp_path)
    model = ScriptedModel([
        {"role": "assistant", "content": [tool_use(1, "grep", pattern="bonus", glob="transcript.md")]},
        {"role": "assistant", "content": [
            tool_use(2, "offer_call", topic="Weighing two job offers", why="A decision is due this week.",
                     conversation_ids=[77], inbox="inbox/2026-10-14-182500"),
            tool_use(3, "write_state", text="# State\n\n## Agenda for the next call\n- Offer B's bonus is 10%\n")]},
        {"role": "assistant", "content": [{"text": "You talked through two job offers. I asked whether you want "
                                                   "a short call tonight to settle it."}]},
    ])
    sent = []

    async def send(offer, workspace, now):
        sent.append(offer.id)
        return "notification sent"

    add_event(ws, "bee_change", now=NOW, inbox="inbox/2026-10-14-182500", conversation_ids=[77])
    events = MetaSession(ws).take_events()
    entry = asyncio.run(Thinker(ws, ("zai.glm-5", "ap-southeast-2"), client=model, send=send).think(events, NOW))

    first = model.sent[0]
    assert first["modelId"] == "zai.glm-5" and first["toolConfig"]["tools"] == TOOLS
    assert "Weighing two job offers" in first["messages"][0]["content"][0]["text"]  # the change, without a lookup
    (offer,) = OfferStore(ws).all()
    assert (offer.topic, offer.conversation_ids, offer.state) == ("Weighing two job offers", [77], "asked")
    assert offer.inbox == str((ws / "inbox" / "2026-10-14-182500").resolve()) and sent == [offer.id]
    assert "Offer B's bonus is 10%" in MetaSession(ws).state()
    (logged,) = MetaSession(ws).entries()
    assert logged["decision"].startswith("You talked through two job offers")
    assert [t["name"] for t in logged["tools"]] == ["grep", "offer_call", "write_state"]
    assert "ten percent" in logged["tools"][0]["result"]
    assert [a["tool"] for a in logged["actions"]] == ["offer_call", "write_state"]
    assert entry["events"][0]["kind"] == "bee_change"


def test_doing_nothing_is_logged_too_and_a_failure_proposes_nothing(tmp_path):
    ws = workspace_with_a_call(tmp_path)
    quiet_model = ScriptedModel([{"role": "assistant", "content": [{"text": "A two-line hello. Nothing to do."}]}])
    entry = asyncio.run(Thinker(ws, ("m", "r"), client=quiet_model).think([{"kind": "bee_change", "at": "x"}], NOW))
    assert entry["decision"] == "A two-line hello. Nothing to do." and entry["actions"] == []

    class Broken:
        def converse(self, **request):
            raise RuntimeError("Bedrock throttled")

    failed = asyncio.run(Thinker(ws, ("m", "r"), client=Broken()).think([{"kind": "call_ended", "at": "x"}], NOW))
    assert "Bedrock throttled" in failed["error"] and OfferStore(ws).all() == []
    assert len(MetaSession(ws).entries()) == 2


def test_an_open_offer_about_the_same_thing_is_not_asked_again(tmp_path):
    ws = workspace_with_a_call(tmp_path)
    model = ScriptedModel([
        {"role": "assistant", "content": [tool_use(1, "offer_call", topic="Job offers", why="w")]},
        {"role": "assistant", "content": [tool_use(2, "offer_call", topic="Job offers", why="w")]},
        {"role": "assistant", "content": [{"text": "Asked once."}]},
    ])
    entry = asyncio.run(Thinker(ws, ("m", "r"), client=model).think([], NOW))
    assert len(OfferStore(ws).all()) == 1 and "already open" in entry["tools"][1]["result"]


def test_the_context_shows_limits_open_offers_notes_and_the_change(tmp_path):
    ws = workspace_with_a_call(tmp_path)
    text = context(ws, [{"kind": "bee_change", "at": "2026-10-14T18:25:00", "inbox": "inbox/2026-10-14-182500"}],
                   datetime(2026, 10, 14, 23, 30))
    assert "Notifications left today: 4 of 4. It is quiet time: a notification waits until 08:00." in text
    assert "Open offers:\nnone" in text and "## Agenda for the next call" in text
    assert "conversation 77 (14 utterances, COMPLETED): Weighing two job offers" in text


def test_the_calls_get_the_meta_agents_notes_and_answers_wake_it(tmp_path):
    from beelink.context import CallInputs
    from meta.session import meta_notes
    from voice.prep import call_brief, seed_text

    ws = tmp_path / "workspace"
    assert meta_notes(ws) is None  # nothing written yet
    MetaSession(ws).write_state("# State\n\n## Agenda for the next call\n- Ask about Offer B's bonus\n")
    notes = meta_notes(ws)
    seed = seed_text(CallInputs(), {"direction": "inbound", "meta_notes": notes}, NOW)
    assert "Notes from between calls (open threads, and the agenda for this call):\n# State" in seed
    assert "Ask about Offer B's bonus" in call_brief(CallInputs(), {"direction": "inbound", "meta_notes": notes})

    from aiohttp.test_utils import TestClient, TestServer
    from tests.test_offers import asked
    from voice.token_server import make_app

    offer, _ = asked(tmp_path, now=datetime.now())

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, tmp_path / "workspace", room_exists=None)
        async with TestClient(TestServer(app)) as client:
            return (await client.post(f"/offers/{offer.id}", json={"choice": "decline"})).status

    assert asyncio.run(main()) == 200
    (event,) = MetaSession(ws).take_events()
    assert (event["kind"], event["offer"], event["choice"]) == ("offer_answered", offer.id, "decline")
