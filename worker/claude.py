"""The Claude worker: background jobs on Claude through Anthropic's Messages API, in a loop this Mac runs (the worker
role's `sonnet`, voice/model_catalog.py; our own runner, not Managed Agents: the user, 2026-10-15).

A job is one conversation. Claude has Anthropic's server tools, which Anthropic runs within a request:
  web_search      current facts, with sources (10 USD per 1,000 searches)
  web_fetch       a page or a PDF read in full (no charge beyond its tokens)
  code_execution  Python and bash in a sandbox, one container for the whole job (free with the web tools)
and the job's own tools (worker/tools.py: progress, relay_to_user, save_file, save_chart), which this Mac runs
between requests. A file Claude leaves in $OUTPUT_DIR in code execution comes back as a file id: this Mac saves it
into the job folder under a free, safe name at once, and deletes it from Anthropic's Files API once the job is over
(the job's conversation names the file until then, and a request naming a deleted file is refused).

Background jobs need not be quick (the user, 2026-10-15): every request may write up to the role's max_tokens with
thinking at effort high, a job may make MAX_REQUESTS requests and JOB_WEB_SEARCHES web searches, and at either limit
Claude is asked for its answer without tools (a turn that goes on searching past the search limit fails the job, so
a job never searches more than JOB_WEB_SEARCHES + WEB_SEARCHES_PER_REQUEST times). A request that fails midway (a
dropped connection, an overloaded or failing server) is tried again twice. What each request used goes into usage.json at once (tokens, cache reads and
writes, server tool uses), so the day's budget counts a job that fails, or is cancelled when the call ends (then
the request still running counts as an estimate), as well as one that finishes (voice/costs.py job_cost).
"""

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from voice.model_catalog import Choice
from voice.providers import ToolResult, ToolSpec, Turn, Usage
from voice.providers import anthropic as anthropic_api
from worker.runner import Job
from worker.tools import JOB_TOOLS, MAX_FILE_BYTES, free_name, job_request, use_tool, write_usage

log = logging.getLogger("bee-call.worker")

MAX_REQUESTS = 80  # a job's requests, its paused turns continued among them (the harness allows 80 rounds too)
JOB_WEB_SEARCHES = 60  # a job's web searches before it is asked for its answer (0.60 USD)
WEB_SEARCHES_PER_REQUEST = 20  # so one request can pass the job's limit by at most this many
FETCHED_TOKENS = 30_000  # at most this much of one fetched page or PDF reaches the conversation (about 120 KB)
REQUEST_TIMEOUT_S = 1800.0  # one request, its server tools included; the stream's own wait for its next bytes
RETRY_WAITS_S = (15, 60)  # before each new try of a request that failed midway

SERVER_TOOLS: list[dict[str, Any]] = [
    # A search or fetch whose result code execution already filtered (dynamic filtering) is left out of the answer
    # and so out of what goes back with the next request ("excluded"); one Claude read directly comes back whole.
    {"type": "web_search_20260318", "name": "web_search", "max_uses": WEB_SEARCHES_PER_REQUEST,
     "response_inclusion": "excluded"},
    {"type": "web_fetch_20260318", "name": "web_fetch", "max_content_tokens": FETCHED_TOKENS,
     "response_inclusion": "excluded"},
    # The web tools above need code execution 20260120 or later beside them.
    {"type": "code_execution_20260521", "name": "code_execution"},
]
CLIENT_TOOLS = [ToolSpec(name, spec["description"], spec["parameters"]) for name, spec in JOB_TOOLS.items()]
CODE_RESULTS = ("bash_code_execution_tool_result", "code_execution_tool_result")
KIND = re.compile(r"\.[A-Za-z0-9]{1,8}")  # a file name's kind (.csv), kept when the rest of its name cannot be

SYSTEM_PROMPT = """\
You are the background worker for Bee Call, a voice assistant that talks with the user on the phone. \
The coordinator hands you one job at a time and keeps talking with the user while you work.

How to work:
1. Call progress with a short line (under ten words) whenever you start a new step.
2. Use web_search for anything current or factual, and web_fetch to read a page or a PDF in full. For facts that \
change the answer, check at least two sources, and say so when they disagree.
3. Use code execution for calculations and tables. To give the user a file you made there (a CSV table, for \
example), copy it into $OUTPUT_DIR: it is saved into the job folder under its name.
4. When you have the answer, call relay_to_user with one or two short sentences the coordinator can say \
out loud: the conclusion first, numbers rounded, no links.
5. Then call save_file with name "report.md": the answer, how you got it, and the sources as links. \
For a chart, call save_chart with the numbers; never draw one yourself. Mention each chart and file in the \
report by its file name.
6. Finish with a one-paragraph summary as your last message. The user reads it in the app: say what you \
found and which files you saved, not how you passed it to the coordinator.
"""

