"""Runs delegated jobs for a call and reports back to the voice server.

The boundary with any worker backend is one job folder:
  jobs/<NN>/request.md     what the coordinator asked for, its title and the call's language (written here)
  jobs/<NN>/events.jsonl   the job's own progress log (written here, append-only)
  jobs/<NN>/report.md      the backend's final report (written by the backend)
  jobs/<NN>/...            any files the backend made (charts, tables)

A backend only needs `async run(job, progress, relay) -> str` returning a
one-paragraph summary. `progress(text)` updates the job card; `relay(text)`
hands the coordinator a sentence to tell the user.

The coordinator hears about a job through notifications, the way Claude Code
tells its model about a background task. A relay arrives as a notification while
the job still runs: workers relay the answer first and write the report after,
which took 9-20 s more in the first calls (2026-10-13). The job's end arrives as
another notification with its files. The user always hears how a job ended: if a
backend finishes without relaying anything, the end notification carries its
summary, and if it fails, the error.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from voice.language import LANGUAGES

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


@dataclass
class JobNotification:
    """News about a job for the coordinator. A summary or an error is something to tell the user."""
    job_id: str
    status: str  # running, completed or failed
    task: str
    summary: str | None = None
    error: str | None = None
    files: list[str] = field(default_factory=list)

    @property
    def needs_reply(self) -> bool:
        return bool(self.summary or self.error) or self.status == "failed"


NotifyHandler = Callable[[JobNotification], Awaitable[None]]


class Backend(Protocol):
    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str: ...


class NoWorker:
    """Runs no job, as no background worker is set up on this server: each job fails saying why, so the user
    hears it."""

    def __init__(self, why: str) -> None:
        self.why = why

    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str:
        raise RuntimeError(f"no background worker is set up on this server ({self.why})")


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
            if await tell(JobNotification(job.job_id, "running", task, summary=text)):
                relayed = True

        async def run() -> None:
            try:
                summary = await self.backend.run(job, progress, relay)
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
                await tell(JobNotification(job.job_id, "failed", task, error=job.error))
                return
            job.files = sorted(p.name for p in job.dir.iterdir()
                               if p.is_file() and p.name not in ("request.md", "events.jsonl"))
            job.status, job.ended_at = "completed", time.monotonic()
            files = [str((job.dir / name).relative_to(self.session_dir)) for name in job.files]
            self.emit("job_done", job_id=job.job_id, summary=summary, files=files)
            self._job_log(job, "done", summary=summary, files=files)
            if relayed:  # the user already heard the answer; this says only that the job and its files are done
                await tell(JobNotification(job.job_id, "completed", task, files=job.files))
                return
            # The backend never said what to tell the user; its summary will do.
            told = (summary or "").strip()[:600] or "The job finished without a summary."
            self.emit("relay", job_id=job.job_id, text=told)
            self._job_log(job, "relay", text=told)
            await tell(JobNotification(job.job_id, "completed", task, summary=told, files=job.files))

        self.tasks[job.job_id] = asyncio.create_task(run(), name=f"job-{job.job_id}")
        return job

    def status_text(self) -> str:
        """One line per job of this call, for the coordinator's job_status tool."""
        if not self.jobs:
            return "No background jobs in this call."
        now = time.monotonic()
        lines = []
        for job in self.jobs.values():
            seconds = round((job.ended_at or now) - job.started_at)
            if job.status == "running":
                parts = [f"Job {job.job_id}", f"running for {seconds} s", f"task: {short(job.task)}"]
                if job.last_step:
                    parts.append(f"latest step: {job.last_step}")
            else:
                parts = [f"Job {job.job_id}", f"{job.status} after {seconds} s", f"task: {short(job.task)}"]
                if job.files:
                    parts.append(f"files: {', '.join(job.files)}")
                if job.error:
                    parts.append(f"error: {job.error}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    async def cancel_all(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
