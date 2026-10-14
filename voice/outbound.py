"""Bee Call between calls: Bee watcher -> meta agent -> offer -> the user's yes -> call.

Run next to the voice server and the token server:
    uv run python -m voice.outbound                    # watch Bee; the meta agent decides what to propose
    uv run python -m voice.outbound --once INBOX_DIR   # the meta agent looks at one saved Bee change
                                                       # (workspace/inbox/...), then waits for any offer it made
    uv run python -m voice.outbound --rules            # the old rule-based planner decides instead
    uv run python -m voice.outbound --rules --dry-run  # rules, writing plans but never asking or calling
    uv run python -m voice.outbound --rules --direct   # rules, calling without asking (tests only)

The meta agent (meta/agent.py) wakes for events in workspace/meta/events.jsonl: a Bee change from this watcher,
a call that ended (the voice server), the user's answer to an offer (the token server), and a booked call coming
up (this process). Every time it thinks, the meta session records why it woke, what it looked at and what it
decided, in its own words. The rule-based planner (beelink/planner.py) is kept for comparison: with --rules,
each decision is written to plan.md in the inbox folder, call or not.
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

from beelink.planner import COOLDOWN, MAX_AGE, RulePlanner, call_in_progress, last_outbound
from beelink.watcher import BeeWatcher, Trigger
from voice import voip_push
from voice.continuation import earlier_call
from voice.dispatch import RING_SECONDS, place_call
from voice.language import call_language
from voice.offers import OPEN, Offer, OfferStore, agreed, notification, plan_of
from voice.prep import NOTE_SECONDS, OPENING_BY_S, prepare_outbound
from meta.agent import Thinker
from meta.session import DAILY_NOTIFICATIONS, MetaSession, add_event, meta_notes, quiet, quiet_ends

load_dotenv(".env.local")
log = logging.getLogger("bee-call.outbound")
WORKSPACE = Path("workspace")


OFFER_CHECK_S = 5.0  # how often answered and booked offers are checked (arbitrary)
META_CHECK_S = 3.0  # how often the meta agent's inbox is checked; events that come together are seen together
BOOKED_NOTICE = timedelta(minutes=10)  # the meta agent hears of a booked call this early, to set its agenda
META_TRIES = 3  # a think that fails is tried again with the same events this many times in all
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


async def send_offer(offer: Offer, workspace: Path, now: datetime) -> str:
    """Sends an offer's notification unless it is quiet time or the day's notifications are used up; then it is
    held (it still shows in the app) and send_held() sends it later. Returns what happened, in words."""
    store = OfferStore(workspace)
    if quiet(now):
        return f"held: quiet hours, the notification goes out at {quiet_ends(now):%H:%M}; it shows in the app now"
    if store.notified_on(now) >= DAILY_NOTIFICATIONS:
        return f"held: {DAILY_NOTIFICATIONS} notifications went out today already; it shows in the app now"
    payload = notification(offer, now, call_language())
    try:
        sent = await voip_push.alert(payload, workspace, collapse_id=offer.id,
                                     expires_epoch=int(offer.at("expires_at").timestamp()))
    except Exception as exc:  # the offer still waits in the app's list
        print(f"asked: offer {offer.id}, but the notification failed: {exc}", flush=True)
        return f"the notification failed ({exc}); it shows in the app"
    if not sent:
        return "no notification (no APNs key, or the app has not sent its token yet); it shows in the app"
    store.mark_notified(offer.id, now)
    print(f"asked: offer {offer.id}, {payload['aps']['alert']['body']!r} (apns-id {sent})", flush=True)
    return f"notification sent: {payload['aps']['alert']['body']}"


async def send_held(workspace: Path, now: datetime) -> None:
    """Sends notifications held by quiet hours or the daily limit, once they may go out."""
    for offer in OfferStore(workspace).unsent(now):
        result = await send_offer(offer, workspace, now)
        if result.startswith("held") or result.startswith("no notification"):
            return


def busy(workspace: Path, now: datetime) -> bool:
    """A call is going on, or an AI call was placed so recently that it may still be ringing."""
    placed = last_outbound(workspace)
    return call_in_progress(workspace, now) or (
        placed is not None and timedelta(0) <= now - placed < timedelta(seconds=RING_SECONDS + 30))


async def call_due(workspace: Path, now: datetime | None = None, prepare=prepare_outbound, place=place_call) -> list[str]:
    """Places a call the user said yes to: "Call me now", an accepted time that has come (prepared up to
    BOOKED_AHEAD early), or a time booked in a call. One call per round, and none while another call is going on
    or ringing; the others wait for a later round and close if they are more than LATE_BY late. Returns the
    room, or nothing."""
    store = OfferStore(workspace)
    now = now or datetime.now()
    for offer in store.due(now, ahead=BOOKED_AHEAD):
        if busy(workspace, now):
            return []
        right_now = offer.state == "now"
        if store.claim(offer.id, now) is None:  # called off meanwhile, or another runner has it
            continue
        try:
            extra = {"agreed": agreed(offer), "meta_notes": meta_notes(workspace)}
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
            store.mark(offer.id, "failed", datetime.now(), error=f"{type(exc).__name__}: {exc}"[:300])
            log.exception("could not place the call for offer %s", offer.id)
            continue
        store.mark(offer.id, "called", datetime.now(), room=room)
        print(f"calling for offer {offer.id}: room {room}", flush=True)
        return [room]
    return []


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


def bee_event(trigger: Trigger, workspace: Path) -> dict:
    """A Bee change as a meta agent event: the inbox folder relative to the workspace, and what changed."""
    try:
        inbox = str(trigger.inbox_dir.resolve().relative_to(workspace.resolve()))
    except ValueError:
        inbox = str(trigger.inbox_dir)
    return {"inbox": inbox, "conversation_ids": trigger.conversation_ids, "counts": trigger.counts,
            "reason": trigger.reason}


async def think_about_new_events(session: MetaSession, thinker: Thinker, now: datetime, tries: int) -> int:
    """One look at the inbox. The cursor moves only after a think that worked; a failed one sees the same events
    next round, up to META_TRIES times in all, and then they are logged as given up. Returns the failed tries so far."""
    events, offset = session.peek_events()
    if not events:
        return tries
    entry = await thinker.think(events, now)
    print(f"meta agent ({entry['seconds']} s): {entry.get('decision') or entry.get('error')}", flush=True)
    tries = tries + 1 if entry.get("error") else 0
    if entry.get("error") and tries < META_TRIES:
        return tries
    if tries:
        session.log({"at": now.isoformat(timespec="seconds"), "events": events, "actions": [], "tools": [],
                     "error": f"gave up on these events after {tries} failed tries"})
    session.commit(offset)
    return 0


async def serve_meta(workspace: Path, thinker: Thinker, until_offer_closes: bool = False) -> None:
    """Wakes the meta agent for new events, tells it about booked calls coming up, and sends held notifications
    once they may go out. With until_offer_closes (for --once), stops when no offer is open any more."""
    session, store, announced, tries = MetaSession(workspace), OfferStore(workspace), set(), 0
    while True:
        now = datetime.now()
        try:
            for offer in store.open(now):
                call_at = offer.at("call_at")
                if offer.state == "booked" and call_at and timedelta(0) < call_at - now <= BOOKED_NOTICE \
                        and offer.id not in announced:
                    announced.add(offer.id)
                    add_event(workspace, "booked_soon", offer=offer.id, call_at=offer.call_at, topic=offer.topic)
            tries = await think_about_new_events(session, thinker, now, tries)
            await send_held(workspace, now)
        except Exception:
            log.exception("the meta loop failed")
        if until_offer_closes and not store.open(datetime.now()):
            return
        await asyncio.sleep(META_CHECK_S)


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
    parser.add_argument("--direct", action="store_true", help="with --rules: call without asking (tests only)")
    parser.add_argument("--rules", action="store_true", help="the rule-based planner decides, not the meta agent")
    args = parser.parse_args()
    planner = RulePlanner(WORKSPACE, cooldown=timedelta(0) if args.no_cooldown else COOLDOWN,
                          max_age=timedelta(days=3650) if args.any_age else MAX_AGE)
    thinker = Thinker(WORKSPACE, send=send_offer)
    if args.once:
        if args.rules:
            made = await handle(saved_trigger(args.once), planner, args.dry_run, args.direct)
            if made and not args.direct:
                await serve_offers(WORKSPACE, until=made)  # wait for the answer, then call
            return
        # The meta agent looks at that change; for any offer it makes, wait for the answer and place the call.
        add_event(WORKSPACE, "bee_change", **bee_event(saved_trigger(args.once), WORKSPACE))
        placing = asyncio.create_task(serve_offers(WORKSPACE))
        try:
            await serve_meta(WORKSPACE, thinker, until_offer_closes=True)
        finally:
            placing.cancel()
        return

    async def on_trigger(trigger: Trigger) -> None:
        # Errors go back to the watcher, which keeps its cursor so the same change is looked at again.
        if args.rules:
            await handle(trigger, planner, args.dry_run, args.direct)
        else:
            add_event(WORKSPACE, "bee_change", **bee_event(trigger, WORKSPACE))

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        loops = [] if args.dry_run else [serve_offers(WORKSPACE)] + ([] if args.rules else [serve_meta(WORKSPACE, thinker)])
        await asyncio.gather(BeeWatcher(WORKSPACE, on_trigger).run(), *loops)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
