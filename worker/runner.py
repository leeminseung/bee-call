"""Runs delegated jobs for a call and reports back to the voice server.

The boundary with any worker backend is one job folder:
  jobs/<NN>/request.md     what the coordinator asked for, its title and the call's language (written here)
  jobs/<NN>/events.jsonl   the job's own progress log (written here, append-only)
  jobs/<NN>/report.md      the backend's final report (written by the backend)
  jobs/<NN>/report.html    the report as a readable page for the app (written here when the job is done)
  jobs/<NN>/...            any files the backend made (charts, tables)

A backend only needs `async run(job, progress, relay) -> str` returning a
one-paragraph summary. `progress(text)` updates the job card; `relay(text)`
hands the coordinator a sentence to tell the user.

A backend can have another standing by (StandingBy: the model catalog's standby for the worker role). When the
first fails before it produced anything (nothing relayed, no file saved), the job runs again on the standby; the
call's record (job_standby), the job's own log and every notification after it say so, and what each attempt used
stays counted (usage.json moves to usage.1.json). A decline (JobDeclined) never moves: it is a decision, not an
outage, as for the call AI.

The coordinator hears about a job through notifications, the way Claude Code
tells its model about a background task. A relay arrives as a notification while
the job still runs: workers relay the answer first and write the report after,
which took 9-20 s more in the first calls (2026-10-13). The job's end arrives as
another notification with its files. The user always hears how a job ended: if a
backend finishes without relaying anything, the end notification carries its
summary, and if it fails, the error.
"""

import asyncio
import functools
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from voice.language import LANGUAGES
from worker.chart import chart_of_dark
from worker.report_page import PAGE, REPORT, write_page

EmitEvent = Callable[..., None]  # recorder.event(name, **fields)
log = logging.getLogger("worker.runner")


@dataclass
class Job:
    job_id: str
    dir: Path
    task: str
    now: datetime  # when it started, on the person's clock (Person.now()): the worker reads dates against it
    title: str | None = None  # a few words for the user's screen, in the call's language (delegate's title)
    language: str | None = None  # the call's language: the worker writes what the user reads in it
    status: str = "running"  # then completed, failed or cancelled
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    last_step: str | None = None
    files: list[str] = field(default_factory=list)  # names of the files the job made
    error: str | None = None
    note: str | None = None  # how it ran, when not as asked: the standby ran it instead


@dataclass
class JobNotification:
    """News about a job for the coordinator. A summary or an error is something to tell the user."""
    job_id: str
    status: str  # running, completed or failed
    task: str
    summary: str | None = None
    error: str | None = None
    files: list[str] = field(default_factory=list)
    note: str | None = None  # how it ran, when not as asked (Job.note)

    @property
    def needs_reply(self) -> bool:
        return bool(self.summary or self.error) or self.status == "failed"


NotifyHandler = Callable[[JobNotification], Awaitable[None]]


class Backend(Protocol):
    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str: ...


class JobDeclined(RuntimeError):
    """The worker's model declined the job: a decision, which the standby never takes over."""


@dataclass
class StandingBy:
    """A worker with another standing by for a job the first cannot run (the model catalog's worker standby)."""
    first: Backend
    standby: Callable[[], Backend]  # made only when needed
    first_name: str
    standby_name: str

    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str:
        return await self.first.run(job, progress, relay)  # JobRunner moves the job to the standby


OWN_FILES = ("request.md", "events.jsonl")  # what this runner writes into a job folder


def made_files(job: Job) -> list[str]:
    """The files a worker made in the job's folder: neither the runner's own nor its usage, nor a chart's dark PNG or
    the report's page, which are for the app (contract 2.4: the call view finds them beside the chart and the
    report)."""
    names = [p.name for p in job.dir.iterdir() if p.is_file() and p.name not in OWN_FILES
             and not USAGE.fullmatch(p.name) and not p.name.startswith(".")]
    return [name for name in names if chart_of_dark(name) not in names and not (name == PAGE and REPORT in names)]


USAGE = re.compile(r"usage(?:\.\d+)?\.json")  # a job's usage.json, and an earlier attempt's usage.1.json


def keep_attempt_usage(job: Job) -> None:
    """Keeps what an attempt used (usage.json) under the next free usage.<n>.json, for the budget to count."""
    usage = job.dir / "usage.json"
    if not usage.exists():
        return
    n = 1
    while (job.dir / f"usage.{n}.json").exists():
        n += 1
    usage.replace(job.dir / f"usage.{n}.json")


class NoWorker:
    """Runs no job, as no background worker is set up on this server: each job fails saying why, so the user
    hears it."""

    def __init__(self, why: str) -> None:
        self.why = why

    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str:
        raise RuntimeError(f"no background worker is set up on this server ({self.why})")


escape = functools.partial(html.escape, quote=False)


def short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + " (cut short)"


