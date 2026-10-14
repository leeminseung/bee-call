"""Bee Call between calls: Bee watcher -> meta agent -> offer -> the user's yes -> call.

Run next to the voice server and the token server:
    uv run python -m voice.outbound                    # everyone approved (voice/people.py), each with their own
                                                       # loops: Bee watcher (if they connected Bee), meta agent,
                                                       # offers and the calls they agreed to
For one person on a Mac (--person, or BEE_CALL_DEV_PERSON):
    uv run python -m voice.outbound --once INBOX_DIR   # the meta agent looks at one saved Bee change
                                                       # (<their folder>/inbox/...), then waits for any offer it made
    uv run python -m voice.outbound --rules            # the old rule-based planner decides instead
    uv run python -m voice.outbound --rules --dry-run  # rules, writing plans but never asking or calling

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

from beelink import cli as bee
from beelink.calls import our_call_windows
from beelink.planner import COOLDOWN, MAX_AGE, OUTBOUND_LOG, RulePlanner, call_in_progress, last_outbound
from beelink.watcher import BeeWatcher, Trigger
from voice import voip_push
from voice.continuation import earlier_call
from voice.dispatch import DECLINES, RING_SECONDS, place_call
from voice.costs import SpendLog, day_budget
from voice.language import CallLanguage, call_language
from voice.people import People, Person, Settings, dev_person
from voice.offers import OPEN, Offer, OfferStore, agreed, not_placed_notification, notification, plan_of
from voice.prep import NOTE_SECONDS, OPENING_BY_S, prepare_outbound
from meta.agent import Thinker
from meta.session import DAILY_NOTIFICATIONS, MetaSession, add_event, meta_notes, quiet, quiet_ends

load_dotenv(".env.local")
log = logging.getLogger("bee-call.outbound")


OFFER_CHECK_S = 5.0  # how often answered and booked offers are checked (arbitrary)
META_CHECK_S = 3.0  # how often the meta agent's inbox is checked; events that come together are seen together
BOOKED_NOTICE = timedelta(minutes=10)  # the meta agent hears of a booked call this early, to set its agenda
META_TRIES = 3  # a think that fails is tried again with the same events this many times in all
BOOKED_AHEAD = timedelta(seconds=30)  # a booked call is prepared this early, so it rings on time
NOW_PREP_S = OPENING_BY_S + 2.0  # "Call me now": ring soon; the note gets this long (GLM 5 needed 4.5-7.7 s)
PEOPLE_CHECK_S = 10.0  # how often the list of people and their settings is read again (arbitrary)
# An offer notification waits this long after a call ended (answered, missed or declined) and after the last offer
# notification went out (Matt, 2026-10-15).
OFFER_GAP = timedelta(minutes=30)


async def handle(trigger: Trigger, planner: RulePlanner, dry_run: bool, person: Person) -> str | None:
    """Decides; a call decision asks the user. Returns the offer id."""
    plan = planner.decide(trigger)
    print(f"plan: {'CALL' if plan.call else 'no call'} - {plan.reason} ({plan.path})", flush=True)
    if not plan.call or dry_run:
        return None
    now = planner.now()
    offer = OfferStore(planner.workspace).ask(plan, now)
    await send_offer(offer, person, now)
    return offer.id


def last_call_end(workspace: Path) -> datetime | None:
    """When the person's last call ended: a call recorded in their folder (either direction), or an AI call that
    rang without an answer (missed) or was declined while it rang."""
    ends = [end for _, end in our_call_windows(workspace)]
    declined_at = {}
    for line in _lines(workspace / DECLINES):
        declined_at[line.get("room")] = datetime.fromisoformat(line["at"]).timestamp()
    for line in _lines(workspace / OUTBOUND_LOG):
        placed = datetime.fromisoformat(line["at"]).timestamp()
        ends.append(declined_at.get(line.get("room"), placed + RING_SECONDS))
    return datetime.fromtimestamp(max(ends)) if ends else None


def _lines(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and isinstance(line.get("at"), str):
            out.append(line)
    return out


def notification_wait(person: Person, now: datetime) -> str | None:
    """Why an offer notification may not go out now, or None. It never goes out during a call, or within OFFER_GAP
    of the last call's end or of the last offer notification, or in quiet hours; send_held() sends it once it may.
    The offer shows in the app's list meanwhile."""
    workspace = person.workspace
    if busy(workspace, now):
        return f"a call is going on; the notification goes out {OFFER_GAP.seconds // 60} minutes after it ends"
    ended = last_call_end(workspace)
    if ended is not None and timedelta(0) <= now - ended < OFFER_GAP:
        return f"the last call ended at {ended:%H:%M}; the notification goes out at {ended + OFFER_GAP:%H:%M}"
    sent = OfferStore(workspace).last_notified()
    if sent is not None and timedelta(0) <= now - sent < OFFER_GAP:
        return f"the last offer went out at {sent:%H:%M}; this one goes out at {sent + OFFER_GAP:%H:%M}"
    if quiet(now):
        return f"quiet hours, the notification goes out at {quiet_ends(now):%H:%M}"
    return None


