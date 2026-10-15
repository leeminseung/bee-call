"""Claude on Anthropic's own API in the coordinator slot, the way a real call runs it.

Runs the real call AI (BeeCoordinator: its instructions, the Korean rule, a call prep note, every tool) in a LiveKit
text session, with the voice server's own model setup for COORDINATOR_LLM=anthropic (voice/agent.py
call_ai_with_standby): the person's call model (--call-model haiku or sonnet, voice/call_models.py) with
its thinking and effort, caching on. The caller's turns are the
caller's lines of a call's transcript.md (--transcript; lines less than a second apart are one turn, as LiveKit hears
them), or a few built-in Korean ones. Bee answers from the test fake (tests/fakebee), background jobs do not run, and
GLM 5 is not called: where it would take the call, a stand-in says so and the reason is printed. Nothing touches Bee,
AWS or the worker.

Per turn it prints the time to Claude's first words (the adapter's measure, from sending the request to the first
text), the input tokens, how many were read from the cache and written to it, the tools called and what was said.
Errors are printed with their status and type: a 400 means the request's shape is wrong.

The key comes from ANTHROPIC_API_KEY or --env-file and is never printed (its length is).

Run:  uv run python probes/haiku_probe.py [--call-model haiku|sonnet] [--thinking T] [--effort E]
                                         [--transcript PATH] [--turns 6] [--rounds 2] [--language ko|en]
                                         [--env-file .env.local]
      --thinking and --effort try another setting than the call model's own.
Cost: a round of six turns is about 0.1 US cent with Haiku and 2 cents with Sonnet (requests of 4-5K input tokens,
most of them read from the cache).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SEOUL = ZoneInfo("Asia/Seoul")
TURNS = ["네, 안녕하세요.", "어", "오늘은 집에서 대회 준비를 좀 했어요. 해커톤 제출이 다음 주라서요.",
         "내일 오후 세 시에 다시 전화해 줄 수 있어요?", "아, 그리고 지난주에 친구랑 했던 이야기 기억나요?",
         "좋아요. 그럼 오늘은 여기까지 할게요."]
PREP_NOTE = """\
- Today the user stayed home and worked on a hackathon entry due next week; no conversations with other people.
- Earlier this week they talked with a friend about weekend hiking plans (Bee conversation, not yet confirmed).
- One unconfirmed fact: "Prefers calls in the late afternoon."
- Open todo suggestion: "Send the hackathon demo video to the team."
Opening already said: "네, Bee Call이에요. 어떤 이야기 해 볼까요?"
"""
OPENING_KO, OPENING_EN = "네, Bee Call이에요. 어떤 이야기 해 볼까요?", "Hi, it's Bee Call. What shall we talk about?"
LINE = re.compile(r"^- (\d+):(\d+(?:\.\d+)?) \*\*User:\*\* (.*)$")


def caller_turns(path: Path, limit: int) -> list[str]:
    """The caller's lines of a transcript.md, joined into turns where they came less than a second apart."""
    turns: list[tuple[float, str]] = []
    for line in path.read_text().splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        at, words = int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()
        if turns and at - turns[-1][0] < 1.0:
            turns[-1] = (at, f"{turns[-1][1]} {words}")
        else:
            turns.append((at, words))
    return [words for _, words in turns][:limit]


