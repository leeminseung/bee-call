"""A background job outlives its call (2026-10-16): the friend's research was cancelled when the AI took "조사 끝나는
대로 다시 알려줘" for a goodbye, and a result relayed in the call lost its report and chart when the person hung up. A job
still running when the call ends now runs to its end in the call's process, at most AFTER_CALL_JOBS_S more; its end
lands in the call's record after call_ended, and the person gets one plain notification. A job cut off by a restart
is marked failed when the voice server starts again, and the call view never shows a dead job as running."""

import asyncio
import json
import os
import time
from datetime import datetime
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
    monkeypatch.setattr(agent, "keep_sweeping", lambda root: None)
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


def test_a_reply_queued_before_the_goodbye_is_not_said_after_it(tmp_path):
    """The same race the other way round, in calls in text with Claude Sonnet 5.5 (2026-10-16): a search ended while
    the AI was answering "Alright, bye.", so its news queued a reply before end_call ran. LiveKit asks for a queued
    reply at once, and the Claude adapter sends one request at a time (voice/claude_llm.py), so the reply's words came
    after end_call and were said after the goodbye: "The call has ended, but your exhibition search is in". A reply
    whose first words come once end_call ran is never said; the result reaches the person's phone instead (above)."""
    import pytest
    from types import SimpleNamespace

    from livekit.agents import AgentSession
    from livekit.agents.llm import StopResponse
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

    from tests.helpers import ScriptedLLM, _ScriptedStream, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    class Held(_ScriptedStream):
        wait: asyncio.Event | None = None

        async def _run(self) -> None:
            if self.wait is not None:
                await self.wait.wait()
            await super()._run()

    class OneAtATime(ScriptedLLM):
        """Answers its second request only once `release` is set, as if it waited for the first."""

        def __init__(self, replies):
            super().__init__(replies)
            self.release = asyncio.Event()

        def chat(self, *, chat_ctx, tools=None, **kwargs):
            self.requests.append(chat_ctx.copy())
            stream = Held(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=DEFAULT_API_CONNECT_OPTIONS)
            stream.wait = self.release if len(self.requests) == 2 else None
            return stream

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        model = OneAtATime([["Bye, take care!"], ["The call has ended, but your search is in."]])
        async with AgentSession(llm=model) as session:
            await session.start(coordinator)
            await session.run(user_input="Alright, bye.")
            news = session.generate_reply()  # the search's news, before end_call ran
            while len(model.requests) < 2:  # the reply was asked for and waits for its turn
                await asyncio.sleep(0.01)
            with pytest.raises(StopResponse):
                await coordinator.end_call(None)
            model.release.set()
            await news
            said = [i.text_content for i in session.history.items if getattr(i, "role", None) == "assistant"]
        rec.close()
        return said, [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]

    said, log = asyncio.run(main())
    assert said == ["Bye, take care!"]
    assert [entry["ended"] for entry in log] == ["done", "after end_call"]  # the model log keeps what was not said


# ---------- second review of 2026-10-16: one notification a job, a stop that does not wait, the sweep ----------

def test_a_job_that_ends_while_the_call_is_closing_is_notified_once(tmp_path, monkeypatch, quick):
    """Finding 3: a job's end that came after the call ended but before finish_jobs (here while the title was
    written) was notified by both, two "Results are in" for one job."""
    me = signed_in(tmp_path)
    real_title = agent.title_call

    async def slow_title(*args, **kwargs):  # the real one may take up to 5 s (voice/titles.py)
        await asyncio.sleep(1.0)
        return await real_title(*args, **kwargs)

    monkeypatch.setattr(agent, "title_call", slow_title)
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.3, hang_up_after=0.4)
    assert [p["aps"]["alert"]["body"] for p, _ in pushes] == ["Results are in: Price check"]


def test_a_job_still_making_its_page_when_the_call_ends_is_notified_once(tmp_path, monkeypatch, quick):
    """Finding 3 too: the runner marks a job completed before it renders the page and tells the call, so finish_jobs
    and the later news both notified it."""
    import worker.runner as runner

    me = signed_in(tmp_path)
    real_render = runner.render_apart

    def slow_render(folder):
        time.sleep(1.0)
        return real_render(folder)

    monkeypatch.setattr(runner, "render_apart", slow_render)
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.15, hang_up_after=0.4)
    assert [p["aps"]["alert"]["body"] for p, _ in pushes] == ["Results are in: Price check"]


