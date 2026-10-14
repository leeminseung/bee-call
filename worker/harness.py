"""The cloud worker: an Amazon Bedrock AgentCore Harness agent that does delegated jobs.

AWS runs the agent loop (Strands-based) in an isolated microVM per session, in
Sydney. This Mac starts a job with InvokeHarness and serves the harness's inline
functions: when the agent calls one, the harness stops and hands the call to us;
we run it and send the result back in the same session, and the agent carries on.

Inline functions (run here, on this Mac):
  progress(text)                       a line on the job card
  relay_to_user(text)                  one or two sentences for the voice to tell the user
  web_search(question)                 Nova Web Grounding (us-east-1): an answer with sources
  save_file(name, content)             a text result file in the job folder (report.md, a CSV table)
  save_chart(name, title, kind, ...)   a chart from numbers; this Mac draws it as SVG (worker/chart.py)
Built-in tool (run by AWS): AgentCore Code Interpreter, for calculations, except for models that
fail with it (NO_CODE_INTERPRETER).

The job folder stays on this Mac, with one writer per file: the agent writes into it
only through save_file. The harness itself is created by scripts/deploy_worker.py.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import botocore.session
from botocore.config import Config
from botocore.exceptions import EventStreamError

from beelink.clock import now_text
from voice.aws_session import aws_session
from voice.costs import SpendLog
from worker.chart import render_svg
from worker.runner import Job
from worker.web_search import SearchResult, web_search

log = logging.getLogger("bee-call.worker")

REGION = "ap-southeast-2"
HARNESS_NAME = "bee_call_worker"
# Written by scripts/deploy_worker.py; machine-wide, not one person's (voice/people.py data root).
STATE_FILE = Path(os.environ.get("BEE_CALL_DATA") or "data") / "harness.json"
OLD_STATE_FILE = Path("workspace") / "state" / "harness.json"  # where it was before the per-person folders
# The product model is Claude Opus 5.5. While Claude on Bedrock is blocked for this account, GLM 5
# stands in (Matt, 2026-10-13): steps take 1-5 s and it runs the code interpreter. Kimi K3 checked
# sources more carefully but thought for up to a minute or more per step and cannot use the code
# interpreter in the harness.
DEFAULT_MODEL = os.environ.get("WORKER_MODEL", "zai.glm-5")
# Kimi K3 in the harness fails on its first model call ("The task request was rejected by the
# target") when the built-in code interpreter is attached, and works without it (2026-10-12).
# Nova 2 Lite (2026-10-12) and GLM 5 (2026-10-13) work with it. Claude is untested (blocked).
NO_CODE_INTERPRETER = ("moonshotai.kimi-k3",)
REGION_PREFIXES = ("global.", "us.", "eu.", "au.", "apac.", "jp.")
MAX_ROUNDS = 40  # inline-function round trips per job (arbitrary)
MAX_FILE_BYTES = 5_000_000
MAX_TOOL_TEXT = 8000

SYSTEM_PROMPT = """\
You are the background worker for Bee Call, a voice assistant that talks with the user on the phone. \
The coordinator hands you one job at a time and keeps talking with the user while you work.