class StubJobs:
    """Background jobs do not run in the probe."""

    def start(self, task: str, title: str | None = None, language: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(job_id="1")

    def status_text(self) -> str:
        return "No background jobs in this call."


def stand_in_glm():
    """Where GLM 5 would take the call: no Bedrock in the probe."""
    from tests.helpers import ScriptedLLM

    return ScriptedLLM([["(GLM 5 would answer here)"]] * 20, model="glm-5-not-called")


async def one_round(turns: list[str], workspace: Path, round_no: int, call_model, language_code: str = "ko"
                    ) -> dict[str, Any]:
    from livekit.agents import AgentSession

    from voice import agent as voice_agent
    from voice.coordinator import BeeCoordinator
    from voice.language import call_language
    from voice.recorder import CallRecorder

    voice_agent.COORDINATOR_LLM = "anthropic"
    voice_agent.aws.LLM = lambda **_: stand_in_glm()  # never Bedrock from the probe
    now = datetime.now(SEOUL)
    rec = CallRecorder(workspace, room=f"probe-{round_no}", now=now)
    language = call_language(language_code)
    model = voice_agent.make_llm(rec, language, call_model)
    prep_note = PREP_NOTE if language_code == "ko" else PREP_NOTE.replace(OPENING_KO, OPENING_EN)
    coordinator = BeeCoordinator(rec, StubJobs(), workspace, clock=lambda: datetime.now(SEOUL), prep_note=prep_note,
                                 language=language)
    metrics: list[Any] = []
    rows = []
    async with AgentSession(llm=model) as session:
        voice_agent.wire_events(session, rec)
        session.on("metrics_collected", lambda ev: metrics.append(ev.metrics))
        await session.start(coordinator)
        for words in turns:
            before = len(session.history.items)
            started = time.perf_counter()
            error = None
            try:
                await session.run(user_input=words)
            except Exception as exc:  # a failed turn: shown, and the round goes on
                error = f"{type(exc).__name__}: {exc}"[:300]
            items = session.history.items[before:]
            rows.append({
                "caller": words, "seconds": round(time.perf_counter() - started, 2), "error": error,
                "said": " / ".join(i.text_content or "" for i in items if getattr(i, "role", None) == "assistant"),
                "tools": [f"{i.name}({i.arguments})" for i in items if getattr(i, "type", None) == "function_call"],
            })
    rec.close()
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    return {"rows": rows, "events": events, "metrics": [m.model_dump() for m in metrics if m.type == "llm_metrics"],
            "folder": str(rec.dir)}


def report(result: dict[str, Any], round_no: int) -> None:
    events = result["events"]
    for choice in (e for e in events if e["type"] == "coordinator_model"):
        print(f"round {round_no}: the call's model is {choice['model']}"
              + (f" instead of {choice.get('instead_of')}: {choice.get('reason')}" if choice.get("reason") else ""))
    turns = [e for e in events if e["type"] == "claude_turn"]
    print(f"{'request':>7} {'first words s':>13} {'input':>6} {'cached':>6} {'written':>7} {'output':>6}  stop")
    llm_metrics = result["metrics"]
    for n, turn in enumerate(turns):
        written = llm_metrics[n]["cache_creation_tokens"] if n < len(llm_metrics) else None
        first = turn.get("first_text_s")
        print(f"{n + 1:>7} {'-' if first is None else round(first, 2)!s:>13} {turn.get('input_tokens')!s:>6} "
              f"{turn.get('cache_read_tokens')!s:>6} {written!s:>7} {turn.get('output_tokens')!s:>6}  "
              f"{turn.get('stop')} {turn.get('status')}")
    for row in result["rows"]:
        print(f"- caller: {row['caller']}\n  AI ({row['seconds']} s): {row['said']}"
              + (f"\n  tools: {', '.join(row['tools'])}" if row["tools"] else "")
              + (f"\n  ERROR: {row['error']}" if row["error"] else ""))
    for e in events:
        if e["type"] in ("error", "claude_refusal", "claude_reply_empty", "claude_reply_declined"):
            print(f"! {e['type']}: {json.dumps({k: v for k, v in e.items() if k not in ('t', 'type')}, ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--call-model", default="haiku", help="the person's setting: haiku or sonnet")
    parser.add_argument("--thinking", help="another thinking type than the call model's (disabled, adaptive, ...)")
    parser.add_argument("--effort", help="another effort than the call model's (low, medium, high)")
    parser.add_argument("--transcript", type=Path, help="a call's transcript.md: its caller lines are the turns")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=1, help="calls one after another (the second reads the cache)")
    parser.add_argument("--env-file", type=Path, default=REPO / ".env.local")
    parser.add_argument("--out", type=Path, help="where to keep the rounds' records (a new temporary folder)")
    parser.add_argument("--language", default="ko", choices=("ko", "en"), help="the call's language")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        key = (dotenv_values(args.env_file).get("ANTHROPIC_API_KEY") if args.env_file.exists() else None) or ""
        if not key.strip():
            raise SystemExit(f"no ANTHROPIC_API_KEY in the environment or {args.env_file}")
        os.environ["ANTHROPIC_API_KEY"] = key.strip()
    print(f"ANTHROPIC_API_KEY: {len(os.environ['ANTHROPIC_API_KEY'])} characters")
    # Bee answers from the test fake, never from a real account.
    out = args.out or Path(tempfile.mkdtemp(prefix="haiku-probe-"))
    os.environ["BEE_BIN"] = str(REPO / "tests" / "fakebee" / "bee")
    os.environ["FAKE_BEE_LOG"] = str(out / "fake-bee-calls.jsonl")
    from beelink import cli
    cli.use_machine_bee()

    import dataclasses

    from voice.call_models import CALL_MODELS
    call_model = CALL_MODELS[args.call_model]
    call_model = dataclasses.replace(call_model, thinking=args.thinking or call_model.thinking,
                                     effort=args.effort or call_model.effort)
    print(f"call model: {call_model.model}, thinking {call_model.thinking}, effort {call_model.effort}")
    turns = caller_turns(args.transcript, args.turns) if args.transcript else TURNS[:args.turns]
    print(f"{len(turns)} caller turns" + (f" from {args.transcript}" if args.transcript else " (built in)"))
    for round_no in range(1, args.rounds + 1):
        result = asyncio.run(one_round(turns, out / f"round-{round_no}", round_no, call_model, args.language))
        (out / f"round-{round_no}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        report(result, round_no)
    print(f"records: {out}")


if __name__ == "__main__":
    main()