LIMIT_NOTE = ("This job has used its {what}. Write your final answer now from what you have, without tools: the "
              "conclusion first, then how you got it, with the sources as links.")


# An error event in the middle of a stream comes with the stream's own status (200): its kind is in its body.
PASSING_ERRORS = ("overloaded_error", "api_error", "rate_limit_error", "timeout_error")


def retryable(exc: BaseException) -> bool:
    """A failure worth another try: the connection (also one dropped mid-stream), an overloaded or failing server,
    a rate limit."""
    import anthropic
    import httpx

    if isinstance(exc, (anthropic.APIConnectionError, httpx.TransportError)):  # timeouts among them
        return True
    if isinstance(exc, anthropic.APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        kind = body.get("error", {}).get("type") if isinstance(body.get("error"), dict) else None
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500 or kind in PASSING_ERRORS
    return False


class ClaudeWorker:
    def __init__(self, choice: Choice, client: Any = None) -> None:
        self.choice = choice
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic_api.async_client(
                api_key=anthropic_api.api_key() or None, max_retries=2,
                timeout=anthropic.Timeout(REQUEST_TIMEOUT_S, connect=10.0))
        return self._client

    async def run(self, job: Job, progress: Callable[[str], None], relay: Callable[[str], Awaitable[None]]) -> str:
        turns = [Turn("user", text=job_request(job))]
        usage: dict[str, Any] = {"provider": anthropic_api.PROVIDER, "model": self.choice.offering.model,
                                 "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
                                 "server_tools": {}, "requests": 0, "stop_reason": None, "status": "running",
                                 "steps": []}  # seconds per request and per job tool call
        container: str | None = None
        last = False  # the next request asks for the answer without tools
        outputs: dict[str, str] = {}  # file id -> what became of it
        try:
            for n in range(MAX_REQUESTS):
                params = anthropic_api.ADAPTER.params(self.choice, SYSTEM_PROMPT, turns, CLIENT_TOOLS)
                params["tools"] = SERVER_TOOLS + params["tools"]
                if container:
                    params["container"] = container
                if last:
                    params["tool_choice"] = {"type": "none"}
                started = time.monotonic()
                final = await self._request(params, job, usage)
                usage["steps"].append({"request_s": round(time.monotonic() - started, 1), "calls": []})
                usage["stop_reason"] = final.stop_reason
                write_usage(job, usage)
                content = anthropic_api.content_of(final)
                container = getattr(getattr(final, "container", None), "id", None) or container
                turns.append(Turn("assistant", native=content, native_provider=anthropic_api.PROVIDER))
                await self._save_outputs(content, job, outputs)
                searches = usage["server_tools"].get("web_search", 0)
                if final.stop_reason == "end_turn":
                    break
                if final.stop_reason == "pause_turn":  # a long server tool turn: sent back as it came
                    if searches > JOB_WEB_SEARCHES:
                        raise RuntimeError(f"the worker kept searching past this job's limit of {JOB_WEB_SEARCHES} "
                                           "web searches")
                    continue
                if final.stop_reason != "tool_use" or last:
                    raise RuntimeError(f"the worker stopped before finishing ({final.stop_reason or 'no stop reason'})")
                results = await self._use_tools(content, job, progress, relay, usage["steps"][-1]["calls"])
                write_usage(job, usage)
                note = None
                if searches >= JOB_WEB_SEARCHES:
                    note, last = LIMIT_NOTE.format(what="web searches"), True
                elif n + 2 >= MAX_REQUESTS:  # the next request is the job's last
                    note, last = LIMIT_NOTE.format(what="requests"), True
                turns.append(Turn("user", text=note or "", results=results))
            else:
                raise RuntimeError(f"the worker did not finish within {MAX_REQUESTS} requests")
            usage["status"] = "completed"
        except asyncio.CancelledError:
            usage["status"] = "cancelled"
            raise
        except BaseException:
            usage["status"] = "failed"
            raise
        finally:
            write_usage(job, usage)
            if usage["status"] != "cancelled":  # a call that ended is not held up for this
                await self._forget_outputs(outputs, job)
        summary = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip() \
            or "The worker finished without a summary."
        if not (job.dir / "report.md").exists():
            (job.dir / "report.md").write_text(f"# Report for job {job.job_id}\n\n{summary}\n")
        log.info("job %s took %d requests: %s", job.job_id, usage["requests"], usage["steps"])
        return summary

    async def _request(self, params: dict[str, Any], job: Job, usage: dict[str, Any]) -> Any:
        """One request, tried again after a failure worth it; each try's use is in usage.json as soon as it ends."""
        for attempt in range(len(RETRY_WAITS_S) + 1):
            used = Usage()
            try:
                return await anthropic_api.ADAPTER.stream(params, usage=used, client=self.client())
            except Exception as exc:
                if attempt == len(RETRY_WAITS_S) or not retryable(exc):
                    raise
                log.warning("job %s: a request failed (%s); trying again", job.job_id, exc)
            finally:
                _count(usage, used)
                write_usage(job, usage)
            await asyncio.sleep(RETRY_WAITS_S[attempt])
        raise AssertionError("unreachable")

    async def _use_tools(self, content: list[dict[str, Any]], job: Job, progress: Callable[[str], None],
                         relay: Callable[[str], Awaitable[None]], calls: list[dict[str, Any]]) -> list[ToolResult]:
        """Runs the job's tools Claude called, in order; a failure goes back to Claude, which can try another way."""
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name, started = block.get("name"), time.monotonic()
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name not in JOB_TOOLS:
                log.warning("the Claude worker called an unknown tool %r", name)
                text, ok = f"There is no tool named {name}. Use one of: {', '.join(JOB_TOOLS)}.", False
            else:
                try:
                    text, ok = await use_tool(name, args, job, progress, relay)
                except Exception as exc:
                    text, ok = f"{name} failed: {exc}", False
            calls.append({"name": name, "s": round(time.monotonic() - started, 1)})
            results.append(ToolResult(block["id"], text, error=not ok))
        if not results:
            raise RuntimeError("the worker stopped for tools but called none of the job's")
        return results

    async def _save_outputs(self, content: list[dict[str, Any]], job: Job, outputs: dict[str, str]) -> None:
        """Saves into the job folder each file code execution left in $OUTPUT_DIR (not one over MAX_FILE_BYTES)."""
        found = [item["file_id"] for block in content if block.get("type") in CODE_RESULTS
                 and isinstance(block.get("content"), dict)
                 for item in block["content"].get("content") or []
                 if isinstance(item, dict) and isinstance(item.get("file_id"), str)]
        files = self.client().files
        for file_id in found:
            if file_id in outputs:
                continue
            n = len(outputs) + 1
            outputs[file_id] = "failed"
            try:
                about = await files.retrieve_metadata(file_id)
                given = str(getattr(about, "filename", "") or "")
                if (getattr(about, "size_bytes", 0) or 0) > MAX_FILE_BYTES:
                    log.warning("job %s: %s from code execution is over %d bytes; not saved", job.job_id, given,
                                MAX_FILE_BYTES)
                    outputs[file_id] = "too large"
                else:
                    data = await (await files.download(file_id)).read()
                    kind = given[given.rfind("."):] if "." in given else ""
                    name = free_name(job.dir, given, f"output-{n}{kind if KIND.fullmatch(kind) else ''}")
                    if len(data) > MAX_FILE_BYTES:
                        outputs[file_id] = "too large"
                    else:
                        (job.dir / name).write_bytes(data)
                        outputs[file_id] = name
            except Exception:  # left in the Files API: it may come down later by hand
                log.warning("job %s: could not save %s from code execution", job.job_id, file_id, exc_info=True)

    async def _forget_outputs(self, outputs: dict[str, str], job: Job) -> None:
        """Deletes from the Files API the files the job saved (or found too large), once its conversation is over."""
        for file_id, became in outputs.items():
            if became == "failed":
                continue
            try:
                await self.client().files.delete(file_id)
            except Exception:
                log.warning("job %s: could not delete %s from the Files API", job.job_id, file_id, exc_info=True)


def _count(usage: dict[str, Any], used: Usage) -> None:
    """One request's use, added to the job's usage.json totals."""
    usage["requests"] += 1
    usage["input_tokens"] += used.input_tokens
    usage["output_tokens"] += used.output_tokens
    usage["cache_read"] += used.cache_read
    usage["cache_write"] += used.cache_write
    for name, count in used.server_tools.items():
        usage["server_tools"][name] = usage["server_tools"].get(name, 0) + count
    if used.estimated:
        usage["estimated"] = True
