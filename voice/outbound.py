"""Lets the AI call first, after asking: Bee watcher -> planner -> offer -> the user's yes -> call.

Run next to the voice server and the token server:
    uv run python -m voice.outbound                    # watch Bee; ask when the planner says a call is worth it
    uv run python -m voice.outbound --dry-run          # watch and write plans, never ask or call
    uv run python -m voice.outbound --once INBOX_DIR   # decide for one saved Bee change (workspace/inbox/...),
                                                       # then wait until that offer is answered and called
    uv run python -m voice.outbound --direct           # call without asking (tests only)

The planner is the rule-based stand-in (beelink/planner.py) until Claude makes this
decision. Each decision is written to plan.md in the inbox folder, call or not.
A "call" decision becomes an offer (voice/offers.py): a notification asks the user, and
this process places the call when they tap "Call me now" or when the time they accepted
comes, and also for calls the user booked out loud in a call.
Before the phone rings, the coordinator's model reads Bee and writes its call prep note
(voice/prep.py), with the model the voice server uses: set COORDINATOR_LLM and
COORDINATOR_MODEL the same way for both (GLM 5 when unset).
"""

import argparse
import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from beelink.planner import COOLDOWN, MAX_AGE, RulePlanner
from beelink.watcher import BeeWatcher, Trigger
from voice import voip_push
from voice.continuation import earlier_call
from voice.dispatch import place_call
from voice.language import call_language
from voice.offers import OPEN, Offer, OfferStore, agreed, notification, plan_of
from voice.prep import NOTE_SECONDS, OPENING_BY_S, prepare_outbound

load_dotenv(".env.local")
log = logging.getLogger("bee-call.outbound")
WORKSPACE = Path("workspace")


OFFER_CHECK_S = 5.0  # how often answered and booked offers are checked (arbitrary)
BOOKED_AHEAD = timedelta(seconds=30)  # a booked call is prepared this early, so it rings on time
NOW_PREP_S = OPENING_BY_S + 2.0  # "Call me now": ring soon; the note gets this long (GLM 5 needed 4.5-7.7 s)


async def handle(trigger: Trigger, planner: RulePlanner, dry_run: bool, direct: bool = False) -> str | None:
    """Decides; a call decision asks the user (returns the offer id), or with `direct` calls at once (returns
    the room)."""
    plan = planner.decide(trigger)
    print(f"plan: {'CALL' if plan.call else 'no call'} - {plan.reason} ({plan.path})", flush=True)
    if not plan.call or dry_run:
        return None
    if direct:
        prep = await prepare_outbound(plan)
        print(f"prepared in {prep.seconds} s: " + (f"opening {prep.opening!r}" if prep.note else
                                                    f"no note ({prep.error})"), flush=True)
        room = await place_call(plan, planner.workspace, prep=prep.to_meta())
        print(f"calling: room {room}", flush=True)
        return room
    now = planner.now()
    offer = OfferStore(planner.workspace).ask(plan, now)
    await send_offer(offer, planner.workspace, now)
    return offer.id


async def send_offer(offer: Offer, workspace: Path, now: datetime) -> None:
    payload = notification(offer, now, call_language())
    try:
        sent = await voip_push.alert(payload, workspace, collapse_id=offer.id,
                                     expires_epoch=int(offer.at("expires_at").timestamp()))
    except Exception as exc:  # the offer still waits in the app's list
        print(f"asked: offer {offer.id}, but the notification failed: {exc}", flush=True)
        return
    note = f"apns-id {sent}" if sent else "no notification (no APNs key or no alert token yet); it waits in the app"
    print(f"asked: offer {offer.id}, {payload['aps']['alert']['body']!r} ({note})", flush=True)


async def call_due(workspace: Path, now: datetime | None = None, prepare=prepare_outbound, place=place_call) -> list[str]:
    """Places the calls the user said yes to: "Call me now", an accepted time that has come (prepared up to
    BOOKED_AHEAD early), or a time booked in a call. Returns the rooms."""
    store, rooms = OfferStore(workspace), []
    now = now or datetime.now()
    for offer in store.due(now + BOOKED_AHEAD):
        right_now = offer.state == "now"
        store.mark(offer, "calling", now)
        try:
            extra = {"agreed": agreed(offer)}
            if offer.continue_from:
                extra["earlier_call"] = earlier_call(workspace, offer.continue_from)
            plan = plan_of(offer)
            prep = await prepare(plan, extra=extra, deadline_s=NOW_PREP_S if right_now else NOTE_SECONDS)
            wait = (offer.at("call_at") - datetime.now()).total_seconds()
            if wait > 0:  # prepared early for a booked time
                await asyncio.sleep(wait)
            room = await place(plan, workspace, prep=prep.to_meta(), continue_from=offer.continue_from,
                               agreed=extra["agreed"])
        except Exception as exc:
            store.mark(offer, "failed", datetime.now(), error=f"{type(exc).__name__}: {exc}"[:300])
            log.exception("could not place the call for offer %s", offer.id)
            continue
        store.mark(offer, "called", datetime.now(), room=room)
        print(f"calling for offer {offer.id}: room {room}", flush=True)
        rooms.append(room)
    return rooms


async def serve_offers(workspace: Path, until: str | None = None) -> None:
    """Checks for due calls every OFFER_CHECK_S; with `until`, stops once that offer is no longer open."""
    store = OfferStore(workspace)
    while True:
        try:
            await call_due(workspace)
        except Exception:
            log.exception("checking offers failed")
        if until is not None:
            offer = store.get(until)
            if offer is None or offer.state not in OPEN:
                print(f"offer {until}: {offer.state if offer else 'gone'}", flush=True)
                return
        await asyncio.sleep(OFFER_CHECK_S)


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
    parser.add_argument("--direct", action="store_true", help="call without asking first (tests only)")
    args = parser.parse_args()
    planner = RulePlanner(WORKSPACE, cooldown=timedelta(0) if args.no_cooldown else COOLDOWN,
                          max_age=timedelta(days=3650) if args.any_age else MAX_AGE)
    if args.once:
        made = await handle(saved_trigger(args.once), planner, args.dry_run, args.direct)
        if made and not args.direct:
            await serve_offers(WORKSPACE, until=made)  # wait for the answer, then call
        return

    async def on_trigger(trigger: Trigger) -> None:
        # Errors go back to the watcher, which keeps its cursor so the same change is looked at again.
        await handle(trigger, planner, args.dry_run, args.direct)

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await asyncio.gather(BeeWatcher(WORKSPACE, on_trigger).run(),
                             *([] if args.dry_run else [serve_offers(WORKSPACE)]))
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
