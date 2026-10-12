"""Which Bedrock model can sit in the coordinator slot while Claude is blocked?

Runs the real coordinator (its instructions and tools) in a LiveKit text session against
each model, through LiveKit's Bedrock Converse plugin: the path the voice server uses for
any model other than Claude. Six short scenes from one call. Bee search returns a canned
conversation and delegated jobs do not run, so nothing touches Bee or the worker.

For every scene it records the time to the first token of each model request, what the
model said, which tools it called with what, and problems: replies too long to say in one
breath, written formatting (lists, symbols), or a wrong or missing tool call.

Run:  uv run python probes/coordinator_bench.py [--rounds N] [model ...]
Cost: about 1-2 US cents per model and round (around ten requests of 2-3K input tokens).
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from probes/ like the other probes
os.environ.setdefault("AWS_PROFILE", "bee-call")

from livekit.agents import AgentSession, llm  # noqa: E402
from livekit.plugins import aws  # noqa: E402

import beelink.cli  # noqa: E402
from voice.coordinator import Coordinator  # noqa: E402
from voice.recorder import CallRecorder  # noqa: E402

REGION = "ap-southeast-2"
MODELS = ["moonshotai.kimi-k2.5", "zai.glm-5", "deepseek.v3.2", "minimax.minimax-m2.5",
          "global.amazon.nova-2-lite-v1:0"]
GREETING = "Hi, it's Bee Call. What's on your mind?"
CONTEXT_NOTE = ("Note from the app, call context: The user called you. Unconfirmed Bee facts to check: "
                "[fact 812] The user's team offsite is next Friday.")
BEE_HIT = [{"conversation_id": 5121, "start_time": "2026-10-11T18:30:00+09:00",
            "short_summary": "Planned a Saturday hike with Dana",
            "summary": ("Matt and Dana agreed to hike Gwanaksan on Saturday morning if it does not rain. "
                        "They meet at Seoul National University station at 8 a.m. Dana brings snacks.")}]
JOB_NOTE = ("Note from the app: background job 01 finished. Result: Saturday in Seoul looks dry, rain chance "
            "about 10%, high around 20°C. Gwanaksan's main trail from Seoul National University is the easier one, "
            "about 2.5 hours round trip. Bukhansan's Baegundae route is steeper, about 4 hours. "
            "The report and sources are in the app.")

# (name, how the scene starts, what it should do). "say" scenes are user turns, "note" scenes
# are app notes followed by a reply request, as the voice server does for finished jobs.
SCENES = [
    ("small talk", ("say", "Hey. I just got out of a long meeting and I'm kind of tired."), None),
    ("delegate", ("say", "Can you check if it's going to rain in Seoul this Saturday, and which hike is easier, "
                         "Gwanaksan or Bukhansan?"), "delegate"),
    ("Bee recall", ("say", "What did Dana and I agree on yesterday about the hike?"), "search_bee"),
    ("job result", ("note", JOB_NOTE), None),
    ("confirm fact", ("say", "Oh, and yes, the team offsite is next Friday. That one's right."), "queue_bee_write"),
    ("goodbye", ("say", "Great, that's all for now. Thanks!"), "end_call"),
]
MAX_WORDS = 40  # about one breath of speech (arbitrary); a job result may take two or three sentences
MAX_WORDS_JOB_RESULT = 60
WRITTEN = re.compile(r"(^\s*[-*•]\s|^\s*\d+\.\s|\*\*|#|\||`|https?://|[\U0001F300-\U0001FAFF])", re.M)
# Tool-call syntax leaking into the reply text; the voice would read it out.
MARKUP = re.compile(r"(<\s*[|｜/]?\s*[A-Za-z_｜]|function_call|tool_call|DSML)")


class StubJobs:
    """Stands in for the job runner: records the task, starts nothing."""

    def __init__(self) -> None:
        self.tasks: list[str] = []

    def start(self, task: str):
        self.tasks.append(task)
        return SimpleNamespace(job_id=f"{len(self.tasks):02d}")


async def fake_bee_search(query: str, limit: int = 3, timeout: float | None = None):
    return BEE_HIT


async def run_once(model_id: str, workspace: Path) -> list[dict]:
    beelink.cli.search_conversations = fake_bee_search  # the coordinator calls it through the module
    model = aws.LLM(model=model_id, region=REGION)
    requests: list[dict] = []
    model.on("metrics_collected", lambda m: requests.append(
        {"ttft": m.ttft, "duration": m.duration, "in": m.prompt_tokens, "out": m.completion_tokens}))
    agent = Coordinator(CallRecorder(workspace, room="bench"), StubJobs(), workspace)
    results = []
    async with AgentSession(llm=model) as session:
        await session.start(agent)
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role="assistant", content=GREETING)
        ctx.add_message(role="system", content=CONTEXT_NOTE)
        await agent.update_chat_ctx(ctx)
        for name, (kind, text), expected_tool in SCENES:
            seen = len(session.history.items)
            first_request = len(requests)
            started = time.monotonic()
            error = None
            try:
                if kind == "say":
                    await asyncio.wait_for(session.run(user_input=text), 60)
                else:
                    ctx = agent.chat_ctx.copy()
                    ctx.add_message(role="system", content=text)
                    await agent.update_chat_ctx(ctx)
                    await asyncio.wait_for(session.generate_reply(), 60)
            except Exception as exc:  # a broken model or plugin path is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"[:200]
            new = session.history.items[seen:]
            said = [i.text_content for i in new if getattr(i, "role", None) == "assistant" and i.text_content]
            tools = [(i.name, i.arguments) for i in new if getattr(i, "type", None) == "function_call"]
            reqs = requests[first_request:]
            searched_before = any(t[0] == "search_bee" for s in results for t in s["tools"])
            problems = []
            if error:
                problems.append("error")
            limit = MAX_WORDS_JOB_RESULT if name == "job result" else MAX_WORDS
            if any(len(s.split()) > limit for s in said):
                problems.append("long")
            if any(MARKUP.search(s) for s in said):
                problems.append("tool syntax in speech")
            elif any(WRITTEN.search(s) for s in said):
                problems.append("written format")
            names = [t[0] for t in tools]
            if expected_tool == "search_bee":
                # Answering from a search it already did earlier in the call is fine.
                if "search_bee" not in names and not searched_before:
                    problems.append("no search_bee")
                if not any("gwanak" in s.lower() for s in said):
                    problems.append("wrong recall")
            elif expected_tool and expected_tool not in names:
                problems.append(f"no {expected_tool}")
            if not expected_tool and names:
                problems.append("unexpected tool " + ",".join(names))
            if expected_tool == "queue_bee_write" and "queue_bee_write" in names:
                args = json.loads(dict(tools)["queue_bee_write"] or "{}")
                if args.get("kind") != "fact_confirm" or str(args.get("fact_id")) != "812":
                    problems.append("wrong bee write")
            if not said and name != "goodbye":
                problems.append("said nothing")
            results.append({"scene": name, "seconds": round(time.monotonic() - started, 2),
                            "ttft": [round(r["ttft"], 2) for r in reqs if r["ttft"] is not None and r["ttft"] >= 0],
                            "tokens_in": sum(r["in"] or 0 for r in reqs), "tokens_out": sum(r["out"] or 0 for r in reqs),
                            "said": said, "tools": tools, "problems": problems, "error": error})
            if name == "goodbye" or error:
                break
    return results


def summary(model_id: str, rounds: list[list[dict]]) -> dict:
    scenes = [s for r in rounds for s in r]
    first_ttfts = [s["ttft"][0] for s in scenes if s["ttft"]]
    words = [len(t.split()) for s in scenes for t in s["said"]]
    return {"model": model_id,
            "scenes_ok": f"{sum(1 for s in scenes if not s['problems'])}/{len(scenes)}",
            "first_token_median_s": round(statistics.median(first_ttfts), 2) if first_ttfts else None,
            "first_token_max_s": max(first_ttfts) if first_ttfts else None,
            "words_median": statistics.median(words) if words else None,
            "tokens_in": sum(s["tokens_in"] for s in scenes), "tokens_out": sum(s["tokens_out"] for s in scenes),
            "problems": sorted({p for s in scenes for p in s["problems"]})}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", default=MODELS)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--out", default="workspace/bench")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for model_id in args.models:
        rounds = []
        for _ in range(args.rounds):
            with tempfile.TemporaryDirectory() as tmp:
                rounds.append(await run_once(model_id, Path(tmp)))
        report.append({"summary": summary(model_id, rounds), "rounds": rounds})
        print(json.dumps(report[-1]["summary"], ensure_ascii=False), flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"coordinator-bench-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"details: {out / f'coordinator-bench-{stamp}.json'}")


if __name__ == "__main__":
    asyncio.run(main())
