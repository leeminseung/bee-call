"""Continuing an earlier call: what the new call learns about the earlier one, and how the id gets there
(the app's token request, the dispatch metadata, the prep's opening, the coordinator's instructions)."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import jwt

from beelink.context import CallInputs
from tests.contract import check_body
from voice.continuation import earlier_call, session_dir
from voice.coordinator import Coordinator
from voice.language import call_language
from voice.prep import seed_text
from voice.recorder import CallRecorder

NOW = datetime(2026, 10, 14, 1, 20, tzinfo=ZoneInfo("Asia/Seoul"))
EARLIER = "2026-10-13/2353"


def earlier_session(workspace: Path) -> Path:
    folder = workspace / EARLIER
    folder.mkdir(parents=True)
    events = [
        {"t": 0.0, "type": "call_started", "started_at": "2026-10-13T23:53:32", "room": "call-1"},
        {"t": 26.5, "type": "job_started", "job_id": "01", "task": "Compare 13-inch and 15-inch laptop prices in Korea"},
        {"t": 30.0, "type": "job_started", "job_id": "02", "task": "Find a laptop bag"},
        {"t": 31.0, "type": "job_started", "job_id": "03", "task": "Check the Apple store hours"},
        {"t": 146.1, "type": "job_done", "job_id": "01", "summary": "The 13-inch starts at 1.59 million won.",
         "files": ["jobs/01/report.md", "jobs/01/usage.json"]},
        {"t": 150.0, "type": "job_failed", "job_id": "03", "error": "web search timed out"},
        {"t": 199.7, "type": "session_closed", "reason": "CloseReason.PARTICIPANT_DISCONNECTED"},
        {"t": 200.4, "type": "job_failed", "job_id": "02", "error": "cancelled"},
        {"t": 200.4, "type": "call_ended"},
    ]
    (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    (folder / "transcript.md").write_text(
        "# Call 2026-10-13 23:53 (room call-1)\n\n"
        "- 00:04.1 **AI:** Hi Minseung! What's on your mind tonight?\n"
        "- 00:19.2 **User:** I'm choosing between a 13-inch and a 15-inch laptop.\n"
        "- 03:17.5 **AI:** Sure! Here's the comparison:\n\nThe 13-inch starts at about 1.6 million won. (interrupted)\n")
    return folder


def test_the_new_call_learns_what_was_said_and_what_the_jobs_did(tmp_path):
    earlier_session(tmp_path)
    text = earlier_call(tmp_path, EARLIER)
    assert text.startswith("The earlier call: Tue Oct 13, 11:53 PM, about 3 min.")
    assert "AI: Hi Minseung! What's on your mind tonight?" in text
    assert "User: I'm choosing between a 13-inch and a 15-inch laptop." in text
    assert "AI: Sure! Here's the comparison: The 13-inch starts at about 1.6 million won. (interrupted)" in text
    assert "Finished. Result: The 13-inch starts at 1.59 million won." in text
    assert "Files in the app: report.md\n" in text  # not usage.json
    assert "Find a laptop bag\n  Stopped unfinished when that call ended." in text
    assert "Failed: web search timed out" in text


def test_only_ids_of_earlier_calls_name_a_folder(tmp_path):
    earlier_session(tmp_path)
    assert session_dir(tmp_path, EARLIER) == tmp_path / EARLIER
    for bad in ("../../etc", "2026-10-13/2353/../2353", "2026-10-13", "2026-10-13/0000", None, 2353):
        assert session_dir(tmp_path, bad) is None
        assert earlier_call(tmp_path, bad) is None


def test_an_earlier_call_is_found_by_its_call_id_as_well(tmp_path):
    """Contract section 7: the D app continues a call by its call id (its LiveKit room); the pre-D form
    <date>/<time> is still taken. A later record in the same room is <room>.2."""
    earlier_session(tmp_path)
    again = tmp_path / "2026-10-13" / "2359"
    again.mkdir()
    (again / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started", "room": "call-1",
                                                    "started_at": "2026-10-13T23:59:00"}) + "\n")
    assert session_dir(tmp_path, "call-1") == tmp_path / EARLIER
    assert session_dir(tmp_path, "call-1.2") == again
    assert earlier_call(tmp_path, "call-1") == earlier_call(tmp_path, EARLIER)
    for bad in ("call-9", "call-1.3", "../call-1", ".", ""):
        assert session_dir(tmp_path, bad) is None


def test_the_app_asks_for_a_continued_call_with_the_record_id(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from tests.helpers import signed_in
    from voice.token_server import make_app

    me = signed_in(tmp_path)
    earlier_session(me.workspace)

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            good = await client.get("/token", params={"identity": "user", "continue": EARLIER})
            by_id = await client.get("/token", params={"continue": "call-1"})
            plain = await client.get("/token", params={"identity": "user"})
            missing = await client.get("/token", params={"identity": "user", "continue": "2026-10-13/0001"})
            sneaky = await client.get("/token", params={"identity": "user", "continue": "../../etc"})
            unknown = await client.get("/token", params={"continue": "call-9"})
            return (await good.json(), await by_id.json(), await plain.json(), missing.status, sneaky.status,
                    unknown.status)

    good, by_id, plain, missing, sneaky, unknown = asyncio.run(main())
    for body in (good, by_id, plain):
        check_body("token", body)
    meta = lambda body: json.loads(  # noqa: E731
        jwt.decode(body["token"], options={"verify_signature": False})["roomConfig"]["agents"][0]["metadata"])
    assert meta(good) == {"direction": "inbound", "continue_from": EARLIER, "person": "matt"}
    assert meta(by_id) == {"direction": "inbound", "continue_from": "call-1", "person": "matt"}
    assert meta(plain) == {"direction": "inbound", "person": "matt"}
    assert (missing, sneaky, unknown) == (400, 400, 400)


def test_the_opening_picks_up_the_earlier_call(tmp_path):
    earlier_session(tmp_path)
    meta = {"direction": "inbound", "continue_from": EARLIER, "earlier_call": earlier_call(tmp_path, EARLIER)}
    seed = seed_text(CallInputs(), meta, NOW, bee=True)
    assert "The user is calling you from the app to continue an earlier call with you." in seed
    assert "The earlier call this one continues:\nThe earlier call: Tue Oct 13" in seed
    assert seed.endswith("Then let the user lead. Keep the note to about 70 words, never more than 100. The call is in English: write the opening in English, whatever language the notes, the earlier calls or Bee are in.")
    assert "calling you from the app." in seed_text(CallInputs(), {"direction": "inbound"}, NOW, bee=True)


def test_the_coordinator_keeps_the_earlier_call_for_the_whole_call(tmp_path):
    earlier_session(tmp_path)
    text = earlier_call(tmp_path, EARLIER)
    agent = Coordinator(CallRecorder(tmp_path, room="r", now=NOW), SimpleNamespace(), tmp_path, clock=lambda: NOW,
                        earlier_call=text)
    assert "The user started this call to continue an earlier call with you." in agent.instructions
    assert text in agent.instructions
    plain = Coordinator(CallRecorder(tmp_path, room="r2", now=NOW), SimpleNamespace(), tmp_path, clock=lambda: NOW)
    assert "earlier call" not in plain.instructions


def test_a_continued_call_without_an_opening_still_says_so(monkeypatch):
    from voice import agent

    monkeypatch.setattr(agent, "COORDINATOR_LLM", "bedrock")
    for code, words in (("en", "pick up where we left off"), ("ko", "이어서")):
        assert words in agent.greeting({"direction": "inbound", "earlier_call": "..."}, call_language(code))
