"""A background job outlives its call (2026-10-16): the friend's research was cancelled when the AI took "조사 끝나는
대로 다시 알려줘" for a goodbye, and a result relayed in the call lost its report and chart when the person hung up. A job
still running when the call ends now runs to its end in the call's process, at most AFTER_CALL_JOBS_S more; its end
lands in the call's record after call_ended, and the person gets one plain notification. A job cut off by a restart
is marked failed when the voice server starts again, and the call view never shows a dead job as running."""

import asyncio
import json
import os
import time
from pathlib import Path

from tests.fake_call import FakeCall, use_fakes
from tests.helpers import signed_in
from tests.test_voice_server import call_folders, events_of, quick  # noqa: F401 (quick is a fixture)
from voice import agent
from worker.fake import FakeWorker


def in_a_call(monkeypatch, me, step_s: float, *, title: str = "Price check", hang_up_after: float = 0.0):
    """A call that starts one job (the fake worker, `step_s` a step) and ends `hang_up_after` seconds later (while it
    runs, unless it took less); returns the pushes sent, the replies asked of the call AI after it ended, and the
    call."""
    monkeypatch.setattr(agent, "WORKER", "fake")
    monkeypatch.setattr(agent, "FakeWorker", lambda: FakeWorker(step_seconds=step_s))
    pushes: list = []

    async def alert(payload, person, **options):
        pushes.append((payload, options))
        return "A-1"

    monkeypatch.setattr(agent.voip_push, "alert", alert)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)
    asked_after: list = []

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        coordinator = call.session.started["agent"]
        await coordinator.delegate(None, task="Compare MacBook prices in Korea", title=title)
        await asyncio.sleep(hang_up_after)
        call.session.generate_reply = lambda **kwargs: asked_after.append(kwargs)  # from the call's end on
        call.hang_up()
        await call.end()

    asyncio.run(main())
    return pushes, asked_after, call


def test_a_job_still_running_when_the_call_ends_runs_to_its_end_after_it(tmp_path, monkeypatch, quick):
    me = signed_in(tmp_path)
    pushes, asked_after, _ = in_a_call(monkeypatch, me, step_s=0.3)
    (folder,) = call_folders(me.workspace)
    kinds = [e["type"] for e in events_of(folder)]
    assert "job_failed" not in kinds and kinds.index("call_ended") < kinds.index("job_done")
    assert (folder / "jobs" / "01" / "report.md").exists() and (folder / "jobs" / "01" / "report.html").exists()
    assert asked_after == []  # the call AI is not asked to speak once its call is over
    ((payload, options),) = pushes  # one notification, at the job's end
    assert payload["aps"]["alert"] == {"title": "Bee Call", "body": "Results are in: Price check"}
    assert (payload["kind"], payload["call"]) == ("job", "call-matt-20261015-090000")  # the call's id
    assert options["collapse_id"].endswith("-01") and len(options["collapse_id"]) <= 64


def test_a_job_that_ends_after_a_korean_call_is_told_in_korean(tmp_path, monkeypatch, quick):
    me = signed_in(tmp_path)
    me.people.change_settings("matt", {"language": "ko"})
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.2, title="맥북 가격 비교")
    ((payload, _),) = pushes
    assert payload["aps"]["alert"]["body"] == "결과가 나왔어요: 맥북 가격 비교"


def test_a_job_still_running_long_after_its_call_is_stopped_saying_so(tmp_path, monkeypatch, quick):
    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "AFTER_CALL_JOBS_S", 0.3)
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=2.0)
    (folder,) = call_folders(me.workspace)
    (failed,) = [e for e in events_of(folder) if e["type"] == "job_failed"]
    assert "after the call" in failed["error"] and failed["error"] != "cancelled"
    ((payload, _),) = pushes
    assert payload["aps"]["alert"]["body"] == "Couldn't finish: Price check"


