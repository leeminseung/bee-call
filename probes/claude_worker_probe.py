"""One background job on the Claude worker (worker/claude.py) against the real API, in Korean by default.

It runs the worker role's default choice from the model catalog (Claude Sonnet 5.5, thinking adaptive at effort
high) with Anthropic's web search, web fetch and code execution and the job's own tools, through the real JobRunner,
in a scratch call folder. As the job runs it prints each progress line, what it relays and how it ends; then the time
it took, its cost as the day's budget counts it (voice/costs.py job_cost, from the job's usage.json: tokens, cache
reads and writes, web searches), the requests it made, the files it left with their sizes, and its summary. Nothing
touches Bee, AWS, LiveKit or a person's folder.

The key comes from ANTHROPIC_API_KEY or --env-file and is never printed (its length is).

Run:  uv run python probes/claude_worker_probe.py [--task "..."] [--title "..."] [--language ko|en] [--keep DIR]
                                                 [--env-file .env.local]
Cost: one research job, about 0.3 to 1 USD (web searches, and the conversation read from the cache at each request).
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SEOUL = ZoneInfo("Asia/Seoul")
TASK = ("지금 한국 애플 공식 홈페이지 기준으로 맥북 에어와 맥북 프로 기본 모델의 가격을 비교하고, 개발자가 코딩용으로 "
        "쓰기에 어느 쪽이 나은지 추천해 줘. 가격표는 CSV 파일로, 가격 비교 차트도 하나 만들어 줘.")
TITLE = "맥북 에어·프로 가격 비교"


async def main(args: argparse.Namespace) -> None:
    from voice import costs, model_catalog
    from worker.claude import ClaudeWorker
    from worker.runner import JobRunner

    choice = model_catalog.choice("worker")
    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="claude-worker-"))
    call = root / datetime.now(SEOUL).strftime("%Y-%m-%d") / datetime.now(SEOUL).strftime("%H%M")
    call.mkdir(parents=True, exist_ok=True)
    print(f"{choice.name} ({choice.offering.model}), thinking {choice.thinking}, effort {choice.effort}, "
          f"max_tokens {choice.max_tokens}; job folder under {call}")
    started = time.monotonic()

    def at() -> str:
        return f"[{time.monotonic() - started:6.1f} s]"

    def emit(name: str, **fields) -> None:
        if name in ("job_progress", "relay"):
            print(f"{at()} {name}: {fields.get('text')}")
        elif name in ("job_done", "job_failed"):
            print(f"{at()} {name}: {json.dumps(fields, ensure_ascii=False)[:600]}")

    async def notify(news) -> None:
        print(f"{at()} the coordinator hears: {news.status} {news.summary or news.error or ''}"[:400])

    runner = JobRunner(call, emit, ClaudeWorker(choice), notify, clock=lambda: datetime.now(SEOUL))
    job = runner.start(args.task, title=args.title, language=args.language)
    await asyncio.gather(*runner.tasks.values(), return_exceptions=True)
    took = time.monotonic() - started
    usage = json.loads((job.dir / "usage.json").read_text())
    print(f"\n{job.status} after {took:.0f} s ({took / 60:.1f} min) in {usage['requests']} requests")
    print(f"tokens: input {usage['input_tokens']:,} (cache read {usage['cache_read']:,}, cache written "
          f"{usage['cache_write']:,}), output {usage['output_tokens']:,}; server tools {usage['server_tools']}"
          + (" (estimated in part)" if usage.get("estimated") else ""))
    print(f"cost as the budget counts it: {costs.job_cost(usage):.3f} USD")
    print("seconds per request:", [step["request_s"] for step in usage["steps"]])
    print("files:")
    for path in sorted(job.dir.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            print(f"  {path.name:28} {path.stat().st_size:>9,} bytes")
    if job.error:
        print(f"error: {job.error}")
    report = job.dir / "report.md"
    if report.exists():
        print("\nreport.md, first lines:\n" + "\n".join(report.read_text().splitlines()[:25]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--title", default=TITLE)
    parser.add_argument("--language", default="ko", choices=("ko", "en"))
    parser.add_argument("--keep", help="the folder to keep the job in (a new scratch folder by default)")
    parser.add_argument("--env-file", type=Path, default=REPO / ".env.local")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        key = (dotenv_values(args.env_file).get("ANTHROPIC_API_KEY") if args.env_file.exists() else None) or ""
        if not key.strip():
            raise SystemExit(f"no ANTHROPIC_API_KEY in the environment or {args.env_file}")
        os.environ["ANTHROPIC_API_KEY"] = key.strip()
    print(f"ANTHROPIC_API_KEY: {len(os.environ['ANTHROPIC_API_KEY'])} characters")
    asyncio.run(main(args))
