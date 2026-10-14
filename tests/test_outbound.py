"""Tests for AI-initiated calls: the planner, placing the call, tokens and the voice server's call context.

No network: LiveKit is a fake object here, and the Bee change comes from a fixture.
"""

import asyncio
import base64
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from beelink.context import CallInputs
from beelink.planner import OUTBOUND_LOG, RulePlanner
from voice import agent
from voice.dispatch import AGENT_NAME, place_call
from voice.language import call_language
from voice.prep import Prep, call_brief
from voice.outbound import saved_trigger
from voice.token_server import make_token, recent_calls

FIXTURE = Path(__file__).parent / "fixtures" / "inbox" / "changed.json"


def inbox(tmp_path: Path, ended_minutes_ago: float = 5, **changes) -> Path:
    folder = tmp_path / "workspace" / "inbox" / "2026-10-11-184500"
    folder.mkdir(parents=True)
    data = json.loads(FIXTURE.read_text())
    now_ms = time.time() * 1000
    for c in data["conversations"]:  # the fixture's conversations ended a few minutes ago
        c["start_time"], c["end_time"] = now_ms - (ended_minutes_ago + 15) * 60000, now_ms - ended_minutes_ago * 60000
    data.update(changes)
    (folder / "changed.json").write_text(json.dumps(data))
    return folder


def test_planner_calls_about_a_finished_conversation_and_writes_the_plan(tmp_path):
    folder = inbox(tmp_path)
    plan = RulePlanner(tmp_path / "workspace").decide(saved_trigger(folder))
    assert plan.call and plan.conversation_ids == [5121]  # the 2-utterance hello is not worth a call
    assert plan.topic == "Planned a Saturday hike with Dana"
    text = (folder / "plan.md").read_text()
    assert "- Decision: call" in text and "rule-based stand-in" in text


def test_planner_stays_quiet_without_something_to_talk_about(tmp_path):
    folder = inbox(tmp_path, conversations=[])
    plan = RulePlanner(tmp_path / "workspace").decide(saved_trigger(folder))
    assert not plan.call and "- Decision: no call" in (folder / "plan.md").read_text()


def test_planner_does_not_call_about_last_nights_conversation(tmp_path):
    folder = inbox(tmp_path, ended_minutes_ago=9 * 60)  # caught up in the morning
    assert not RulePlanner(tmp_path / "workspace").decide(saved_trigger(folder)).call


def test_planner_calls_about_a_new_voice_note(tmp_path):
    folder = inbox(tmp_path, conversations=[], journals=[{"id": 9, "text": "Remind me to compare the two offers."}])
    plan = RulePlanner(tmp_path / "workspace").decide(saved_trigger(folder))
    assert plan.call and plan.kind == "voice note" and "compare the two offers" in plan.reason


