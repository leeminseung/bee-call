"""Test stand-in worker that answers a delegated job with one web search (Nova Web Grounding).

The product worker is the AgentCore Harness agent running Claude. Until Claude on
Bedrock is available, this lets the delegate -> job card -> relay flow do real
research: it writes the answer and its sources to report.md and relays a summary
of at most two sentences. It is a test stand-in, not the product.
"""

from typing import Awaitable, Callable

from beelink.clock import now_text
from worker.runner import Job
from worker.web_search import SearchResult, web_search

Search = Callable[[str], Awaitable[SearchResult]]


class SearchWorker:
    def __init__(self, search: Search = web_search) -> None:
        self.search = search

    async def run(self, job: Job, progress: Callable[[str], None],
                  relay: Callable[[str], Awaitable[None]]) -> str:
        progress("Searching the web")
        # The search model has no clock either; give it today's date for relative dates.
        result = await self.search(f"{job.task} (It is now {now_text()}.)")
        (job.dir / "report.md").write_text(f"# Report for job {job.job_id}\n\nTask: {job.task}\n\n{result.markdown()}")
        progress(f"Read {len(result.sources)} sources")
        await relay(result.summary or "The web search found nothing useful.")
        return f"Web search answered with {len(result.sources)} sources (about {result.cost_usd:.3f} USD)."
