"""About how much one person's use cost today, and the daily budget check (voice/people.py daily_budget_usd).

day_budget() is the one budget check. Every path that starts spending checks it, with no way to skip it: the
token server before a call the user starts, voice/outbound.py's call_due before an agreed call, the meta loop
before the meta agent thinks, and the voice server when a call the user started begins. A spend equal to the
budget counts as over (Matt, 2026-10-15). It is checked only before a call starts; a call in progress is never cut.

It is an estimate from what the processes write down, not AWS's bill:
  <date>/<time>/usage.json         a call: minutes, and the models LiveKit saw it use (voice/agent.py, at the end)
  <date>/<time>/jobs/*/usage.json  a background job's model tokens and its web searches, after every round
                                   (worker/harness.py); a round still running when the call ends counts at once
                                   as a guess ("estimated") and as what it used once it ends
  meta/session.jsonl               each meta agent think's model tokens (meta/agent.py)
  state/spend.jsonl                use recorded where it happens, outside a call's own record (SpendLog): every
                                   call prep (voice/prep.py, in voice/outbound.py before the AI's call rings and in
                                   the voice server while the user's call rings), also for calls nobody answered
                                   and calls hung up while they rang, every worker warm-up, and each call's
                                   title written after it (voice/titles.py); and what a call left before the AI
                                   spoke used, since its folder goes (voice/agent.py)
A call still going on has no usage.json yet; it counts by its minutes so far at CALL_USD_PER_MINUTE. A call's
call_prep event only shows the prep on the call's record; its cost is in state/spend.jsonl.

Prices in USD, checked 2026-10-14: the models' prices are in the model catalog (voice/model_catalog.py); here
  Amazon Transcribe streaming: 0.0001667 a second (AWS price file, published 2026-09-11)
  Amazon Polly, Sydney: neural 16 and standard 4 per million characters (AWS price file); generative 30
  (us-east-1's published price; Sydney's file does not list it)
  Nova Web Grounding: 0.03 a search (AWS, checked 2026-10-12)
ElevenLabs is paid with prepaid credits, so it adds nothing here.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from beelink.clock import parse_time
from voice.call_folders import EVENTS, read_events
from voice.model_catalog import price
from voice.people import Person

STT_USD_PER_SECOND = 0.0001667
POLLY_USD_PER_CHAR = {"standard": 4 / 1_000_000, "neural": 16 / 1_000_000, "generative": 30 / 1_000_000}
WEB_SEARCH_USD = 0.03
# A call without its usage.json yet (arbitrary until calls are measured: speech recognition alone is 0.01 a minute,
# and GLM 5 reads the whole conversation again at every turn).
CALL_USD_PER_MINUTE = 0.10


def llm_cost(model: str | None, input_tokens: Any, output_tokens: Any, cached: Any = 0, cache_writes: Any = 0) -> float:
    """A model's tokens at the model catalog's prices, by the provider's model id (voice/model_catalog.py; a model it
    does not list costs more than any it does). `input_tokens` counts every prompt token, `cached` (cache reads) and
    `cache_writes` among them."""
    offer = price(model)
    cached, cache_writes = _number(cached), _number(cache_writes)
    fresh = max(0.0, _number(input_tokens) - cached - cache_writes)
    prompt = fresh + offer.cache_read_share * cached + offer.cache_write_share * cache_writes
    return offer.usd_in * prompt + offer.usd_out * _number(output_tokens)


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
            total += llm_cost(entry.get("model"), entry.get("input_tokens"), entry.get("output_tokens"),
                              entry.get("input_cached_tokens"), entry.get("input_cache_creation_tokens"))
        elif kind == "stt_usage" and "transcribe" in provider:
            total += STT_USD_PER_SECOND * _number(entry.get("audio_duration"))
        elif kind == "tts_usage" and "polly" in provider:
            per_char = POLLY_USD_PER_CHAR.get(str(entry.get("model", "")).lower(), POLLY_USD_PER_CHAR["generative"])
            total += per_char * _number(entry.get("characters_count"))
    return total


def _call_minutes(folder: Path) -> float:
    """How long the call has gone on, from the call's events.jsonl."""
    return max((_number(event.get("t")) for event in read_events(folder / EVENTS)), default=0.0) / 60