class JobRunner:
    def __init__(self, session_dir: Path, emit: EmitEvent, backend: Backend, on_notify: NotifyHandler, *,
                 clock: Callable[[], datetime]) -> None:
        """`clock`: the person's clock (Person.now), for the date each job is told."""
        self.session_dir = session_dir
        self.clock = clock
        self.emit = emit
        self.backend = backend
        self.on_notify = on_notify
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def _next_job(self, task: str, title: str | None, language: str | None) -> Job:
        jobs = self.session_dir / "jobs"
        jobs.mkdir(exist_ok=True)
        n = len([p for p in jobs.iterdir() if p.is_dir()]) + 1
        job = Job(job_id=f"{n:02d}", dir=jobs / f"{n:02d}", task=task, now=self.clock(), title=title,
                  language=language)
        job.dir.mkdir()
        heading = f"# Job {job.job_id}" + (f": {title}" if title else "")
        said_in = f"\nLanguage: {LANGUAGES[language].name}\n" if language in LANGUAGES else ""
        (job.dir / "request.md").write_text(f"{heading}\n\n{task}\n{said_in}")
        return job

    def _job_log(self, job: Job, name: str, **fields) -> None:
        with (job.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"at": time.time(), "type": name, **fields}, ensure_ascii=False) + "\n")

    def start(self, task: str, title: str | None = None, language: str | None = None) -> Job:
        """Starts a job and returns at once; the call keeps going while it runs. `title`: a few words for the user's
        screen; `language`: the call's (voice/language.py), for what the worker writes for the user."""
        job = self._next_job(task, title, language)
        self.jobs[job.job_id] = job
        self.emit("job_started", job_id=job.job_id, task=task, title=title, language=language)
        self._job_log(job, "started", task=task, title=title, language=language)

        def progress(text: str) -> None:
            job.last_step = text
            self.emit("job_progress", job_id=job.job_id, text=text)
            self._job_log(job, "progress", text=text)

        async def tell(news: JobNotification) -> bool:
            try:
                await self.on_notify(news)
                return True
            except Exception:
                log.exception("could not tell the coordinator about job %s", job.job_id)
                return False

        relayed = False

        async def relay(text: str) -> None:
            nonlocal relayed
            text = (text or "").strip()
            if not text:  # a broken tool call can carry no text; then the summary at the end is told instead
                log.warning("job %s relayed an empty message", job.job_id)
                return
            self.emit("relay", job_id=job.job_id, text=text)
            self._job_log(job, "relay", text=text)
            if await tell(JobNotification(job.job_id, "running", task, summary=text, note=job.note)):
                relayed = True

        async def run() -> None:
            try:
                summary = await self._run(job, progress, relay, lambda: relayed)
            except asyncio.CancelledError:
                job.status, job.ended_at = "cancelled", time.monotonic()
                self.emit("job_failed", job_id=job.job_id, error="cancelled")
                self._job_log(job, "cancelled")
                raise
            except Exception as exc:
                job.status, job.ended_at = "failed", time.monotonic()
                job.error = str(exc)[:300] or type(exc).__name__  # TimeoutError() has no message
                self.emit("job_failed", job_id=job.job_id, error=str(exc))
                self._job_log(job, "failed", error=str(exc))
                await tell(JobNotification(job.job_id, "failed", task, error=job.error, note=job.note))
                return
            job.files = sorted(made_files(job) + [n for n in ("usage.json",) if (job.dir / n).exists()])
            job.status, job.ended_at = "completed", time.monotonic()
            files = [str((job.dir / name).relative_to(self.session_dir)) for name in job.files]
            self.emit("job_done", job_id=job.job_id, summary=summary, files=files)
            self._job_log(job, "done", summary=summary, files=files)
            told = None  # relayed: the user heard the answer; the end says only that the job and its files are done
            if not relayed:  # the backend never said what to tell the user; its summary will do
                told = (summary or "").strip()[:600] or "The job finished without a summary."
                self.emit("relay", job_id=job.job_id, text=told)
                self._job_log(job, "relay", text=told)
            # The report as a readable page for the app (contract 2.4), whoever wrote it, before the call AI hears the
            # job is done. Rendered off the call's event loop once the job's end is all on record: a call ending now
            # cuts only the wait, never the result.
            try:
                await asyncio.to_thread(write_page, job.dir)
            except Exception:
                log.exception("job %s: its report was left without a page", job.job_id)
            await tell(JobNotification(job.job_id, "completed", task, summary=told, files=job.files, note=job.note))

        self.tasks[job.job_id] = asyncio.create_task(run(), name=f"job-{job.job_id}")
        return job

    async def _run(self, job: Job, progress: Callable[[str], None], relay: Callable[[str], Awaitable[None]],
                   relayed: Callable[[], bool]) -> str:
        """The job on its backend; when that has a standby and fails before it produced anything (not a decline, not
        cut off), the job again on the standby."""
        backend = self.backend
        if not isinstance(backend, StandingBy):
            return await backend.run(job, progress, relay)
        try:
            return await backend.first.run(job, progress, relay)
        except (asyncio.CancelledError, JobDeclined):
            raise
        except Exception as exc:
            if relayed() or made_files(job):
                raise
            why = (str(exc).strip() or type(exc).__name__)[:300]
            log.warning("job %s: %s could not run it (%s); %s runs it", job.job_id, backend.first_name, why,
                        backend.standby_name)
            self.emit("job_standby", job_id=job.job_id, instead_of=backend.first_name, worker=backend.standby_name,
                      reason=why)
            self._job_log(job, "standby", instead_of=backend.first_name, worker=backend.standby_name, reason=why)
            keep_attempt_usage(job)
            job.note = f"{backend.first_name} could not run this job ({why}); {backend.standby_name} ran it instead."
            return await backend.standby().run(job, progress, relay)

    def status_text(self) -> str:
        """One line per job of this call, for the coordinator's job_status tool."""
        if not self.jobs:
            return "No background jobs in this call."
        now = time.monotonic()
        lines = []
        for job in self.jobs.values():  # what the worker wrote is escaped: it comes from web pages too
            seconds = round((job.ended_at or now) - job.started_at)
            if job.status == "running":
                parts = [f"Job {job.job_id}", f"running for {seconds} s", f"task: {escape(short(job.task))}"]
                if job.last_step:
                    parts.append(f"latest step: {escape(job.last_step)}")
            else:
                parts = [f"Job {job.job_id}", f"{job.status} after {seconds} s", f"task: {escape(short(job.task))}"]
                if job.files:
                    parts.append(f"files: {escape(', '.join(job.files))}")
                if job.error:
                    parts.append(f"error: {escape(job.error)}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    async def cancel_all(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