def test_a_job_running_in_a_call_shows_life_in_its_log(tmp_path, monkeypatch):
    """The call view tells a working job from a dead one by its log's time: a running job touches it as it works."""
    from worker import runner

    monkeypatch.setattr(runner, "HEARTBEAT_S", 0.05)

    class Slow:
        async def run(self, job, progress, relay):
            await asyncio.sleep(0.4)
            return "done"

    async def main():
        jobs = runner.JobRunner(tmp_path, lambda *a, **k: None, Slow(), lambda news: asyncio.sleep(0),
                                clock=lambda: __import__("datetime").datetime.now())
        job = jobs.start("wait a little")
        log = job.dir / "events.jsonl"
        os.utime(log, (1_000, 1_000))
        await asyncio.sleep(0.2)
        touched = log.stat().st_mtime
        await jobs.tasks[job.job_id]
        return touched

    assert asyncio.run(main()) > time.time() - 5


def test_a_job_cut_off_by_a_restart_is_failed_when_the_voice_server_starts(tmp_path, monkeypatch):
    """A deploy restarts the voice server; a job still running is killed with it (after up to 10 minutes). When the
    voice server starts again, each job with no end whose log went quiet is failed on record, saying why."""
    from voice import call_view
    from worker.runner import JOB_STALE_S

    me = signed_in(tmp_path)
    folder = me.workspace / "2026-10-16" / "1324"
    folder.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "room": "call-matt-1", "started_at": "2026-10-16T13:24:00+09:00"},
             {"t": 5.0, "type": "job_started", "job_id": "01", "task": "Moderna", "title": "모더나 조사"},
             {"t": 6.0, "type": "job_started", "job_id": "02", "task": "Still going", "title": "Fresh"},
             {"t": 7.0, "type": "job_started", "job_id": "03", "task": "Done", "title": "Done"},
             {"t": 9.0, "type": "job_done", "job_id": "03", "summary": "Fine.", "files": []},
             {"t": 60.0, "type": "session_closed", "reason": "CloseReason.USER_INITIATED"},
             {"t": 61.0, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
    stale, fresh = time.time() - JOB_STALE_S - 60, time.time()
    for job_id, touched, end in (("01", stale, None), ("02", fresh, None), ("03", stale, "done")):
        job = folder / "jobs" / job_id
        job.mkdir(parents=True)
        entries = [{"at": 1, "type": "started"}] + ([{"at": 2, "type": end}] if end else [])
        (job / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
        os.utime(job / "events.jsonl", (touched, touched))
    started = []
    monkeypatch.setattr(agent.cli, "run_app", lambda server: started.append(server))
    monkeypatch.setenv("BEE_CALL_DATA", str(tmp_path / "data"))
    agent.main()
    assert started == [agent.server]
    failed = [e for e in events_of(folder) if e["type"] == "job_failed"]
    assert [e["job_id"] for e in failed] == ["01"] and "restarted" in failed[0]["error"]
    assert json.loads((folder / "jobs" / "01" / "events.jsonl").read_text().splitlines()[-1])["type"] == "failed"
    from tests.helpers import SEOUL
    view = call_view.call_view(me.workspace, "call-matt-1", zone=SEOUL, language="ko", now=me.person.now())
    states = {i["title"]: (i["state"], i["failure"]) for i in view["items"] if i["type"] == "job"}
    assert states == {"모더나 조사": ("failed", "error"), "Fresh": ("running", None), "Done": ("done", None)}


def test_livekit_gives_the_call_s_process_the_time_its_jobs_may_take(monkeypatch):
    """LiveKit ends a job's process shutdown_process_timeout after it is asked to close (10 s by default): the call's
    process keeps its jobs running up to AFTER_CALL_JOBS_S after the call, so LiveKit is told to wait longer.
    (AgentServer has no public accessor for it.)"""
    assert agent.server._shutdown_process_timeout >= agent.AFTER_CALL_JOBS_S + 30


def test_a_continued_call_hears_that_a_job_of_the_earlier_one_is_still_running(tmp_path):
    from voice.continuation import earlier_call

    folder = tmp_path / "2026-10-16" / "1324"
    folder.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "room": "call-matt-1", "started_at": "2026-10-16T13:24:00+09:00"},
             {"t": 5.0, "type": "job_started", "job_id": "01", "task": "Research Moderna", "title": "Moderna"},
             {"t": 61.0, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    (folder / "transcript.md").write_text("# Call\n\n- 00:05.0 **User:** 조사 끝나는 대로 다시 알려줘.\n")
    (folder / "jobs" / "01").mkdir(parents=True)
    (folder / "jobs" / "01" / "events.jsonl").write_text('{"at": 1, "type": "started"}\n')
    text = earlier_call(tmp_path, "2026-10-16/1324")
    assert "Still running" in text and "Stopped unfinished" not in text
    old = time.time() - 3600
    os.utime(folder / "jobs" / "01" / "events.jsonl", (old, old))
    assert "Stopped unfinished" in earlier_call(tmp_path, "2026-10-16/1324")


def test_a_job_started_in_a_call_in_text_finishes_after_the_person_hangs_up(tmp_path, monkeypatch):
    """Seam 3 end to end: the person asks for a job and hangs up at once; the call in text returns once the job is
    done, its end after call_ended, and the notification is on the push record (not sent: no phone here)."""
    from voice import text_call

    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "COORDINATOR_LLM", "echo")
    monkeypatch.setattr(agent, "WORKER", "fake")
    monkeypatch.setattr(agent, "FakeWorker", lambda: FakeWorker(step_seconds=0.3))
    found = asyncio.run(text_call.text_call(me.people, "matt", ["test job"], say=lambda line: None))
    kinds = [e["type"] for e in events_of(Path(found["record"]))]
    assert kinds.index("call_ended") < kinds.index("job_done")
    pushes = [json.loads(l) for l in (me.workspace / "state" / "pushes.jsonl").read_text().splitlines()]
    (result,) = [p for p in pushes if p["type"] == "alert"]
    assert result["payload"]["aps"]["alert"]["body"].startswith("Results are in: ")


def test_a_result_that_came_in_the_call_s_last_moments_is_notified_too(tmp_path, monkeypatch, quick):
    """In a call in text, a search ended while the AI said goodbye: its news came 0.4 s before end_call, and the AI
    read it out after "Bye, take care!". The person may not hear a result told in the call's last seconds, so a job
    that ended within RECENT_RESULT_S of the call's end is notified as well; one that ended earlier is not."""
    me = signed_in(tmp_path)
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.01, hang_up_after=0.3)  # done before the call ends
    ((payload, _),) = pushes
    assert payload["aps"]["alert"]["body"] == "Results are in: Price check"
    monkeypatch.setattr(agent, "RECENT_RESULT_S", 0.1)
    me = signed_in(tmp_path / "earlier")
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.01, hang_up_after=0.5)
    assert pushes == []


def test_once_the_ai_said_goodbye_it_says_nothing_more(tmp_path):
    """end_call drains the goodbye; a reply queued by job news before it (the race above) would still be spoken after
    the goodbye. Once end_call ran, the call AI's model is not asked again and nothing more is said."""
    import pytest
    from types import SimpleNamespace

    from livekit.agents import AgentSession
    from livekit.agents.llm import StopResponse

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        model = ScriptedLLM([["Bye, take care!"], ["The search found three shows."]])
        async with AgentSession(llm=model) as session:
            await session.start(coordinator)
            await session.run(user_input="Alright, bye.")
            assert not coordinator.ending
            with pytest.raises(StopResponse):
                await coordinator.end_call(None)
            assert coordinator.ending
            await session.generate_reply()  # job news queued a reply
            said = [i.text_content for i in session.history.items if getattr(i, "role", None) == "assistant"]
        rec.close()
        return said, len(model.requests)

    said, asked = asyncio.run(main())
    assert said == ["Bye, take care!"] and asked == 1
