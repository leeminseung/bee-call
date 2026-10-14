"""About how much one person's use cost today, for the daily budget (voice/people.py daily_budget_usd).

It is an estimate from what the processes write down, not AWS's bill:
  <date>/<time>/usage.json         a call: minutes, and the models LiveKit saw it use (voice/agent.py, at the end)
  <date>/<time>/events.jsonl       the call prep's model use (the call_prep event)
  <date>/<time>/jobs/*/usage.json  a background job's model tokens and its web searches (worker/harness.py)
  meta/session.jsonl               each meta agent think's model tokens (meta/agent.py)
A call still going on has no usage.json yet; it counts by its minutes so far at CALL_USD_PER_MINUTE.

Prices in USD, checked 2026-10-14:
  Amazon Transcribe streaming: 0.0001667 a second (AWS price file, published 2026-09-11)
  Amazon Polly, Sydney: neural 16 and standard 4 per million characters (AWS price file); generative 30
  (us-east-1's published price; Sydney's file does not list it)
  GLM 5 on Bedrock: 1.00 per million input tokens, 3.20 per million output (third-party price lists; AWS's model
  card points to the Bedrock pricing page without the figures)
  Nova Web Grounding: 0.03 a search (AWS, checked 2026-10-12)
ElevenLabs is paid with prepaid credits, so it adds nothing here.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

STT_USD_PER_SECOND = 0.0001667
POLLY_USD_PER_CHAR = {"standard": 4 / 1_000_000, "neural": 16 / 1_000_000, "generative": 30 / 1_000_000}
LLM_USD_PER_TOKEN = {  # (input, output)
    "zai.glm-5": (1.00 / 1_000_000, 3.20 / 1_000_000),
}
# A model not in the table is priced like this: Claude Sonnet 5.5's list price, above every stand-in we use.
UNKNOWN_LLM_USD_PER_TOKEN = (3.00 / 1_000_000, 15.00 / 1_000_000)
WEB_SEARCH_USD = 0.03
# A call without its usage.json yet (arbitrary until calls are measured: speech recognition alone is 0.01 a minute,
# and GLM 5 reads the whole conversation again at every turn).
CALL_USD_PER_MINUTE = 0.10


def llm_cost(model: str | None, input_tokens: Any, output_tokens: Any) -> float:
    price_in, price_out = LLM_USD_PER_TOKEN.get(model or "", UNKNOWN_LLM_USD_PER_TOKEN)
    return price_in * _number(input_tokens) + price_out * _number(output_tokens)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def call_cost(usage: dict[str, Any]) -> float:
    """A call's usage.json: {"minutes", "models": [LiveKit's model usage entries, model_dump()]}. The providers
    are named as LiveKit's plugins name them ("Amazon Transcribe", "Amazon Polly", "AWS Bedrock", "ElevenLabs")."""
    total = 0.0
    for entry in usage.get("models") or []:
        if not isinstance(entry, dict):
            continue
        kind, provider = entry.get("type"), str(entry.get("provider", "")).lower()
        if kind == "llm_usage":
            total += llm_cost(entry.get("model"), entry.get("input_tokens"), entry.get("output_tokens"))
        elif kind == "stt_usage" and "transcribe" in provider:
            total += STT_USD_PER_SECOND * _number(entry.get("audio_duration"))
        elif kind == "tts_usage" and "polly" in provider:
            per_char = POLLY_USD_PER_CHAR.get(str(entry.get("model", "")).lower(), POLLY_USD_PER_CHAR["generative"])
            total += per_char * _number(entry.get("characters_count"))
    return total


def _call_events(folder: Path) -> tuple[float, float]:
    """(minutes so far, the call prep's cost) from the call's events.jsonl."""
    last, prep = 0.0, 0.0
    try:
        with (folder / "events.jsonl").open("rb") as f:
            for raw in f:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                last = max(last, _number(event.get("t")))
                if event.get("type") == "call_prep":
                    prep += llm_cost(event.get("model"), event.get("input_tokens"), event.get("output_tokens"))
    except OSError:
        pass
    return last / 60, prep


def job_cost(usage: dict[str, Any]) -> float:
    # The worker's usage.json does not name its model; it is WORKER_MODEL.
    model = usage.get("model") or os.environ.get("WORKER_MODEL", "zai.glm-5")
    searches = sum(1 for step in usage.get("steps") or [] if isinstance(step, dict)
                   for call in step.get("calls") or [] if isinstance(call, dict) and call.get("name") == "web_search")
    return llm_cost(model, usage.get("input_tokens"), usage.get("output_tokens")) + WEB_SEARCH_USD * searches


def spent_today(workspace: Path, now: datetime) -> float:
    total = 0.0
    day = workspace / now.strftime("%Y-%m-%d")
    if day.is_dir():
        for call in day.iterdir():
            if not call.is_dir():
                continue
            minutes, prep = _call_events(call)
            usage = _json(call / "usage.json")
            total += prep + (call_cost(usage) if isinstance(usage, dict) else CALL_USD_PER_MINUTE * minutes)
            for job in (call / "jobs").glob("*/usage.json"):
                used = _json(job)
                if isinstance(used, dict):
                    total += job_cost(used)
    try:
        lines = (workspace / "meta" / "session.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    today = now.strftime("%Y-%m-%d")
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and str(entry.get("at", "")).startswith(today) and isinstance(entry.get("usage"), dict):
            total += llm_cost(entry.get("model"), entry["usage"].get("input_tokens"), entry["usage"].get("output_tokens"))
    return total
