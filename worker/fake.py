"""Test stand-in for the worker until AgentCore Harness is wired up.

It pretends to work for a few seconds, writes a small report and a CSV, and
relays one sentence. It does no real analysis; it exists so the delegate ->
progress -> relay -> job card flow can be tested end to end.
"""

import asyncio
from typing import Awaitable, Callable

from worker.runner import Job


class FakeWorker:
    def __init__(self, step_seconds: float = 2.0) -> None:
        self.step_seconds = step_seconds

    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str:
        progress("Reading the request")
        await asyncio.sleep(self.step_seconds)
        progress("Running a placeholder calculation")
        await asyncio.sleep(self.step_seconds)
        (job.dir / "result.csv").write_text("item,value\nplaceholder,42\n")
        report = (f"# Report for job {job.job_id}\n\nThis is a test report from the fake worker.\n\n"
                  f"Task: {job.task}\n\nResult: placeholder value 42 (see result.csv).\n")
        (job.dir / "report.md").write_text(report)
        await relay(f"The test job is done. The placeholder result is 42.")
        return "Fake worker finished; placeholder result 42."