def job_cost(usage: dict[str, Any]) -> float:
    # The worker's usage.json does not name its model; it is WORKER_MODEL.
    model = usage.get("model") or os.environ.get("WORKER_MODEL", "zai.glm-5")
    searches = sum(1 for step in usage.get("steps") or [] if isinstance(step, dict)
                   for call in step.get("calls") or [] if isinstance(call, dict) and call.get("name") == "web_search")
    return llm_cost(model, usage.get("input_tokens"), usage.get("output_tokens")) + WEB_SEARCH_USD * searches


def on_day(at: Any, day: datetime) -> bool:
    """Whether a stored time falls on the person's day (`day` is their time, so their time zone decides)."""
    try:
        return parse_time(str(at)).astimezone(day.tzinfo).date() == day.date()
    except ValueError:
        return False


def spent_today(workspace: Path, now: datetime) -> float:
    """What the person's day has cost so far; `now` is their time (Person.now()), so the day is theirs: their call
    folders are named by their own dates."""
    total = 0.0
    day = workspace / now.strftime("%Y-%m-%d")
    if day.is_dir():
        for call in day.iterdir():
            if not call.is_dir():
                continue
            usage = _json(call / "usage.json")
            total += call_cost(usage) if isinstance(usage, dict) else CALL_USD_PER_MINUTE * _call_minutes(call)
            for job in (call / "jobs").glob("*/usage.json"):
                used = _json(job)
                if isinstance(used, dict):
                    total += job_cost(used)
    try:
        lines = (workspace / "meta" / "session.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("usage"), dict) and on_day(entry.get("at"), now):
            total += llm_cost(entry.get("model"), entry["usage"].get("input_tokens"), entry["usage"].get("output_tokens"))
    return total + SpendLog(workspace).spent_on(now)


SPEND_LOG = Path("state") / "spend.jsonl"  # under the workspace


class SpendLog:
    """state/spend.jsonl: model use recorded where it happens, when it is not in a call's own files. Appending one
    short line is atomic, so the processes that write it need no lock."""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / SPEND_LOG

    def add(self, what: str, *, model: str | None = None, input_tokens: int = 0, output_tokens: int = 0,
            models: list[dict[str, Any]] | None = None, estimated: bool = False, now: datetime | None = None,
            **about: Any) -> None:
        """One use: an LLM's tokens (model, input_tokens, output_tokens), a session's LiveKit usage entries
        (models), or a background job's usage.json (job=...). `estimated`: counted from what was sent, because the
        provider's own count never came."""
        # The moment, with its zone; spent_on() places it on the person's own day when it reads it.
        entry = {"at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"), "what": what, "model": model,
                 "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated": estimated, **about}
        if models is not None:
            entry["models"] = models
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def spent_on(self, day: datetime) -> float:
        try:
            lines = self.path.read_text(errors="replace").splitlines()
        except OSError:
            return 0.0
        total = 0.0
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not on_day(entry.get("at"), day):
                continue
            total += llm_cost(entry.get("model"), entry.get("input_tokens"), entry.get("output_tokens"))
            if isinstance(entry.get("models"), list):
                total += call_cost({"models": entry["models"]})
            if isinstance(entry.get("job"), dict):  # a background job's usage.json whose call folder went
                total += job_cost(entry["job"])
        return total


@dataclass(frozen=True)
class DayBudget:
    spent: float  # USD today, an estimate (spent_today)
    limit: float  # the person's daily_budget_usd

    @property
    def used_up(self) -> bool:
        return self.spent >= self.limit  # equal counts as over

    def why(self) -> str:
        return f"today's budget of {self.limit:.2f} USD is used up (about {self.spent:.2f} USD so far)"


def day_budget(person: Person, now: datetime) -> DayBudget:
    """The one budget check: what the person spent today against their daily budget."""
    return DayBudget(spent_today(person.workspace, now), person.settings().daily_budget_usd)