def test_a_job_done_but_still_making_its_page_at_the_cap_is_told_as_done(tmp_path, monkeypatch, quick):
    """Finding 3, the cap: a job whose work was done while its page was still being made counted as late and was
    notified as "Couldn't finish", while its card said done."""
    import worker.runner as runner

    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "AFTER_CALL_JOBS_S", 0.8)
    real_render = runner.render_apart

    def slow_render(folder):
        time.sleep(2.0)
        return real_render(folder)

    monkeypatch.setattr(runner, "render_apart", slow_render)
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=0.15)
    assert [p["aps"]["alert"]["body"] for p, _ in pushes] == ["Results are in: Price check"]
    (folder,) = call_folders(me.workspace)
    assert [e["type"] for e in events_of(folder) if e["type"] in ("job_started", "job_done", "job_failed")] == \
        ["job_started", "job_done"]


def test_when_the_voice_server_is_stopping_its_jobs_after_calls_stop_at_once(tmp_path, monkeypatch, quick):
    """Finding 4: a deploy (systemd stops the voice server) waited for every job still running after its call, up to
    the unit's 10 minutes, and took no calls meanwhile. The server marks its data root as it starts to stop; a job
    left running after its call then fails at once, saying so, and the person is told it could not finish."""
    from worker.runner import RESTARTED

    me = signed_in(tmp_path)
    (me.people.root / agent.STOPPING).write_text("now")
    started = time.monotonic()
    pushes, _, _ = in_a_call(monkeypatch, me, step_s=5.0)
    assert time.monotonic() - started < 4.0  # not the job's 10 s
    (folder,) = call_folders(me.workspace)
    (failed,) = [e for e in events_of(folder) if e["type"] == "job_failed"]
    assert failed["error"] == RESTARTED
    assert [p["aps"]["alert"]["body"] for p, _ in pushes] == ["Couldn't finish: Price check"]


def test_the_voice_server_marks_its_data_root_as_it_starts_to_stop(tmp_path, monkeypatch):
    drained = []

    async def drain(self, timeout=None):
        drained.append(self)

    monkeypatch.setattr(agent.AgentServer, "drain", drain)
    monkeypatch.setenv("BEE_CALL_DATA", str(tmp_path / "data"))
    asyncio.run(agent.server.drain())
    assert drained == [agent.server] and (tmp_path / "data" / agent.STOPPING).exists()


def started_voice_server(tmp_path, monkeypatch) -> list:
    """agent.main with LiveKit's CLI and the sweep's thread stood in for: the sweeps it started."""
    sweeps = []
    monkeypatch.setattr(agent.cli, "run_app", lambda server: None)
    monkeypatch.setattr(agent, "keep_sweeping", lambda root: sweeps.append(root))
    monkeypatch.setenv("BEE_CALL_DATA", str(tmp_path / "data"))
    agent.main()
    return sweeps


def test_a_new_voice_server_clears_the_stop_mark_and_keeps_sweeping(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / agent.STOPPING).write_text("then")
    assert started_voice_server(tmp_path, monkeypatch) == [tmp_path / "data"]
    assert not (tmp_path / "data" / agent.STOPPING).exists()