def test_planner_keeps_calls_apart_and_never_interrupts_a_call(tmp_path):
    workspace = tmp_path / "workspace"
    folder = inbox(tmp_path)
    log = workspace / OUTBOUND_LOG
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"at": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")}) + "\n")
    assert not RulePlanner(workspace).decide(saved_trigger(folder)).call
    assert RulePlanner(workspace, cooldown=timedelta(0)).decide(saved_trigger(folder)).call
    live = workspace / "2026-10-11" / "1850"
    live.mkdir(parents=True)
    started = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")
    (live / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started", "started_at": started}) + "\n")
    os.utime(live / "events.jsonl", (time.time() - 600, time.time() - 600))  # a caller quiet for ten minutes
    plan = RulePlanner(workspace, cooldown=timedelta(0)).decide(saved_trigger(folder))
    assert not plan.call and plan.reason == "A call is already in progress."


def test_cooldown_counts_calls_the_user_started(tmp_path):
    workspace = tmp_path / "workspace"
    folder = inbox(tmp_path)
    done = workspace / "2026-10-11" / "1900"
    done.mkdir(parents=True)
    started = (datetime.now() - timedelta(minutes=12)).isoformat(timespec="seconds")
    lines = [{"t": 0.0, "type": "call_started", "started_at": started}, {"t": 300.0, "type": "call_ended"},
             {"t": 5000.0, "type": "bee_write_done"}]  # a Bee write long after the call does not extend it
    (done / "events.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    plan = RulePlanner(workspace).decide(saved_trigger(folder))
    assert not plan.call and "calls are at least 30 minutes apart" in plan.reason


def test_place_call_opens_a_room_with_the_voice_server_and_records_the_ring(tmp_path):
    requests = []

    async def create_room(request):
        requests.append(request)

    workspace = tmp_path / "workspace"
    plan = RulePlanner(workspace).decide(saved_trigger(inbox(tmp_path)))
    lk = SimpleNamespace(room=SimpleNamespace(create_room=create_room))
    prep = Prep(note="Matt planned a hike with Dana.", opening="Hi Matt, it's Bee Call. Got a minute about Saturday?")
    room = asyncio.run(place_call(plan, workspace, lk, prep=prep.to_meta()))
    (request,) = requests
    assert request.name == room and room.startswith("call-")
    assert [a.agent_name for a in request.agents] == [AGENT_NAME]
    meta = json.loads(request.agents[0].metadata)
    assert meta["direction"] == "outbound" and meta["topic"] == plan.topic and meta["conversation_ids"] == [5121]
    assert meta["kind"] == "conversation" and Prep.from_meta(meta["prep"]) == prep
    ring = json.loads((workspace / OUTBOUND_LOG).read_text().splitlines()[-1])
    assert ring["room"] == room and ring["topic"] == plan.topic
    assert (datetime.fromisoformat(ring["expires_at"]) - datetime.fromisoformat(ring["at"])).total_seconds() == 60
    assert ring["expires_epoch"] == int(datetime.fromisoformat(ring["expires_at"]).timestamp())


def claims(jwt: str) -> dict:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_tokens_dispatch_the_voice_server_only_for_calls_the_user_starts():
    start = claims(make_token("matt", "call-1", answer=False, key="APIkey", secret="s" * 32))
    assert start["roomConfig"]["agents"] == [{"agentName": AGENT_NAME, "metadata": '{"direction": "inbound"}'}]
    answer = claims(make_token("matt", "call-1", answer=True, key="APIkey", secret="s" * 32))
    assert "roomConfig" not in answer  # the AI's call already has the voice server; a second one would talk over it


def test_ring_stream_starts_with_calls_placed_in_the_last_minute(tmp_path):
    log = tmp_path / "outbound.jsonl"
    now = datetime.now()
    old = {"at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"), "room": "call-old"}
    new = {"at": (now - timedelta(seconds=5)).isoformat(timespec="seconds"), "room": "call-new"}
    log.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    lines, offset = recent_calls(log, now)
    assert [json.loads(l)["room"] for l in lines] == ["call-new"] and offset == log.stat().st_size


def test_voice_server_greets_and_briefs_by_who_called(monkeypatch):
    monkeypatch.setattr(agent, "COORDINATOR_LLM", "fake")
    english = call_language("en")
    out = agent.call_meta(json.dumps({"direction": "outbound", "topic": "Planned a Saturday hike with Dana",
                                      "reason": "A Bee conversation just ended."}))
    assert agent.greeting(out, english) == ("Hi, it's Bee Call. I'm calling about your conversation: "
                                            "Planned a Saturday hike with Dana. Do you have a minute?")
    assert agent.greeting(agent.call_meta('{"direction": "inbound"}'), english) == "Hi, it's Bee Call. What's on your mind?"
    note = agent.call_meta('{"direction": "outbound", "kind": "voice note"}')
    assert agent.greeting(note, english) == "Hi, it's Bee Call. I'm calling about your new voice note. Do you have a minute?"
    assert agent.call_meta("not json") == {}
    brief = call_brief(CallInputs(facts=[{"id": 7, "text": "Bonus is 15%"}]), out)
    assert brief == "You placed this call. Why: A Bee conversation just ended.\nUnconfirmed Bee fact (id 7): Bonus is 15%"


def test_answering_a_call_that_already_ended_is_refused(tmp_path):
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from tests.helpers import signed_in
    from voice.token_server import PERSON, make_app, token

    me = signed_in(tmp_path)

    async def main():
        async def gone(app, room):
            return False

        async def live(app, room):
            return True

        for exists, expected in ((gone, 410), (live, 200)):
            app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=exists)
            request = make_mocked_request("GET", "/token?room=call-matt-20261014-210000&answer=1", app=app)
            request[PERSON] = "matt"
            try:
                status = (await token(request)).status
            except web.HTTPException as exc:
                status = exc.status
            assert status == expected

    asyncio.run(main())

