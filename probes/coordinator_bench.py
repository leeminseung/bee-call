"""Which Bedrock model can sit in the coordinator slot while Claude is blocked?

Runs the real coordinator (its instructions and tools) in a LiveKit text session against
each model, through LiveKit's Bedrock Converse plugin: the path the voice server uses for
any model other than Claude. Seven short scenes from one call, including one where the caller
adds to a request a moment after asking. Bee search returns a canned conversation and
delegated jobs do not run, so nothing touches Bee or the worker.

For every scene it records the time to the first token of each model request, the time until
the first spoken words (a model that calls a tool without a word speaks only in its next
request), what the model said, which tools it called with what, and problems: replies too
long to say in one breath, written formatting (lists, symbols), tool syntax in speech, slow
first words, or a wrong or missing tool call.

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
MODELS = ["zai.glm-5", "minimax.minimax-m2.5", "global.amazon.nova-2-lite-v1:0"]
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
# are app notes followed by a reply request, as the voice server does for finished jobs, and
# "barge" scenes are two user turns 0.3 s apart.
SCENES = [
    ("small talk", ("say", "Hey. I just got out of a long meeting and I'm kind of tired."), None),
    ("delegate", ("say", "Can you check if it's going to rain in Seoul this Saturday, and which hike is easier, "
                         "Gwanaksan or Bukhansan?"), "delegate"),
    # The first real call (2026-10-13): the caller added "this Saturday" a moment after asking, while the
    # model was still answering the first half. The reply must survive that and the task must say Saturday.
    ("split request", ("barge", ["Can you also check if it will rain in Busan?", "This Saturday, I mean."]),
     "delegate"),
    ("Bee recall", ("say", "What did Dana and I agree on yesterday about the hike?"), "search_bee"),
    ("job result", ("note", JOB_NOTE), None),
    ("confirm fact", ("say", "Oh, and yes, the team offsite is next Friday. That one's right."), "queue_bee_write"),
    ("goodbye", ("say", "Great, that's all for now. Thanks!"), "end_call"),
]
MAX_WORDS = 40  # about one breath of speech (arbitrary); a job result may take two or three sentences
MAX_WORDS_JOB_RESULT = 60
SLOW_FIRST_WORDS_S = 3.0  # first spoken words later than this feel like a dead line (arbitrary)
BARGE_GAP_S = 0.3
WRITTEN = re.compile(r"(^\s*[-*•]\s|^\s*\d+\.\s|\*\*|#|\||`|https?://|[\U0001F300-\U0001FAFF])", re.M)
# Letters outside English text (a GLM 5 reply once began "erratмl", 2026-10-13).
GARBLED = re.compile(r"[^\x00-\x7F‘’“”–—…°]")
# Tool-call syntax leaking into the reply text; the voice would read it out.
MARKUP = re.compile(r"(<\s*[|｜/]?\s*[A-Za-z_｜]|function_call|tool_call|DSML)")


class BenchCoordinator(Coordinator):
    """The real coordinator, noting when each reply's first spoken words come out of the model."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.spoken_at: list[float] = []

    async def llm_node(self, chat_ctx, tools, model_settings):
        async for chunk in super().llm_node(chat_ctx, tools, model_settings):
            text = chunk if isinstance(chunk, str) else (
                chunk.delta.content if isinstance(chunk, llm.ChatChunk) and chunk.delta else None)
            if text and text.strip():
                self.spoken_at.append(time.monotonic())
            yield chunk


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
    agent = BenchCoordinator(CallRecorder(workspace, room="bench"), StubJobs(), workspace, notes_as_user=True)
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
                elif kind == "barge":
                    session.generate_reply(user_input=text[0])
                    await asyncio.sleep(BARGE_GAP_S)
                    session.interrupt()  # what the caller's voice does to a reply in progress
                    started = time.monotonic()  # what counts is how long the caller waits after the second half
                    await asyncio.wait_for(session.run(user_input=text[1]), 60)
                else:
                    await agent.add_note(text)  # the same path the voice server uses for finished jobs
                    await asyncio.wait_for(session.generate_reply(), 60)
            except Exception as exc:  # a broken model or plugin path is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"[:200]
            new = session.history.items[seen:]
            said = [i.text_content.strip() for i in new
                    if getattr(i, "role", None) == "assistant" and i.text_content and i.text_content.strip()]
            spoken = [s for s in agent.spoken_at if s >= started]
            first_words = round(spoken[0] - started, 2) if spoken else None
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
            if any(GARBLED.search(s) for s in said):
                problems.append("garbled text")
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
            if first_words is not None and first_words > SLOW_FIRST_WORDS_S:
                problems.append("slow first words")
            if kind == "barge" and "delegate" in names:
                task = json.loads(dict(tools)["delegate"] or "{}").get("task", "").lower()
                if not any(w in task for w in ("saturday", "october 17", "oct 17", "10/17")):
                    problems.append("task lost the second half")
            if not said and name != "goodbye":
                problems.append("said nothing")
            results.append({"scene": name, "seconds": round(time.monotonic() - started, 2), "first_words": first_words,
                            "ttft": [round(r["ttft"], 2) for r in reqs if r["ttft"] is not None and r["ttft"] >= 0],
                            "tokens_in": sum(r["in"] or 0 for r in reqs), "tokens_out": sum(r["out"] or 0 for r in reqs),
                            "said": said, "tools": tools, "problems": problems, "error": error})
            if name == "goodbye" or error:
                break
    return results


def summary(model_id: str, rounds: list[list[dict]]) -> dict:
    scenes = [s for r in rounds for s in r]
    first_ttfts = [s["ttft"][0] for s in scenes if s["ttft"]]
    first_words = [s["first_words"] for s in scenes if s.get("first_words") is not None]
    words = [len(t.split()) for s in scenes for t in s["said"]]
    return {"model": model_id,
            "scenes_ok": f"{sum(1 for s in scenes if not s['problems'])}/{len(scenes)}",
            "first_token_median_s": round(statistics.median(first_ttfts), 2) if first_ttfts else None,
            "first_token_max_s": max(first_ttfts) if first_ttfts else None,
            "first_words_median_s": round(statistics.median(first_words), 2) if first_words else None,
            "first_words_max_s": max(first_words) if first_words else None,
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