async def send_offer(offer: Offer, person: Person, now: datetime, language: CallLanguage | None = None) -> str:
    """Sends an offer's notification unless it has to wait (notification_wait): then it is held, still shows in the
    app, and send_held() sends it later. Once the day's notifications are used up, the offer shows only in the app.
    Returns what happened, in words."""
    store = OfferStore(person.workspace)
    wait = notification_wait(person, now)
    if wait is not None:
        return f"held: {wait}; it shows in the app now"
    going = store.start_notifying(offer.id, now, DAILY_NOTIFICATIONS)
    if isinstance(going, str):
        return f"held: {going}"
    payload = notification(going, now, language or call_language())
    try:
        sent = await voip_push.alert(payload, person, collapse_id=offer.id,
                                     expires_epoch=int(offer.at("expires_at").timestamp()))
    except Exception as exc:  # the offer still waits in the app's list
        store.notified(offer.id, sent=False)
        print(f"asked: offer {offer.id}, but the notification failed: {exc}", flush=True)
        return f"the notification failed ({exc}); it shows in the app"
    store.notified(offer.id, sent=bool(sent))
    if not sent:
        return "no notification (no APNs key, or the app has not sent its token yet); it shows in the app"
    print(f"asked: offer {offer.id}, {payload['aps']['alert']['body']!r} (apns-id {sent})", flush=True)
    return f"notification sent: {payload['aps']['alert']['body']}"


async def send_held(person: Person, now: datetime, language: CallLanguage | None = None) -> None:
    """Sends the notifications that had to wait (a call, the 30 minutes after one or after a notification, quiet
    hours), once they may go out."""
    for offer in OfferStore(person.workspace).unsent(now):
        result = await send_offer(offer, person, now, language)
        if result.startswith("held") or result.startswith("no notification"):
            return


def busy(workspace: Path, now: datetime) -> bool:
    """A call is going on, or an AI call was placed so recently that it may still be ringing."""
    placed = last_outbound(workspace)
    return call_in_progress(workspace, now) or (
        placed is not None and timedelta(0) <= now - placed < timedelta(seconds=RING_SECONDS + 30))