def killed_job(folder: Path, *, quiet_s: float, call_ended: bool = True) -> Path:
    """A call record with one job that has no end, whose log went quiet `quiet_s` ago; returns the job's log."""
    folder.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "room": "call-matt-1", "started_at": "2026-10-16T13:24:00+09:00"},
             {"t": 5.0, "type": "job_started", "job_id": "01", "task": "Moderna", "title": "모더나 조사"}]
    if call_ended:
        lines += [{"t": 60.0, "type": "session_closed", "reason": "CloseReason.USER_INITIATED"},
                  {"t": 61.0, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
    log = folder / "jobs" / "01" / "events.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"at": 1, "type": "started"}\n')
    quiet = time.time() - quiet_s
    os.utime(log, (quiet, quiet))
    return log


def test_a_job_killed_just_before_a_restart_is_failed_by_a_later_sweep_at_the_time_it_stopped(tmp_path):
    """Finding 2: a job killed seconds before the voice server started again still had a fresh log, so the sweep at
    the start left it, and the next start, days later, failed it timed then. Sweeps keep coming (keep_sweeping),
    and a job is failed at the time its log went quiet; its log keeps that time."""
    from worker.runner import JOB_STALE_S, fail_jobs_left_running

    me = signed_in(tmp_path)
    folder = me.workspace / "2026-10-16" / "1324"
    log = killed_job(folder, quiet_s=5)
    assert fail_jobs_left_running(tmp_path / "data") == []  # at the start: it may still be working
    quiet = log.stat().st_mtime
    later = time.time() + JOB_STALE_S + 60
    assert fail_jobs_left_running(tmp_path / "data", now=later) == [folder / "jobs" / "01"]
    (failed,) = [e for e in events_of(folder) if e["type"] == "job_failed"]
    started = datetime.fromisoformat("2026-10-16T13:24:00+09:00").timestamp()
    assert abs(failed["t"] - (quiet - started)) < 1.0
    assert log.stat().st_mtime == quiet and json.loads(log.read_text().splitlines()[-1])["at"] == quiet
    assert fail_jobs_left_running(tmp_path / "data", now=later + 600) == []  # once


def test_a_call_cut_off_mid_way_gets_no_line_from_the_sweep(tmp_path):
    """Finding 2 too: a line appended to a record with no call_ended made the call read as live again for two
    minutes, and announced it in /changes. Its job is failed in its own log only (the call view shows it failed
    from its quiet log)."""
    from worker.runner import fail_jobs_left_running

    me = signed_in(tmp_path)
    folder = me.workspace / "2026-10-16" / "1324"
    log = killed_job(folder, quiet_s=4 * 86400, call_ended=False)
    record = (folder / "events.jsonl").read_text()
    touched = (folder / "events.jsonl").stat().st_mtime
    assert fail_jobs_left_running(tmp_path / "data") == [folder / "jobs" / "01"]
    assert (folder / "events.jsonl").read_text() == record and (folder / "events.jsonl").stat().st_mtime == touched
    assert json.loads(log.read_text().splitlines()[-1])["type"] == "failed"


def test_the_sweep_runs_again_and_again_in_a_thread_of_its_own(tmp_path, monkeypatch):
    from worker import runner

    swept = []
    monkeypatch.setattr(runner, "fail_jobs_left_running", lambda root: swept.append(root) or [])
    stop = runner.keep_sweeping(tmp_path, every=0.05)
    time.sleep(0.3)
    stop.set()
    assert len(swept) >= 3 and set(swept) == {tmp_path}


def test_no_job_starts_once_the_day_s_budget_is_used_up(tmp_path, monkeypatch, quick):
    """Finding 7: the daily budget was checked only as a call started; a job now runs on after its call, so delegate
    checks it too."""
    from voice.costs import SpendLog

    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "WORKER", "fake")
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)
    said = []

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        coordinator = call.session.started["agent"]
        SpendLog(me.workspace).add("a test", model="claude-sonnet-5-5", input_tokens=10**9)
        said.append(await coordinator.delegate(None, task="Compare MacBook prices in Korea", title="Prices"))
        call.hang_up()
        await call.end()

    asyncio.run(main())
    assert said[0].startswith("Not started: today's budget") and "Tell the user" in said[0]
    (folder,) = call_folders(me.workspace)
    assert not [e for e in events_of(folder) if e["type"] == "job_started"]


def test_a_job_that_ends_after_its_call_tells_the_meta_agent(tmp_path, monkeypatch, quick):
    """Finding 8: the meta agent looks at a call as it ends, before its jobs did; it puts a job's result on the next
    call's agenda (meta/agent.py), so it hears when a job ends after the call."""
    from meta.agent import _event_line
    from meta.session import MetaSession

    me = signed_in(tmp_path)
    in_a_call(monkeypatch, me, step_s=0.3)
    (folder,) = call_folders(me.workspace)
    events = MetaSession(me.workspace).take_events()
    assert [e["kind"] for e in events] == ["call_ended", "job_ended"]
    ended = events[-1]
    call = f"{folder.parent.name}/{folder.name}"
    assert (ended["call"], ended["job"], ended["title"], ended["outcome"]) == (call, "01", "Price check", "done")
    assert _event_line(me.workspace, ended) == (
        f"[{ended['at']}] A background job of call {call} ended after the call: \"Price check\" (done); "
        f"its result is in {call}/jobs/01/")