How to work:
1. Call progress with a short line (under ten words) whenever you start a new step.
2. Use web_search for anything current or factual. For facts that change the answer, check at least \
two sources, and say so when they disagree.
3. {calculations}
4. When you have the answer, call relay_to_user with one or two short sentences the coordinator can say \
out loud: the conclusion first, numbers rounded, no links.
5. Then call save_file with name "report.md": the answer, how you got it, and the sources as links. \
For a chart, call save_chart with the numbers; never write image data yourself. Mention each chart in the \
report by its file name.
6. Finish with a one-paragraph summary as your last message. The user reads it in the app: say what you \
found and which files you saved, not how you passed it to the coordinator.
"""


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


INLINE_FUNCTIONS: dict[str, dict[str, Any]] = {
    "progress": {
        "description": "Show a short progress line on the job card the user sees.",
        "inputSchema": _schema({"text": {"type": "string", "description": "Under ten words."}}, ["text"]),
    },
    "relay_to_user": {
        "description": "Give the coordinator one or two short sentences to say to the user out loud.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "web_search": {
        "description": "Search the web and get an answer with numbered sources.",
        "inputSchema": _schema({"question": {"type": "string", "description": "A specific question."}}, ["question"]),
    },
    "save_file": {
        "description": "Save a text result file (Markdown or CSV) into the job folder the user can open in the app.",
        "inputSchema": _schema({
            "name": {"type": "string", "description": "File name such as report.md or prices.csv; no folders."},
            "content": {"type": "string"},
        }, ["name", "content"]),
    },
    "save_chart": {
        "description": "Draw a line or bar chart from numbers and save it into the job folder as an image.",
        "inputSchema": _schema({
            "name": {"type": "string", "description": "File name without extension, such as rain_by_hour."},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["line", "bar"]},
            "x_label": {"type": "string"},
            "y_label": {"type": "string"},
            "series": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"},
                "points": {"type": "array", "description": "[[x, y], ...]; x is a number or a category name.",
                           "items": {"type": "array"}}}, "required": ["label", "points"]}},
        }, ["name", "title", "kind", "series"]),
    },
}


def new_session_id(tag: str) -> str:
    return f"beecall-{tag}-{uuid.uuid4().hex}"  # 33 to 100 characters


def uses_code_interpreter(model_id: str) -> bool:
    for prefix in REGION_PREFIXES:
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
            break
    return not model_id.startswith(NO_CODE_INTERPRETER)


def system_prompt(code_interpreter: bool) -> str:
    calculations = ("Use the code interpreter for calculations, tables and charts." if code_interpreter else
                    "Work out calculations step by step, and put the numbers you used in the report.")
    return SYSTEM_PROMPT.format(calculations=calculations)


def harness_tools(code_interpreter: bool = True) -> list[dict[str, Any]]:
    tools = [{"type": "inline_function", "name": name, "config": {"inlineFunction": spec}}
             for name, spec in INLINE_FUNCTIONS.items()]
    if code_interpreter:
        tools.append({"type": "agentcore_code_interpreter", "name": "code_interpreter",
                      "config": {"agentCoreCodeInterpreter": {}}})
    return tools


def harness_definition(role_arn: str, model_id: str = DEFAULT_MODEL) -> dict[str, Any]:
    """The CreateHarness / UpdateHarness parameters for the worker."""
    code_interpreter = uses_code_interpreter(model_id)
    return {
        "harnessName": HARNESS_NAME,
        "executionRoleArn": role_arn,
        "model": {"bedrockModelConfig": {"modelId": model_id, "maxTokens": 4096}},
        "systemPrompt": [{"text": system_prompt(code_interpreter)}],
        "tools": harness_tools(code_interpreter),
        "memory": {"disabled": {}},
        "truncation": {"strategy": "sliding_window", "config": {"slidingWindow": {"messagesCount": 60}}},
        "environment": {"agentCoreRuntimeEnvironment": {
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600},
        }},
        "maxIterations": 60,
        "timeoutSeconds": 900,
    }


# ---------- reading the event stream ----------

@dataclass
class ToolUse:
    tool_use_id: str
    name: str
    input_json: str = ""

    @property
    def input(self) -> dict[str, Any]:
        try:
            value = json.loads(self.input_json or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Round:
    """What one InvokeHarness stream said, up to the point the harness stopped."""
    text: str = ""  # text of the last assistant message
    tool_uses: list[ToolUse] = field(default_factory=list)  # in the last assistant message
    answered: set[str] = field(default_factory=set)  # tool uses the harness already ran (built-in tools)
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[ToolUse]:
        # Every call the harness left unanswered is ours to answer, including calls to tools that do
        # not exist: those get an error result so the agent can carry on. Leaving them out ended a
        # job with stop reason tool_use and nothing to do (2026-10-13).
        return [t for t in self.tool_uses if t.name not in BUILT_IN_TOOLS and t.tool_use_id not in self.answered]


def read_round(events: Iterable[dict[str, Any]]) -> Round:
    out = Round()
    try:
        _read_events(events, out)
    except EventStreamError as exc:  # botocore raises exception events while iterating
        out.errors.append(str(exc))
    return out


def _read_events(events: Iterable[dict[str, Any]], out: Round) -> None:
    role, blocks = None, {}
    for event in events:
        if "messageStart" in event:
            role, blocks = event["messageStart"].get("role"), {}
            if role == "assistant":
                out.text, out.tool_uses = "", []
        elif "contentBlockStart" in event:
            start = event["contentBlockStart"]
            payload = start.get("start", {})
            if "toolUse" in payload:
                use = ToolUse(payload["toolUse"]["toolUseId"], payload["toolUse"]["name"])
                blocks[start["contentBlockIndex"]] = use
                if role == "assistant":
                    out.tool_uses.append(use)
            elif "toolResult" in payload:
                out.answered.add(payload["toolResult"]["toolUseId"])
        elif "contentBlockDelta" in event:
            delta_event = event["contentBlockDelta"]
            delta = delta_event.get("delta", {})
            if "text" in delta and role == "assistant":
                out.text += delta["text"]
            elif "toolUse" in delta:
                use = blocks.get(delta_event.get("contentBlockIndex"))
                if use is not None:
                    use.input_json += delta["toolUse"].get("input", "")
        elif "messageStop" in event:
            out.stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            out.input_tokens += usage.get("inputTokens", 0)
            out.output_tokens += usage.get("outputTokens", 0)
        else:  # contentBlockStop needs nothing; exception events end the round with an error
            out.errors += [f"{name}: {value}" for name, value in event.items() if name.endswith(("Exception", "Error"))]


# ---------- running a job ----------

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
RESERVED = {"request.md", "events.jsonl", "usage.json"}  # written by this Mac, never by the agent
FINISHED = {"end_turn", "stop_sequence"}
# A new harness session starts a fresh microVM, which took about 30 s on 2026-10-13, so the voice
# server warms one at call start and keeps it from idling out until a job takes it.
WARM_TEXT = ("Warm-up before a phone call, not a job. Reply with the single word ready and call no tools. "
             "Jobs come in later messages.")
KEEP_WARM_S = 240  # under the session idle timeout (idleRuntimeSessionTimeout 300 in harness_definition)
BUILT_IN_TOOLS = {"code_interpreter"}  # run by the harness itself


def update_request(definition: dict[str, Any], harness_id: str) -> dict[str, Any]:
    """UpdateHarness parameters for a definition: some fields must be wrapped in {"optionalValue": ...}."""
    shape = botocore.session.get_session().get_service_model("bedrock-agentcore-control") \
        .operation_model("UpdateHarness").input_shape
    out: dict[str, Any] = {"harnessId": harness_id}
    for key, value in definition.items():
        if key == "harnessName":
            continue
        member = shape.members[key]
        wrapped = member.type_name == "structure" and list(member.members) == ["optionalValue"]
        out[key] = {"optionalValue": value} if wrapped else value
    return out


def allowed_name(name: str) -> bool:
    return bool(SAFE_NAME.fullmatch(name)) and name.casefold() not in RESERVED


def load_harness_arn() -> str | None:
    if os.environ.get("WORKER_HARNESS_ARN"):
        return os.environ["WORKER_HARNESS_ARN"]
    for path in (STATE_FILE, OLD_STATE_FILE):
        if path.exists():
            return json.loads(path.read_text()).get("harnessArn")
    return None


class HarnessWorker:
    def __init__(self, harness_arn: str | None = None, client: Any | None = None,
                 search: Callable[[str], Awaitable[SearchResult]] = web_search) -> None:
        self.harness_arn = harness_arn or load_harness_arn()
        if not self.harness_arn:
            raise RuntimeError("no harness ARN: run scripts/deploy_worker.py first")
        self.client = client or aws_session().client(
            "bedrock-agentcore", region_name=REGION, config=Config(read_timeout=900))
        self.search = search
        self._ready: str | None = None  # a warm session no job is using
        self._warm_lock = asyncio.Lock()  # one invocation at a time on the warm session

    async def keep_warm(self, spend: SpendLog) -> None:
        """Keeps one harness session awake for the next job. Run it as a task for the length of a
        call and cancel it when the call ends; a job takes the warm session when it starts. What each warm-up
        costs goes into the person's spend log."""
        try:
            while True:
                async with self._warm_lock:
                    self._ready = self._ready or new_session_id("warm")
                    used = [0, 0]
                    try:
                        await self._ping(self._ready, used)
                    except Exception:
                        log.warning("worker warm-up failed", exc_info=True)
                        self._ready = None
                    finally:
                        if any(used):
                            spend.add("worker_warm_up", model=DEFAULT_MODEL, input_tokens=used[0],
                                      output_tokens=used[1])
                await asyncio.sleep(KEEP_WARM_S)
        except asyncio.CancelledError:
            pass  # the call ended

    async def _ping(self, session_id: str, used: list[int]) -> None:
        """One warm-up; `used` gets its tokens (input, output) as they are reported."""
        messages = [{"role": "user", "content": [{"text": WARM_TEXT}]}]
        for _ in range(3):
            found = await asyncio.to_thread(self._invoke, session_id, messages)
            used[0] += found.input_tokens
            used[1] += found.output_tokens
            if found.errors:
                raise RuntimeError("; ".join(found.errors))
            if not found.pending:
                return
            messages = [{"role": "user", "content": [
                {"toolResult": {"toolUseId": use.tool_use_id, "status": "error",
                                "content": [{"text": "This is a warm-up, not a job. Do not call tools now."}]}}
                for use in found.pending]}]

    async def _take_session(self, job_id: str) -> str:
        # Waiting for a warm-up still in flight is quicker than starting another cold session.
        async with self._warm_lock:
            session_id, self._ready = self._ready, None
        return session_id or new_session_id(f"job-{job_id}")

    def _invoke(self, session_id: str, messages: list[dict[str, Any]]) -> Round:
        response = self.client.invoke_harness(harnessArn=self.harness_arn, runtimeSessionId=session_id,
                                              messages=messages)
        return read_round(response["stream"])

    async def _call(self, use: ToolUse, job: Job, progress: Callable[[str], None],
                    relay: Callable[[str], Awaitable[None]]) -> tuple[str, str]:
        """Runs one inline function. Returns (text for the agent, status)."""
        args = use.input
        if use.name == "progress":
            progress(str(args.get("text", ""))[:200])
            return "Shown on the job card.", "success"
        if use.name == "relay_to_user":
            await relay(str(args.get("text", "")))
            return "The coordinator will tell the user.", "success"
        if use.name == "web_search":
            result = await self.search(str(args.get("question", "")))
            return result.markdown()[:MAX_TOOL_TEXT], "success"
        if use.name == "save_file":
            name = str(args.get("name", ""))
            if not allowed_name(name):
                return f"Not saved: {name!r} is not an allowed file name.", "error"
            data = str(args.get("content", "")).encode()
            if len(data) > MAX_FILE_BYTES:
                return f"Not saved: {len(data)} bytes is over the {MAX_FILE_BYTES} byte limit.", "error"
            (job.dir / name).write_bytes(data)
            return f"Saved {name} ({len(data)} bytes).", "success"
        if use.name == "save_chart":
            name = f"{str(args.get('name', 'chart')).removesuffix('.svg')}.svg"
            if not allowed_name(name):
                return f"Not saved: {name!r} is not an allowed file name.", "error"
            svg = render_svg(str(args.get("title", "")), str(args.get("kind", "line")), args.get("series") or [],
                             str(args.get("x_label", "")), str(args.get("y_label", "")))
            (job.dir / name).write_text(svg)
            return f"Saved {name}. Mention it in the report.", "success"
        log.warning("worker called an unknown tool %r", use.name)
        return f"There is no tool named {use.name}. Use one of: {', '.join(INLINE_FUNCTIONS)}.", "error"

    async def run(self, job: Job, progress: Callable[[str], None], relay: Callable[[str], Awaitable[None]]) -> str:
        session_id = await self._take_session(job.job_id)
        # The worker has no clock; relative dates in the task ("this Saturday") need today's date.
        messages = [{"role": "user", "content": [{"text": f"Job {job.job_id} (it is now {now_text(job.now)}; read today, "
                                                         f"tomorrow and weekdays against this): {job.task}"}]}]
        # What the job used so far, in usage.json after every round, so the daily budget counts a job that fails or
        # is cancelled when the call ends as well as one that finishes (voice/costs.py).
        usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "stop_reason": None, "status": "running",
                                 "steps": []}  # seconds per model round (AWS side, built-in tools included) and call
        steps = usage["steps"]
        try:
            for _ in range(MAX_ROUNDS):
                started = time.monotonic()
                found = await asyncio.to_thread(self._invoke, session_id, messages)
                steps.append({"round_s": round(time.monotonic() - started, 1), "calls": []})
                usage["input_tokens"] += found.input_tokens
                usage["output_tokens"] += found.output_tokens
                usage["stop_reason"] = found.stop_reason
                write_usage(job, usage)
                if found.errors:
                    raise RuntimeError("; ".join(found.errors))
                if not found.pending:
                    break
                results = []
                for use in found.pending:
                    started = time.monotonic()
                    try:
                        text, status = await self._call(use, job, progress, relay)
                    except Exception as exc:  # the agent hears about it and can try another way
                        text, status = f"{use.name} failed: {exc}", "error"
                    steps[-1]["calls"].append({"name": use.name, "s": round(time.monotonic() - started, 1)})
                    write_usage(job, usage)  # a web search costs as soon as it ran
                    results.append({"toolResult": {"toolUseId": use.tool_use_id, "content": [{"text": text}],
                                                   "status": status}})
                messages = [{"role": "user", "content": results}]
            else:
                raise RuntimeError(f"the worker did not finish within {MAX_ROUNDS} rounds")
            if found.stop_reason not in FINISHED:
                # max_iterations_exceeded, timeout_exceeded, max_tokens, content_filtered and the like
                raise RuntimeError(f"the worker stopped before finishing ({found.stop_reason or 'no stop reason'})")
            usage["status"] = "completed"
        except asyncio.CancelledError:
            usage["status"] = "cancelled"
            raise
        except Exception:
            usage["status"] = "failed"
            raise
        finally:
            write_usage(job, usage)
        summary = found.text.strip() or "The worker finished without a summary."
        if not (job.dir / "report.md").exists():
            (job.dir / "report.md").write_text(f"# Report for job {job.job_id}\n\n{summary}\n")
        log.info("job %s took %d rounds: %s", job.job_id, len(steps), steps)
        self._ready = self._ready or session_id  # still warm: the next job can use it
        return summary


def write_usage(job: Job, usage: dict[str, Any]) -> None:
    """The job's usage.json, replaced whole, so a reader never sees half of it."""
    tmp = job.dir / ".usage.json.tmp"
    tmp.write_text(json.dumps(usage))
    tmp.replace(job.dir / "usage.json")
