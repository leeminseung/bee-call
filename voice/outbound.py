"""Lets the AI call first: Bee watcher -> planner -> call.

Run next to the voice server:
    uv run python -m voice.outbound                    # watch Bee; call when the planner says so
    uv run python -m voice.outbound --dry-run          # watch and write plans, never call
    uv run python -m voice.outbound --once INBOX_DIR   # decide for one saved Bee change (workspace/inbox/...)

The planner is the rule-based stand-in (beelink/planner.py) until Claude makes this
decision. Each decision is written to plan.md in the inbox folder, call or not.
"""

import argparse
import asyncio
import json
import logging
import signal
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from beelink.planner import COOLDOWN, MAX_AGE, RulePlanner
from beelink.watcher import BeeWatcher, Trigger
from voice.dispatch import place_call

load_dotenv(".env.local")
log = logging.getLogger("bee-call.outbound")
WORKSPACE = Path("workspace")


async def handle(trigger: Trigger, planner: RulePlanner, dry_run: bool) -> str | None:
    plan = planner.decide(trigger)
    print(f"plan: {'CALL' if plan.call else 'no call'} - {plan.reason} ({plan.path})", flush=True)
    if not plan.call or dry_run:
        return None
    room = await place_call(plan, planner.workspace)
    print(f"calling: room {room}", flush=True)
    return room


def saved_trigger(inbox: Path) -> Trigger:
    changed = json.loads((inbox / "changed.json").read_text())
    ids = [c["id"] for c in changed.get("conversations", []) if "id" in c]
    counts = {k: len(v) for k, v in changed.items() if isinstance(v, list)}
    return Trigger(reason="saved", inbox_dir=inbox, conversation_ids=ids, counts=counts)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write plans but never call")
    parser.add_argument("--once", type=Path, help="decide for one saved inbox folder, then exit")
    parser.add_argument("--no-cooldown", action="store_true", help="ignore the gap between calls (testing)")
    parser.add_argument("--any-age", action="store_true", help="also call about old conversations (testing)")
    args = parser.parse_args()
    planner = RulePlanner(WORKSPACE, cooldown=timedelta(0) if args.no_cooldown else COOLDOWN,
                          max_age=timedelta(days=3650) if args.any_age else MAX_AGE)
    if args.once:
        await handle(saved_trigger(args.once), planner, args.dry_run)
        return

    async def on_trigger(trigger: Trigger) -> None:
        # Errors go back to the watcher, which keeps its cursor so the same change is looked at again.
        await handle(trigger, planner, args.dry_run)

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await BeeWatcher(WORKSPACE, on_trigger).run()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
