"""Which Bedrock model can sit in the coordinator slot while Claude is blocked?

Runs the real coordinator (its instructions and tools) in a LiveKit text session against
each model, through LiveKit's Bedrock Converse plugin: the path the voice server uses for
any model other than Claude. Nine short scenes from one call, including one where the caller
adds to a request a moment after asking, and one where the caller cuts off the job result
after its first part and asks something else. Bee search returns a canned conversation and
delegated jobs do not run, so nothing touches Bee or the worker.

The cut-off scene cuts the job result off where its second half starts. --cut-off-style picks
what the model then sees: "none" (the default, what the voice server does) is LiveKit's own
handling, which keeps only the words heard, marked interrupted; "rest" adds a note with the part
not heard; "full" keeps the whole reply and adds a note saying which part was heard, the layout
the Claude adapter must use. The last two are tests: with GLM 5 on 2026-10-13 both did worse.

For every scene it records the time to the first token of each model request, the time until
the first spoken words (a model that calls a tool without a word speaks only in its next
request), what the model said, which tools it called with what, and problems: replies too
long to say in one breath, written formatting (lists, symbols), tool syntax in speech, slow
first words, or a wrong or missing tool call.

Run:  uv run python probes/coordinator_bench.py [--rounds N] [--cut-off-style none|rest|full]
                                                  [--scenes "name,name"] [model ...]
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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from probes/ like the other probes
os.environ.setdefault("AWS_PROFILE", "bee-call")

from livekit.agents import AgentSession, llm  # noqa: E402
from livekit.plugins import aws  # noqa: E402

import beelink.cli  # noqa: E402
from voice.coordinator import BeeCoordinator, Coordinator  # noqa: E402
from voice.recorder import CallRecorder  # noqa: E402
from worker.runner import JobNotification  # noqa: E402

REGION = "ap-southeast-2"
MODELS = ["zai.glm-5", "minimax.minimax-m2.5", "global.amazon.nova-2-lite-v1:0"]
GREETING = "Hi, it's Bee Call. What's on your mind?"
CONTEXT_NOTE = ("Note from the app, call context: The user called you. Unconfirmed Bee facts to check: "
                "[fact 812] The user's team offsite is next Friday.")
BEE_HIT = [{"conversation_id": 5121, "start_time": "2026-10-11T18:30:00+09:00",
            "short_summary": "Planned a Saturday hike with Dana",
            "summary": ("Matt and Dana agreed to hike Gwanaksan on Saturday morning if it does not rain. "
                        "They meet at Seoul National University station at 8 a.m. Dana brings snacks.")}]
NOW = datetime(2026, 10, 13, 16, 40, tzinfo=ZoneInfo("Asia/Seoul"))  # a Tuesday; this Saturday is October 17
JOB_TASK = "Check if it will rain in Seoul this Saturday, October 17, and which hike is easier, Gwanaksan or Bukhansan"
# What the worker relays while it still writes the report.
JOB_RESULT = JobNotification("01", "running", JOB_TASK, summary=(
    "Saturday in Seoul looks dry, about a 10% rain chance and a high near 20°C. Gwanaksan from Seoul National "
    "University is the easier hike, about 2.5 hours round trip; Bukhansan's Baegundae route is steeper, about 4 hours."))
JOB_DONE = JobNotification("01", "completed", JOB_TASK, files=["report.md", "seoul_rain_chance.svg"])
# Where each half of the job result starts in a reply (the cut-off scene cuts there), and the details that show the
# caller heard that half.
TOPICS = {"weather": ("dry", "rain", "percent", "10%"),
          "hike": ("gwanak", "bukhan", "easier", "2.5", "two and a half", "steeper", "4 hours", "four hours")}
DETAILS = {"weather": ("dry", "10 percent", "10%", "ten percent", "20 degrees", "20°"),
           "hike": ("easier", "steeper", "2.5", "two and a half", "2 and a half", "4 hours", "four hours", "4-hour")}
CUT_OFF_STYLES = ("none", "rest", "full")
WORD = re.compile(r"\w+(?:['’]\w+)*")
# Speech that reads like data: GLM 5 once began a reply "RowIndex 0: Row {     row_idx: 0, ..." (2026-10-13).
DUMP = re.compile(r"[{}\[\]]|\b[a-z]+_[a-z]+\b|RowIndex")
# A sentence about the Busan job (it never finishes in the bench) that claims a result: GLM 5 said "Busan looks good
# too, about a 15% chance of rain" once, and "looks like the Busan forecast just finished" once (2026-10-13).
INVENTED = re.compile(r"\d+\s*(%|percent|degrees|°)|just finished|came back|\bis (done|ready|in)\b", re.I)

# (name, how the scene starts, what it should do). "say" scenes are user turns, "job" scenes
# are job notifications followed by a reply request, as the voice server does, "barge" scenes
# are two user turns 0.3 s apart, "cut" scenes cut the last reply off before a user turn, and
# "status" scenes add job 01's quiet end notification before a user turn.
SCENES = [
    ("small talk", ("say", "Hey. I just got out of a long meeting and I'm kind of tired."), None),
    ("delegate", ("say", "Can you check if it's going to rain in Seoul this Saturday, and which hike is easier, "
                         "Gwanaksan or Bukhansan?"), "delegate"),
    # The first real call (2026-10-13): the caller added "this Saturday" a moment after asking, while the
    # model was still answering the first half. The reply must survive that and the task must say Saturday.
    ("split request", ("barge", ["Can you also check if it will rain in Busan?", "This Saturday, I mean."]),
     "delegate"),
    ("Bee recall", ("say", "What did Dana and I agree on yesterday about the hike?"), "search_bee"),
    ("job result", ("job", JOB_RESULT), None),
    # The caller hears the job result up to its second half (weather or hikes), then asks something else.
    ("cut off", ("cut", "Great. Hey, what's today's date again?"), None),
    ("job status", ("status", "How's the Busan one going?"), "job_status"),
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


class BenchCoordinator(BeeCoordinator):
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
    """Stands in for the job runner: records the task, starts nothing. Job 01 has finished, later ones run."""

    def __init__(self) -> None:
        self.tasks: list[str] = []

    def start(self, task: str, title: str | None = None, language: str | None = None):
        self.tasks.append(task)
        return SimpleNamespace(job_id=f"{len(self.tasks):02d}")

    def status_text(self) -> str:
        lines = [f"Job 01 | completed after 41 s | task: {task} | files: report.md, seoul_rain_chance.svg" if n == 1
                 else f"Job {n:02d} | running for 35 s | task: {task} | latest step: Comparing two forecasts"
                 for n, task in enumerate(self.tasks, 1)]
        return "\n".join(lines) or "No background jobs in this call."


def unheard_rest(reply: str, heard: str) -> str:
    """The part of a reply after as many words as the caller heard."""
    words = list(WORD.finditer(reply))
    n = len(WORD.findall(heard))
    rest = reply[words[n - 1].end():] if 0 < n <= len(words) else reply
    return re.sub(r"^[^\w\s]+", "", rest).strip()


def mentions(texts: list[str], words) -> bool:
    return any(w in t for t in texts for w in words)


async def cut_last_reply(agent: Coordinator, style: str) -> str | None:
    """Cuts the last reply off where its second half (weather or hikes) starts, as if the caller cut in there,
    and lays it out for the model in the given style. Returns the half the caller missed."""
    ctx = agent.chat_ctx.copy()
    last = next((i for i in reversed(ctx.items) if getattr(i, "type", None) == "message"
                 and i.role == "assistant" and i.text_content), None)
    if last is None:
        return None
    full = last.text_content
    words = list(re.finditer(r"\S+", full))
    first = {topic: next((n for n, w in enumerate(words) if any(k in w.group().lower() for k in keys)), None)
             for topic, keys in TOPICS.items()}
    if None in first.values():
        return None
    missed = max(first, key=first.get)
    if first[missed] < 3:
        return None
    heard = full[:words[first[missed] - 1].end()]
    if style != "full":  # LiveKit's handling: only the words heard stay, marked interrupted
        ctx.items[ctx.items.index(last)] = last.model_copy(update={"content": [heard], "interrupted": True})
        await agent.update_chat_ctx(ctx)
    if style == "rest":
        await agent.add_note("Note from the app: the caller cut you off, so they heard your last reply only as far "
                             f'as it is written above. They did not hear the rest: "{unheard_rest(full, heard)}"')
    elif style == "full":  # the Claude adapter's wording
        await agent.add_note(f'Note from the app: the caller cut you off. They heard only this part of your reply: "{heard}"')
    return missed


async def fake_bee_search(query: str, limit: int = 3, timeout: float | None = None):
    return BEE_HIT


async def run_once(model_id: str, workspace: Path, cut_off_style: str = "none", scenes=SCENES) -> list[dict]:
    beelink.cli.search_conversations = fake_bee_search  # the coordinator calls it through the module
    model = aws.LLM(model=model_id, region=REGION)
    requests: list[dict] = []
    model.on("metrics_collected", lambda m: requests.append(
        {"ttft": m.ttft, "duration": m.duration, "in": m.prompt_tokens, "out": m.completion_tokens}))
    agent = BenchCoordinator(CallRecorder(workspace, room="bench", now=NOW), StubJobs(), workspace, clock=lambda: NOW)
    results = []
    heard: set[str] = set()  # halves of the job result the caller has heard
    missed_topic = None  # the half the caller missed when they cut the result off, until it is said
    async with AgentSession(llm=model) as session:
        await session.start(agent)
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role="assistant", content=GREETING)
        ctx.add_message(role="system", content=CONTEXT_NOTE)
        await agent.update_chat_ctx(ctx)
        for name, (kind, text), expected_tool in scenes:
            seen = len(session.history.items)
            first_request = len(requests)
            started = time.monotonic()
            error = None
            missed = None
            try:
                if kind == "cut":
                    missed = await cut_last_reply(agent, cut_off_style)
                    if missed is None:  # the job result did not name both halves; nothing to cut
                        results.append({"scene": name, "skipped": "the last reply did not name both topics",
                                        "problems": []})
                        continue
                    heard.discard(missed)
                    missed_topic = missed
                    await asyncio.wait_for(session.run(user_input=text), 60)
                elif kind == "status":
                    await agent.tell_job(JOB_DONE)  # files only: no reply is asked for
                    await asyncio.wait_for(session.run(user_input=text), 60)
                elif kind == "say":
                    await asyncio.wait_for(session.run(user_input=text), 60)
                elif kind == "barge":
                    session.generate_reply(user_input=text[0])
                    await asyncio.sleep(BARGE_GAP_S)
                    session.interrupt()  # what the caller's voice does to a reply in progress
                    started = time.monotonic()  # what counts is how long the caller waits after the second half
                    await asyncio.wait_for(session.run(user_input=text[1]), 60)
                else:
                    if await agent.tell_job(text):  # the same path the voice server uses for job news
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
            searched_before = any(t[0] == "search_bee" for s in results for t in s.get("tools", []))
            problems = []
            if error:
                problems.append("error")
            limit = MAX_WORDS_JOB_RESULT if name in ("job result", "cut off") else MAX_WORDS
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
            lowered = [s.lower() for s in said]
            observed = []
            if kind == "cut":
                if not any("13" in s or "tuesday" in s for s in lowered):
                    problems.append("ignored the question")
                if not mentions(lowered, DETAILS[missed]):
                    problems.append("missed part not said in this reply")
            elif kind != "job":
                if "hike" in heard and mentions(lowered, DETAILS["hike"]):
                    problems.append("repeated the hike result")
                if missed_topic and mentions(lowered, DETAILS[missed_topic]):
                    observed.append("missed part said here, late")
            if kind == "job":
                heard |= {topic for topic, words in DETAILS.items() if mentions(lowered, words)}
            elif missed_topic and mentions(lowered, DETAILS[missed_topic]):
                heard.add(missed_topic)
                missed_topic = None
            if any("note from the app" in s for s in lowered):
                problems.append("read the note")
            if any(DUMP.search(s) for s in said):
                problems.append("data dump in speech")
            busan_asked = any(r["scene"] == "split request" for r in results) and kind != "barge"
            if busan_asked and any(INVENTED.search(sentence) for s in lowered
                                   for sentence in re.split(r"(?<=[.!?])\s+", s) if "busan" in sentence):
                problems.append("invented a job result")
            results.append({"scene": name, "seconds": round(time.monotonic() - started, 2), "first_words": first_words,
                            "missed": missed, "observed": observed,
                            "ttft": [round(r["ttft"], 2) for r in reqs if r["ttft"] is not None and r["ttft"] >= 0],
                            "tokens_in": sum(r["in"] or 0 for r in reqs), "tokens_out": sum(r["out"] or 0 for r in reqs),
                            "said": said, "tools": tools, "problems": problems, "error": error})
            if name == "goodbye" or error:
                break
    return results


def summary(model_id: str, rounds: list[list[dict]], cut_off_style: str) -> dict:
    scenes = [s for r in rounds for s in r if not s.get("skipped")]
    cut_offs = [s for s in scenes if s["scene"] == "cut off"]
    first_ttfts = [s["ttft"][0] for s in scenes if s["ttft"]]
    first_words = [s["first_words"] for s in scenes if s.get("first_words") is not None]
    words = [len(t.split()) for s in scenes for t in s["said"]]
    def count(problem: str) -> int:
        return sum(1 for s in scenes if problem in s["problems"])

    return {"model": model_id, "cut_off_style": cut_off_style,
            "scenes_ok": f"{sum(1 for s in scenes if not s['problems'])}/{len(scenes)}",
            "cut_off_ok": f"{sum(1 for s in cut_offs if not s['problems'])}/{len(cut_offs)}",
            "missed_part_said_late": sum(1 for s in scenes if "missed part said here, late" in s.get("observed", [])),
            "repeated_hike_result": count("repeated the hike result"),
            "invented_job_result": count("invented a job result"),
            "data_dump": count("data dump in speech"),
            "skipped": sum(1 for r in rounds for s in r if s.get("skipped")),
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
    parser.add_argument("--cut-off-style", choices=CUT_OFF_STYLES, default="none")
    parser.add_argument("--scenes", help="comma-separated scene names to run, in their usual order")
    parser.add_argument("--out", default="workspace/bench")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = [n.strip() for n in args.scenes.split(",")] if args.scenes else [name for name, _, _ in SCENES]
    unknown = set(wanted) - {name for name, _, _ in SCENES}
    if unknown:
        raise SystemExit(f"unknown scenes: {', '.join(sorted(unknown))}")
    scenes = [scene for scene in SCENES if scene[0] in wanted]
    report = []
    for model_id in args.models:
        rounds = []
        for _ in range(args.rounds):
            with tempfile.TemporaryDirectory() as tmp:
                rounds.append(await run_once(model_id, Path(tmp), args.cut_off_style, scenes))
        report.append({"summary": summary(model_id, rounds, args.cut_off_style), "rounds": rounds})
        print(json.dumps(report[-1]["summary"], ensure_ascii=False), flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{args.cut_off_style}"
    (out / f"coordinator-bench-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"details: {out / f'coordinator-bench-{stamp}.json'}")


if __name__ == "__main__":
    asyncio.run(main())