async def call_due(person: Person, now: datetime | None = None, prepare=prepare_outbound, place=place_call, *,
                   language: CallLanguage | None = None) -> list[str]:
    """Places a call the user said yes to: "Call me now", an accepted time that has come (prepared up to
    BOOKED_AHEAD early), or a time booked in a call. One call per round, and none while another call is going on
    or ringing; the others wait for a later round and close if they are more than LATE_BY late. Returns the
    room, or nothing. Past the person's daily budget the call is not placed, and the offer says why. The user
    can call the call off until its room is being made: `place` (voice/dispatch.py place_call) checks the claim
    once more right before that, and places nothing for a call that was called off."""
    workspace = person.workspace
    store = OfferStore(workspace)
    now = now or datetime.now()
    for offer in store.due(now, ahead=BOOKED_AHEAD):
        if busy(workspace, now):
            return []
        right_now = offer.state == "now"
        claim = store.claim(offer.id, now)
        if claim is None:  # called off meanwhile, or another runner has it
            continue
        budget = day_budget(person, now)
        if budget.used_up:
            store.fail(claim, now, f"{budget.why()}; ask again tomorrow", why="budget")
            log.info("offer %s not called: %s", offer.id, budget.why())
            continue
        try:
            extra = {"agreed": agreed(claim.offer), "meta_notes": meta_notes(workspace)}
            if offer.continue_from:
                extra["earlier_call"] = earlier_call(workspace, offer.continue_from)
            prep = await prepare(plan_of(claim.offer), extra=extra, spend=SpendLog(workspace), bee=person.settings().bee,
                                 deadline_s=NOW_PREP_S if right_now else NOTE_SECONDS,
                                 **({"language": language} if language else {}))
            wait = (offer.at("call_at") - datetime.now()).total_seconds()
            if wait > 0:  # prepared early for a booked time
                await asyncio.sleep(wait)
            room = await place(claim, person, prep=prep.to_meta())
        except asyncio.CancelledError:  # the loops are stopping: a yes not yet placed is not lost
            store.release(claim, datetime.now())
            raise
        except Exception as exc:
            store.fail(claim, datetime.now(), f"{type(exc).__name__}: {exc}"[:300])
            log.exception("could not place the call for offer %s", offer.id)
            continue
        if room is None:  # called off while it was prepared
            continue
        print(f"calling for offer {offer.id}: room {room}", flush=True)
        return [room]
    return []


async def tell_not_placed(person: Person, now: datetime) -> None:
    """Tells the user, with one plain notification each, about calls they agreed to that could not be placed: the
    budget was used up, it was more than LATE_BY late, or placing failed (contract section 5). These follow the
    user's own yes, so quiet hours hold none of them and they do not count among the day's offers."""
    store = OfferStore(person.workspace)
    for offer in store.to_tell(now):
        telling = store.start_telling(offer.id, now)
        if telling is None:  # another loop got there first
            continue
        payload = not_placed_notification(telling, call_language(person.settings().language))
        try:
            await voip_push.alert(payload, person, collapse_id=f"{offer.id}-not-placed")
        except Exception as exc:  # it shows nowhere else; the offer keeps why
            log.warning("could not tell %s that offer %s was not placed: %s", person.id, offer.id, exc)


async def serve_offers(person: Person, until: str | None = None, **for_person) -> None:
    """Checks for due calls every OFFER_CHECK_S, and tells the user of agreed calls that could not be placed;
    with `until`, stops once that offer is no longer open. `for_person` goes on to call_due (language; prepare
    and place in tests)."""
    store = OfferStore(person.workspace)
    while True:
        try:
            await call_due(person, **for_person)
            await tell_not_placed(person, datetime.now())
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


async def serve_meta(person: Person, thinker: Thinker, until_offer_closes: bool = False,
                     language: CallLanguage | None = None) -> None:
    """Wakes the meta agent for new events, tells it about booked calls coming up, and sends held notifications
    once they may go out. With until_offer_closes (for --once), stops when no offer is open any more. Past the
    person's daily budget it does not think; the events wait for the next day."""
    workspace = person.workspace
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
            if not day_budget(person, now).used_up:
                tries = await think_about_new_events(session, thinker, now, tries)
            await send_held(person, now, language)
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


async def serve_person(people: People, person_id: str, settings: Settings) -> None:
    """One person's loops between calls, reading only their Bee (beelink/cli.py use_bee)."""
    person = people.person(person_id)
    workspace = person.workspace
    language = call_language(settings.language)
    bee.use_bee(people.bee_dir(person_id) if settings.bee else None)  # this task and the loops it starts

    async def send(offer: Offer, ws: Path, now: datetime) -> str:
        return await send_offer(offer, person, now, language)

    async def on_trigger(trigger: Trigger) -> None:
        add_event(workspace, "bee_change", **bee_event(trigger, workspace))

    thinker = Thinker(workspace, send=send, call_time=settings.call_time, bee=settings.bee)
    # A TaskGroup: if one loop fails, the others stop too, so serve_people() never starts a second set beside
    # survivors of the first.
    async with asyncio.TaskGroup() as group:
        group.create_task(serve_offers(person, language=language))
        group.create_task(serve_meta(person, thinker, language=language))
        if settings.bee:
            group.create_task(BeeWatcher(workspace, on_trigger).run())


