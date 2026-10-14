"""Decides whether a change in Bee is worth a call, and writes the plan for it.

The design gives this judgement to the worker (Claude): read the new Bee records
and recent calls, decide, and write plan.md. Until Claude on Bedrock is available,
a rule-based stand-in decides instead. It is a test scaffold, not the product.
Either way the result is the same file, plan.md in the watcher's inbox folder,
which the voice server copies into the call's session folder.

Rules of the stand-in:
- there is something fresh to talk about: a Bee conversation that ended within
  MAX_AGE with at least MIN_UTTERANCES utterances, or a new voice note (our own
  calls were already filtered out by the watcher);
- no call while another call is going on;
- at least COOLDOWN since the last call ended, the last AI call was placed or the
  user was last asked about one, whoever started it.

A "call" decision is a question first: voice/outbound.py asks the user with a
notification (voice/offers.py) and calls only when they say yes.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from beelink.calls import our_call_windows
from beelink.clock import parse_time
from beelink.watcher import Trigger

MIN_UTTERANCES = 4  # arbitrary: shorter exchanges are usually small talk
MAX_AGE = timedelta(hours=1)  # arbitrary: older conversations are not worth a call ("you just talked about...")
COOLDOWN = timedelta(minutes=30)  # arbitrary
# A session without call_ended whose events moved this recently is a call in progress. Long on purpose:
# a quiet caller writes no events for minutes, and a crashed call only blocks new calls for a while.
LIVE_WINDOW_S = 900
OUTBOUND_LOG = Path("state") / "outbound.jsonl"  # under the workspace; written by voice/dispatch.py
OFFERS_DIR = Path("state") / "offers"  # under the workspace; one file per call offer (voice/offers.py)


@dataclass
class Plan:
    call: bool
    reason: str
    inbox_dir: Path | None  # None for a call the user booked in a call (no Bee change behind it)
    conversation_ids: list[int] = field(default_factory=list)
    topic: str | None = None  # what the call is about, in Bee's words (its short summary)
    kind: str | None = None  # "conversation" or "voice note"
    path: Path | None = None  # plan.md


def call_in_progress(workspace: Path, now: datetime) -> bool:
    for events in sorted(workspace.glob("*/*/events.jsonl"), reverse=True)[:3]:
        if now.timestamp() - events.stat().st_mtime > LIVE_WINDOW_S:
            continue
        if '"type": "call_ended"' not in events.read_text():
            return True
    return False


def last_outbound(workspace: Path) -> datetime | None:
    log = workspace / OUTBOUND_LOG
    if not log.exists():
        return None
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    return parse_time(json.loads(lines[-1])["at"]) if lines else None


def last_offer(workspace: Path) -> datetime | None:
    """When the user was last asked about a call (voice/offers.py)."""
    asked = []
    for path in (workspace / OFFERS_DIR).glob("*.json"):
        try:
            history = json.loads(path.read_text()).get("history") or []
            asked += [parse_time(h["at"]) for h in history if h.get("state") == "asked"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return max(asked) if asked else None


def last_call(workspace: Path) -> datetime | None:
    """The latest of: the last AI call placed, the end of the last call recorded (either direction), and the last
    time the user was asked about a call."""
    times = [w_end for _, w_end in our_call_windows(workspace)]
    for moment in (last_outbound(workspace), last_offer(workspace)):
        if moment is not None:
            times.append(moment.timestamp())
    return datetime.fromtimestamp(max(times)).astimezone() if times else None


def journal_text(journal: dict[str, Any]) -> str:
    return str(journal.get("text") or journal.get("transcript") or journal.get("content") or "").strip()


class RulePlanner:
    """The rule-based stand-in for the worker's decision."""

    def __init__(self, workspace: Path, cooldown: timedelta = COOLDOWN, max_age: timedelta = MAX_AGE, *,
                 now: Callable[[], datetime]) -> None:
        """`now`: the person's clock (voice/people.py Person.now)."""
        self.workspace = workspace
        self.cooldown = cooldown
        self.max_age = max_age
        self.now = now

    def decide(self, trigger: Trigger) -> Plan:
        changed = json.loads((trigger.inbox_dir / "changed.json").read_text())
        wanted = set(trigger.conversation_ids)
        now = self.now()
        fresh_after = (now - self.max_age).timestamp() * 1000
        talks = [c for c in changed.get("conversations", [])
                 if c.get("id") in wanted and c.get("state", "COMPLETED") == "COMPLETED"
                 and (c.get("utterances_count") or 0) >= MIN_UTTERANCES
                 and (c.get("end_time") or c.get("start_time") or 0) >= fresh_after]
        notes = [j for j in changed.get("journals", []) if journal_text(j)
                 and (j.get("created_at") or j.get("start_time") or fresh_after) >= fresh_after]
        last = last_call(self.workspace)
        plan = Plan(call=False, reason="", inbox_dir=trigger.inbox_dir)
        if call_in_progress(self.workspace, now):
            plan.reason = "A call is already in progress."
        elif last is not None and now - last < self.cooldown:
            plan.reason = (f"The last call was at {last.astimezone(now.tzinfo):%H:%M}; calls are at least "
                           f"{int(self.cooldown.total_seconds() // 60)} minutes apart.")
        elif talks:
            latest = max(talks, key=lambda c: c.get("end_time") or 0)
            ended = datetime.fromtimestamp((latest.get("end_time") or latest.get("start_time")) / 1000, now.tzinfo)
            plan.call, plan.kind = True, "conversation"
            plan.conversation_ids = [c["id"] for c in talks]
            plan.topic = latest.get("short_summary") or None
            plan.reason = (f"A Bee conversation ended at {ended:%H:%M} ({latest.get('utterances_count')} utterances)"
                           + (f": {plan.topic}" if plan.topic else "."))
        elif notes:
            plan.call, plan.kind = True, "voice note"
            plan.reason = f'A new Bee voice note: "{journal_text(notes[-1])[:120]}"'
        else:
            plan.reason = (f"Nothing fresh to talk about: no conversation with {MIN_UTTERANCES}+ utterances that ended "
                           f"in the last {int(self.max_age.total_seconds() // 60)} minutes, and no new voice note.")
        plan.path = self.write(plan, trigger, now)
        return plan

    def write(self, plan: Plan, trigger: Trigger, now: datetime) -> Path:
        lines = [
            "# Call plan",
            "",
            "Decided by the rule-based stand-in, not by Claude.",
            "",
            f"- Decision: {'call' if plan.call else 'no call'}",
            f"- Why: {plan.reason}",
            f"- Topic: {plan.topic or '-'}",
            f"- Bee conversations: {', '.join(map(str, plan.conversation_ids)) or '-'}",
            f"- Bee change: {trigger.reason}, counts {json.dumps(trigger.counts)}",
            f"- Decided at: {now.isoformat(timespec='seconds')}",
            "",
        ]
        path = trigger.inbox_dir / "plan.md"
        path.write_text("\n".join(lines))
        return path
