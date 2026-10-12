"""Runs delegated jobs for a call and reports back to the voice server.

The boundary with any worker backend is one job folder:
  jobs/<NN>/request.md     what the coordinator asked for (written here)
  jobs/<NN>/events.jsonl   the job's own progress log (written here, append-only)
  jobs/<NN>/report.md      the backend's final report (written by the backend)
  jobs/<NN>/...            any files the backend made (charts, tables)

A backend only needs `async run(job, progress, relay) -> str` returning a
one-paragraph summary. `progress(text)` updates the job card; `relay(text)`
hands the coordinator a sentence to tell the user. The user always hears how a
job ended: if a backend finishes without relaying anything, its summary is
relayed, and if it fails, the failure is relayed.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

EmitEvent = Callable[..., None]  # recorder.event(name, **fields)
RelayHandler = Callable[..., Awaitable[None]]  # (job_id, text, failed=False)
log = logging.getLogger("worker.runner")


@dataclass
class Job:
    job_id: str
    dir: Path
    task: str


class Backend(Protocol):
    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str: ...


class JobRunner:
    def __init__(self, session_dir: Path, emit: EmitEvent, backend: Backend, on_relay: RelayHandler) -> None:
        self.session_dir = session_dir
        self.emit = emit
        self.backend = backend
        self.on_relay = on_relay
        self.tasks: dict[str, asyncio.Task] = {}

    def _next_job(self, task: str) -> Job:
        jobs = self.session_dir / "jobs"
        jobs.mkdir(exist_ok=True)
        n = len([p for p in jobs.iterdir() if p.is_dir()]) + 1
        job = Job(job_id=f"{n:02d}", dir=jobs / f"{n:02d}", task=task)
        job.dir.mkdir()
        (job.dir / "request.md").write_text(f"# Job {job.job_id}\n\n{task}\n")
        return job

    def _job_log(self, job: Job, name: str, **fields) -> None:
        with (job.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"at": time.time(), "type": name, **fields}, ensure_ascii=False) + "\n")

    def start(self, task: str) -> Job:
        """Starts a job and returns at once; the call keeps going while it runs."""
        job = self._next_job(task)
        self.emit("job_started", job_id=job.job_id, task=task)
        self._job_log(job, "started", task=task)

        def progress(text: str) -> None:
            self.emit("job_progress", job_id=job.job_id, text=text)
            self._job_log(job, "progress", text=text)

        relayed = False

        async def relay(text: str) -> None:
            nonlocal relayed
            relayed = True
            self.emit("relay", job_id=job.job_id, text=text)
            self._job_log(job, "relay", text=text)
            await self.on_relay(job.job_id, text)

        async def tell_failure(error: str) -> None:
            try:
                await self.on_relay(job.job_id, error, failed=True)
            except Exception:
                log.exception("could not tell the user that job %s failed", job.job_id)

        async def run() -> None:
            try:
                summary = await self.backend.run(job, progress, relay)
            except asyncio.CancelledError:
                self.emit("job_failed", job_id=job.job_id, error="cancelled")
                self._job_log(job, "cancelled")
                raise
            except Exception as exc:
                self.emit("job_failed", job_id=job.job_id, error=str(exc))
                self._job_log(job, "failed", error=str(exc))
                await tell_failure(str(exc)[:300])
                return
            files = sorted(str(p.relative_to(self.session_dir)) for p in job.dir.iterdir()
                           if p.is_file() and p.name not in ("request.md", "events.jsonl"))
            self.emit("job_done", job_id=job.job_id, summary=summary, files=files)
            self._job_log(job, "done", summary=summary, files=files)
            if not relayed:  # the backend never said what to tell the user; its summary will do
                await relay(summary[:600])

        self.tasks[job.job_id] = asyncio.create_task(run(), name=f"job-{job.job_id}")
        return job

    async def cancel_all(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