async def serve_people(people: People) -> None:
    """Keeps one set of loops per approved person, started again when their settings change (for example after
    they connect Bee) and stopped when they are removed."""
    running: dict[str, tuple[asyncio.Task, tuple]] = {}
    try:
        while True:
            wanted = {}
            for person in people.ids():
                settings = people.settings(person)
                wanted[person] = (settings, (settings.language, settings.call_time, settings.bee))
            for person, (task, key) in list(running.items()):
                if person not in wanted or wanted[person][1] != key or task.done():
                    if task.done() and not task.cancelled() and task.exception():
                        log.error("loops for %s stopped: %r", person, task.exception())
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)  # gone before a new set starts
                    del running[person]
            for person, (settings, key) in wanted.items():
                if person not in running:
                    log.info("serving %s (language %s, Bee %s)", person, settings.language, "on" if settings.bee else "off")
                    running[person] = (asyncio.create_task(serve_person(people, person, settings)), key)
            await asyncio.sleep(PEOPLE_CHECK_S)
    finally:
        for task, _ in running.values():
            task.cancel()
        await asyncio.gather(*(task for task, _ in running.values()), return_exceptions=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="with --rules: write plans but never call")
    parser.add_argument("--once", type=Path, help="decide for one saved inbox folder, then exit")
    parser.add_argument("--no-cooldown", action="store_true", help="ignore the gap between calls (testing)")
    parser.add_argument("--any-age", action="store_true", help="also call about old conversations (testing)")
    parser.add_argument("--rules", action="store_true", help="the rule-based planner decides, not the meta agent")
    parser.add_argument("--person", default=None, help="for --once and --rules: whose (default BEE_CALL_DEV_PERSON)")
    args = parser.parse_args()
    people = People()
    bee.use_bee(None)  # Bee works only through a person's own login (beelink/cli.py)

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    if not args.once and not args.rules:
        try:
            await serve_people(people)
        except asyncio.CancelledError:
            pass
        return

    person_id = args.person or dev_person()
    if not person_id or not people.known(person_id):
        raise SystemExit("--once and --rules work for one approved person: pass --person or set BEE_CALL_DEV_PERSON")
    person = people.person(person_id)
    workspace, settings = person.workspace, person.settings()
    language = call_language(settings.language)
    bee.use_bee(people.bee_dir(person_id) if settings.bee else None)
    planner = RulePlanner(workspace, cooldown=timedelta(0) if args.no_cooldown else COOLDOWN,
                          max_age=timedelta(days=3650) if args.any_age else MAX_AGE)

    async def send(offer: Offer, ws: Path, now: datetime) -> str:
        return await send_offer(offer, person, now, language)

    thinker = Thinker(workspace, send=send, call_time=settings.call_time, bee=settings.bee)
    if args.once:
        if args.rules:
            made = await handle(saved_trigger(args.once), planner, args.dry_run, person)
            if made:
                await serve_offers(person, until=made, language=language)  # wait for the answer
            return
        # The meta agent looks at that change; for any offer it makes, wait for the answer and place the call.
        add_event(workspace, "bee_change", **bee_event(saved_trigger(args.once), workspace))
        placing = asyncio.create_task(serve_offers(person, language=language))
        try:
            await serve_meta(person, thinker, until_offer_closes=True, language=language)
        finally:
            placing.cancel()
        return

    async def on_trigger(trigger: Trigger) -> None:
        # Errors go back to the watcher, which keeps its cursor so the same change is looked at again.
        await handle(trigger, planner, args.dry_run, person)

    try:
        loops = [] if args.dry_run else [serve_offers(person, language=language)]
        await asyncio.gather(BeeWatcher(workspace, on_trigger).run(), *loops)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
