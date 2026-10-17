"""Asking before calling. The phone rings only when the user wants it, or at a time they agreed to (Matt,
2026-10-14).

When a Bee change is worth a call, the planner's plan becomes an offer instead of a call: an app notification
such as "About “Planned a Saturday hike”: free for a quick call at 9:00 PM today?" with three buttons. The AI
calls only when the user taps "Call me now", at the suggested time once the user accepted it, or at a time agreed
out loud in a call (the coordinator's book_call tool). The notification also shows on the lock screen and on
Apple Watch, and the app lists open offers on its first screen, so a missed notification can still be answered.

One offer is one file, workspace/state/offers/<id>.json, and moves through these states:
  asked -> now | booked -> calling -> placing -> called    the user said yes
  asked -> declined | expired                              no call
  booked (agreed in a call, or asked for in a voice memo) -> calling -> placing -> called
  booked | calling -> declined                             a booked call called off before its room exists
  calling | placing -> failed                              the call could not be placed
"calling" means a runner claimed the offer and prepares the call (up to BOOKED_AHEAD early for a booked time);
"placing" means it is creating the call's room right now, after checking once more that the offer is still
wanted. Only the runner holding the claim (a Claim) can move it on, so the room is opened only for a yes that
still stands (voice/dispatch.py place_call).
voice/token_server.py takes the app's answers (POST /offers/<id>) and lists open offers (GET /offers);
voice/outbound.py sends new offers and places the calls that are due.
"""

import fcntl
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from beelink.clock import parse_time
from beelink.planner import OFFERS_DIR, Plan
from voice.language import CallLanguage
from voice.people import DEFAULT_CALL_TIME

MIN_LEAD = timedelta(minutes=10)  # closer than this to today's call time, an offer suggests tomorrow (arbitrary)
LATE_BY = timedelta(minutes=10)  # a booked call this late is not placed any more (arbitrary)
STUCK = timedelta(minutes=5)  # "calling" or "placing" this long means the placing process died (arbitrary)
OPEN = ("asked", "now", "booked", "calling", "placing")
ANSWERS = {"now": "now", "later": "booked", "decline": "declined"}
OFFER_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")


class UnknownChoice(ValueError):
    """The answer is not one of ANSWERS (the token server's 400)."""


class OfferClosed(ValueError):
    """The offer no longer takes this answer: answered, closed, or its call is being placed (409)."""


class QuestionWaiting(ValueError):
    """A question the user has not answered yet: no new one is asked meanwhile."""

    def __init__(self, waiting: "Offer") -> None:
        super().__init__(f'the user has not answered the offer about "{waiting.topic}" ({waiting.id}) yet')
        self.waiting = waiting


# A memo asking for a call this close to a booked one joins it, and the call keeps its time; further apart, the memo
# may have said why it wanted its own time, so it is booked on its own (second review of 2026-10-17). Arbitrary.
BOOKED_NEAR = timedelta(minutes=10)


class AlreadyOffered(ValueError):
    """A Bee conversation that already had an offer, whatever became of it: one conversation leads to at most one
    offer (BC-014)."""


@dataclass
class Offer:
    id: str
    state: str
    reason: str
    topic: str | None = None
    kind: str | None = None  # "conversation" or "voice note" (from the plan); None for a call agreed in a call
    source: str = "bee"  # "bee": asked by notification; "call": agreed out loud in a call
    # Times are ISO with the person's time zone (voice/people.py Person.now); older offers' carry none (parse_time).
    suggested_at: str | None = None  # the time the notification suggests
    call_at: str | None = None  # when to call, once the user said yes
    expires_at: str | None = None  # an unanswered offer closes here
    inbox: str | None = None
    plan: str | None = None
    conversation_ids: list[int] = field(default_factory=list)
    memo_ids: list[str] = field(default_factory=list)  # the voice memos (Bee journals) behind it
    asked: str | None = None  # a voice memo's own words asking for this call (meta/agent.py book_call)
    continue_from: str | None = None  # the call it was agreed in (voice/continuation.py)
    room: str | None = None
    claim: str | None = None  # the runner's claim while it is calling or placing (Claim.token)
    error: str | None = None
    notified_at: str | None = None  # when its notification went out; None while held (quiet hours, daily limit)
    # Its notification: "sending" (recorded before the push), then "sent"; "app only" when the day's notifications
    # were used up: then it shows only in the app's list, and never as a notification (Matt, 2026-10-15).
    notice: str | None = None
    # Why a call the user agreed to was not placed: "budget", "late" (more than LATE_BY) or "failed"; the user is
    # told once, by a plain notification (voice/outbound.py tell_not_placed), at told_at.
    not_placed: str | None = None
    told_at: str | None = None
    soon_told_at: str | None = None  # when the meta agent heard this booked call is coming up (voice/outbound.py)
    history: list[dict[str, str]] = field(default_factory=list)  # [{"at": ISO, "state": ...}]

    def at(self, name: str) -> datetime | None:
        value = getattr(self, name)
        return parse_time(value) if value else None

    def view(self, language: str = "en", zone: tzinfo | None = None) -> dict[str, Any]:
        """What the app shows (contract section 4). The state is what the user can do: the app offers "Call off" for
        "booked", so a booked call a runner already prepares is still "booked" until its room is being made; from
        then on, like a "Call me now" ("now") being prepared, it is "calling". The topic is never empty: without one
        of its own it is its notification's line in the person's `language`. Times carry the offset of the person's
        time zone (`zone`). The epochs, kind and source are for app builds from before D."""
        state = {"now": "calling", "placing": "calling"}.get(self.state, self.state)
        if self.state == "calling" and booked_call(self):
            state = "booked"
        created = parse_time(self.history[0]["at"]) if self.history else datetime.strptime(
            self.id[:15], "%Y%m%d-%H%M%S").astimezone()

        def shown(moment: datetime | None) -> str | None:
            return None if moment is None else moment.astimezone(zone).isoformat(timespec="seconds")

        data = {"id": self.id, "state": state, "topic": topic_line(self.topic, self.kind, language),
                "kind": self.kind, "source": self.source, "created_at": shown(created),
                "suggested_at": shown(self.at("suggested_at")), "call_at": shown(self.at("call_at"))}
        for name in ("suggested_at", "call_at"):
            moment = self.at(name)
            data[name.replace("_at", "_epoch")] = int(moment.timestamp()) if moment else None
        return data


def call_time(now: datetime, at: str | None = None) -> datetime:
    """Today's call time, or tomorrow's when today's is past or too close, on the person's clock (`now` is their
    time). `at`: the person's call_time setting; voice/people.py owns its default."""
    hour, minute = (int(x) for x in (at or DEFAULT_CALL_TIME).split(":"))
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today - now >= MIN_LEAD else today + timedelta(days=1)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Claim:
    """A due offer one runner moved to "calling" (OfferStore.claim). Only its holder can place the call, and only
    while the offer is still claimed with this token."""
    offer: Offer
    token: str
    before: str  # the state it was claimed from, "now" or "booked"


class OfferStore:
    """The offer files. The token server and voice/outbound.py are separate processes that both change them, so
    every change re-reads the file under one lock (offers/.lock), and a call is placed only after claim() moved
    its offer to "calling" (a second runner, or a "Call off" that came in meanwhile, makes the claim fail)."""

    def __init__(self, workspace: Path) -> None:
        self.dir = workspace / OFFERS_DIR

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as handle:  # flock is per open file: never nest these
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _path(self, offer_id: str) -> Path:
        if not OFFER_ID.fullmatch(offer_id or ""):
            raise KeyError(offer_id)
        return self.dir / f"{offer_id}.json"

    def _save(self, offer: Offer) -> Offer:
        path = self._path(offer.id)
        tmp = path.with_name(f".{offer.id}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps(asdict(offer), ensure_ascii=False, indent=1))
        tmp.replace(path)  # readers never see half a file
        return offer

    def get(self, offer_id: str) -> Offer | None:
        try:
            data = json.loads(self._path(offer_id).read_text())
        except (KeyError, OSError, ValueError):
            return None
        known = {f.name for f in fields(Offer)}
        return Offer(**{k: v for k, v in data.items() if k in known})

    def all(self) -> list[Offer]:
        offers = [self.get(p.stem) for p in sorted(self.dir.glob("*.json"))] if self.dir.is_dir() else []
        return [o for o in offers if o is not None]

    def _new_id(self, now: datetime) -> str:
        return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

    @staticmethod
    def _set(offer: Offer, state: str, now: datetime, **changes: Any) -> Offer:
        for key, value in changes.items():
            setattr(offer, key, value)
        offer.state = state
        offer.history.append({"at": _iso(now), "state": state})
        return offer

    def _move(self, offer_id: str, now: datetime, came_from: tuple[str, ...], state: str,
              **changes: Any) -> Offer | None:
        """The one way an offer's state changes once it exists: on the latest copy of its file, under the lock,
        and only from the states the transition allows. None when the offer is gone or in another state."""
        with self._lock():
            offer = self.get(offer_id)
            if offer is None or offer.state not in came_from:
                return None
            return self._save(self._set(offer, state, now, **changes))

    def _move_claimed(self, held: Claim, now: datetime, came_from: tuple[str, ...], state: str, /,
                      **changes: Any) -> Offer | None:
        """A move only the claim's holder makes: None when the offer was called off or is no longer this claim's."""
        with self._lock():
            offer = self.get(held.offer.id)
            if offer is None or offer.state not in came_from or offer.claim != held.token:
                return None
            return self._save(self._set(offer, state, now, **changes))

    def claim(self, offer_id: str, now: datetime) -> Claim | None:
        """Moves a due offer to "calling" if it is still due; None if it changed meanwhile (called off, or claimed
        by another runner). Only the claim's holder can place the call."""
        token = secrets.token_hex(8)
        with self._lock():
            offer = self.get(offer_id)
            if offer is None or offer.state not in ("now", "booked"):
                return None
            before = offer.state
            return Claim(self._save(self._set(offer, "calling", now, claim=token)), token, before)

    def release(self, claim: Claim, now: datetime) -> Offer | None:
        """The runner stopped before it began placing (its loops were restarted, the process was stopped): the
        offer goes back to the state it was claimed from, so the call is still placed, by the next runner."""
        return self._move_claimed(claim, now, ("calling",), claim.before, claim=None)

    def begin_placing(self, claim: Claim, now: datetime) -> Offer | None:
        """Right before the call's room is created: the last check that the user still wants the call. From here
        on it can no longer be called off. None: it was called off (or is no longer this claim's)."""
        return self._move_claimed(claim, now, ("calling",), "placing")

    def placed(self, claim: Claim, now: datetime, room: str) -> Offer | None:
        """The call's room exists and the phone rings."""
        return self._move_claimed(claim, now, ("placing",), "called", room=room)

    def fail(self, claim: Claim, now: datetime, error: str, why: str = "failed") -> Offer | None:
        """The claimed call could not be placed; `error` says why, and `why` ("budget" or "failed") what the
        user is told."""
        return self._move_claimed(claim, now, ("calling", "placing"), "failed", error=error, not_placed=why)

    def call_off(self, offer_id: str, now: datetime, why: str) -> Offer | None:
        """A call booked in a call is moved or called off in that call (voice/coordinator.py)."""
        return self._move(offer_id, now, ("booked",), "declined", error=why)

    def start_notifying(self, offer_id: str, now: datetime, daily_limit: int) -> Offer | str:
        """Records, under the lock and before the push, that this question's notification is going out now; the
        day's count includes it from here. So a process stopped during the push never sends it twice, and two
        senders at once never pass the daily limit together. Returns the offer, or why it is not sent."""
        with self._lock():
            offer = self.get(offer_id)
            if offer is None or offer.state != "asked" or offer.notified_at or offer.notice == "app only":
                return "it no longer waits for its notification"
            if self.notified_on(now) >= daily_limit:
                offer.notice = "app only"
                self._save(offer)
                return f"{daily_limit} notifications went out today already; it shows only in the app"
            offer.notified_at, offer.notice = _iso(now), "sending"
            return self._save(offer)

    def notified(self, offer_id: str, sent: bool) -> None:
        """After the push: kept as sent, or forgotten when it did not go out (no phone or key, APNs refused), so it
        is tried again later."""
        with self._lock():
            offer = self.get(offer_id)
            if offer is not None and offer.notice == "sending":
                offer.notice = "sent" if sent else None
                offer.notified_at = offer.notified_at if sent else None
                self._save(offer)

    def notified_on(self, day: datetime) -> int:
        """How many offer notifications went out on that day of the person's (`day` is their time), counted from
        the moment they started to go out."""
        return sum(1 for o in self.all()
                   if o.notified_at and o.at("notified_at").astimezone(day.tzinfo).date() == day.date())

    def last_notified(self) -> datetime | None:
        """When the latest offer notification went out."""
        sent = [o.at("notified_at") for o in self.all() if o.notified_at]
        return max(sent) if sent else None

    def unsent(self, now: datetime) -> list[Offer]:
        """Open questions whose notification is still held (not those shown only in the app)."""
        return [o for o in self.open(now) if o.state == "asked" and not o.notified_at and o.notice != "app only"]

    def ask(self, plan: Plan, now: datetime, at: str | None = None) -> Offer:
        """A plan the meta agent (or the rule-based planner) wants to call about becomes a question. One question at
        a time: while one is still waiting for the user's answer, a new one is not asked (QuestionWaiting), so a
        notification the user has not answered yet never stops working under them. AlreadyOffered: a Bee
        conversation of the plan already had an offer (BC-014). The one place for both rules (server review 14)."""
        suggested = call_time(now, at)
        offer = Offer(id=self._new_id(now), state="asked", reason=plan.reason, topic=plan.topic, kind=plan.kind,
                      suggested_at=_iso(suggested), expires_at=_iso(suggested),
                      inbox=str(plan.inbox_dir) if plan.inbox_dir else None,
                      plan=str(plan.path) if plan.path else None, conversation_ids=list(plan.conversation_ids),
                      memo_ids=list(plan.memo_ids), history=[{"at": _iso(now), "state": "asked"}])
        with self._lock():
            self._expire(now)  # a question past its time no longer waits
            for earlier in self.all():
                self._once(plan.conversation_ids, plan.memo_ids, earlier)
            waiting = [o for o in self.all() if o.state == "asked"]
            if waiting:
                raise QuestionWaiting(waiting[0])
            return self._save(offer)

    @staticmethod
    def _once(conversation_ids: list[int], memo_ids: list[str], earlier: "Offer") -> None:
        """One Bee conversation, or one voice memo, leads to at most one offer, whatever became of it (BC-014)."""
        shared = sorted(set(conversation_ids) & set(earlier.conversation_ids))
        if shared:
            raise AlreadyOffered(f"Bee conversation {', '.join(map(str, shared))} already had an offer "
                                 f"({earlier.id}, {earlier.state})")
        memos = sorted(set(memo_ids) & set(earlier.memo_ids))
        if memos:
            raise AlreadyOffered(f"voice memo {', '.join(memos)} already had a call booked or offered "
                                 f"({earlier.id}, {earlier.state})")

    def book_from_input(self, topic: str, call_at: datetime, now: datetime, *, reason: str, asked: str,
                        memo_id: str | None = None, conversation_id: int | None = None,
                        inbox: str | None = None) -> tuple[Offer, bool]:
        """The call the user asks for in a Bee input, a voice memo or a conversation, at the time they ask ("오늘 밤
        9시에 전화해서 같이 정리하자"): their asking is their yes, so it is booked, and the person can call it off in
        the app (meta/agent.py book_call). An input asking for a time within BOOKED_NEAR of a call already booked
        joins that call instead, which then reads it too; the call keeps its time and topic (review of 2026-10-17,
        finding 3: two memos booked two calls at 21:00). Returns the offer, and whether the input joined a call
        booked before."""
        memos = [memo_id] if memo_id else []
        conversations = [conversation_id] if conversation_id is not None else []
        offer = Offer(id=self._new_id(now), state="booked", reason=reason, topic=topic,
                      kind="voice note" if memo_id else "conversation", call_at=_iso(call_at), inbox=inbox,
                      memo_ids=memos, conversation_ids=conversations, asked=asked,
                      history=[{"at": _iso(now), "state": "booked"}])
        with self._lock():
            for earlier in self.all():
                self._once(conversations, memos, earlier)
            near = next((o for o in self.all() if o.state == "booked" and o.at("call_at") is not None
                         and abs(o.at("call_at") - call_at) <= BOOKED_NEAR), None)
            if near is not None:
                near.memo_ids = [*near.memo_ids, *memos]
                near.conversation_ids = [*near.conversation_ids, *conversations]
                return self._save(near), True
            return self._save(offer), False

    def book_from_memo(self, topic: str, call_at: datetime, now: datetime, *, reason: str, memo_id: str,
                       asked: str, inbox: str | None = None) -> tuple[Offer, bool]:
        """The call a voice memo asks for (book_from_input)."""
        return self.book_from_input(topic, call_at, now, reason=reason, asked=asked, memo_id=memo_id, inbox=inbox)

    def book(self, topic: str, call_at: datetime, now: datetime, continue_from: str | None = None,
             language: CallLanguage | None = None) -> Offer:
        """A call the user asked for out loud, such as "call me tomorrow at 8". Its reason is said to the person, in
        the call's `language`, as the call's card shows it (app contract, decision 2)."""
        offer = Offer(id=self._new_id(now), state="booked", source="call", topic=topic,
                      reason=booked_reason(now, language), call_at=_iso(call_at), continue_from=continue_from,
                      history=[{"at": _iso(now), "state": "booked"}])
        with self._lock():
            return self._save(offer)

    def answer(self, offer_id: str, choice: str, now: datetime) -> Offer:
        """The user's answer from a notification button or the app. KeyError: no such offer (checked first).
        UnknownChoice: not one of ANSWERS. OfferClosed: the offer no longer waits for an answer. "decline" also
        calls off a booked call (the app's "Call off"), until its room is being made: also while a runner
        prepares it."""
        with self._lock():
            self._expire(now)
            offer = self.get(offer_id)
            if offer is None:
                raise KeyError(offer_id)
            if choice not in ANSWERS:
                raise UnknownChoice(f"choice must be one of {', '.join(ANSWERS)}")
            if choice == "decline" and booked_call(offer) and offer.state in ("booked", "calling"):
                return self._save(self._set(offer, "declined", now, claim=None))
            if offer.state != "asked":
                raise OfferClosed(f"the offer is {offer.state}")
            state = ANSWERS[choice]
            call_at = {"now": _iso(now), "booked": offer.suggested_at}.get(state)
            return self._save(self._set(offer, state, now, call_at=call_at))

    def _expire(self, now: datetime) -> None:
        for offer in self.all():
            call_at, expires_at = offer.at("call_at"), offer.at("expires_at")
            if offer.state == "asked" and expires_at and now >= expires_at:
                self._save(self._set(offer, "expired", now))
            elif offer.state in ("now", "booked") and call_at and now > call_at + LATE_BY:
                self._save(self._set(offer, "expired", now, error="the call could not be placed in time",
                                     not_placed="late"))
            elif offer.state in ("calling", "placing") and offer.history and \
                    now - parse_time(offer.history[-1]["at"]) > STUCK:
                self._save(self._set(offer, "failed", now, error="placing the call did not finish",
                                     not_placed="failed"))

    def expire(self, now: datetime) -> None:
        with self._lock():
            self._expire(now)

    def tell_soon(self, offer_id: str, now: datetime) -> bool:
        """Marks a booked call as announced to the meta agent; False when it already was. Kept in the offer's file,
        so a restart never announces it again (BC-043)."""
        with self._lock():
            offer = self.get(offer_id)
            if offer is None or offer.soon_told_at:
                return False
            offer.soon_told_at = _iso(now)
            self._save(offer)
            return True

    def to_tell(self, now: datetime) -> list[Offer]:
        """Calls the user agreed to that could not be placed, and the user was not told yet."""
        self.expire(now)
        return [o for o in self.all() if o.not_placed and not o.told_at]

    def start_telling(self, offer_id: str, now: datetime) -> Offer | None:
        """Records, before the push, that the user is being told: once, also when two loops look at once."""
        with self._lock():
            offer = self.get(offer_id)
            if offer is None or not offer.not_placed or offer.told_at:
                return None
            offer.told_at = _iso(now)
            return self._save(offer)

    def open(self, now: datetime) -> list[Offer]:
        self.expire(now)
        return [o for o in self.all() if o.state in OPEN]

    def due(self, now: datetime, ahead: timedelta = timedelta(0)) -> list[Offer]:
        """Offers to call: the user tapped "Call me now", or a booked time is at most `ahead` away. Closing
        offers uses `now`, so an answer in the last seconds before the suggested time still counts."""
        self.expire(now)
        until = now + ahead
        return [o for o in self.all() if o.state in ("now", "booked") and o.at("call_at") and o.at("call_at") <= until]


def booked_reason(now: datetime, language: CallLanguage | None) -> str:
    """Why the AI calls at a time the person booked out loud, said to them in their language."""
    if language is not None and language.code == "ko":
        return f"{clock_label(now, language)} 통화에서 이 시간에 다시 전화해 달라고 하셨어요."
    return f"In our call at {now:%-I:%M %p}, you asked me to call you back at this time."


def booked_call(offer: Offer) -> bool:
    """A call agreed for a time (the suggested time, or a time booked in a call), not "Call me now"."""
    return not any(h["state"] == "now" for h in offer.history)


def plan_of(offer: Offer) -> Plan:
    """The plan voice/outbound.py places the call with."""
    return Plan(call=True, reason=offer.reason, inbox_dir=Path(offer.inbox) if offer.inbox else None,
                conversation_ids=list(offer.conversation_ids), topic=offer.topic, kind=offer.kind,
                path=Path(offer.plan) if offer.plan else None, memo_ids=list(offer.memo_ids))


def agreed(offer: Offer) -> str:
    """How the user agreed to this call, for the AI's opening."""
    said = next((h["at"] for h in offer.history if h["state"] in ("now", "booked")), None)
    when = parse_time(said).strftime("%H:%M") if said else "earlier"
    if offer.source == "call":
        return f"the user asked for this call back during an earlier call, at {when}"
    if offer.asked:  # booked from a Bee input that asks for it
        source = "a voice memo" if offer.kind == "voice note" else "what Bee recorded of them"
        return f"the user asked for this call in {source} (\"{offer.asked}\"), and you booked it at {when}"
    if any(h["state"] == "now" for h in offer.history):
        return f"the user tapped \"Call me now\" on your notification at {when}"
    return f"at {when} the user accepted the time your notification suggested"


def clock_label(moment: datetime, language: CallLanguage) -> str:
    """The time of day as the user reads it: "밤 9시", "9:00 PM"."""
    if language.code == "ko":
        hour = moment.hour
        period = "새벽" if hour < 5 else "밤" if hour >= 20 else "저녁" if hour >= 17 else "오후" if hour >= 12 else "오전"
        return f"{period} {hour % 12 or 12}시" + (f" {moment.minute}분" if moment.minute else "")
    return moment.strftime("%-I:%M %p")


KO_WEEKDAYS = "월화수목금토일"


def when_label(moment: datetime, now: datetime, language: CallLanguage) -> str:
    """When a call is, as the user reads it: "오늘 밤 9시", "9:00 PM tomorrow", and after tomorrow its date and
    weekday (review of 2026-10-17, finding 4: a call three days ahead read "tomorrow")."""
    moment = moment.astimezone(now.tzinfo)  # on the person's clock now
    days = (moment.date() - now.date()).days
    if language.code == "ko":
        day = "오늘" if days <= 0 else "내일" if days == 1 else \
            f"{moment.month}월 {moment.day}일 {KO_WEEKDAYS[moment.weekday()]}요일"
        return f"{day} {clock_label(moment, language)}"
    day = "today" if days <= 0 else "tomorrow" if days == 1 else f"on {moment:%A, %B} {moment.day}"
    return f"{clock_label(moment, language)} {day}"


NOT_PLACED = {  # why an agreed call was not placed, as the user reads it
    "en": {"budget": "today's budget is used up.", "late": "it could not be placed on time.",
           "failed": "something went wrong while placing it."},
    "ko": {"budget": "오늘 예산을 다 썼어요.", "late": "제시간에 걸 수 없었어요.", "failed": "거는 중에 문제가 생겼어요."},
}


def not_placed_notification(offer: Offer, language: CallLanguage) -> dict[str, Any]:
    """The plain notification for a call the user agreed to that could not be placed (contract section 5): no
    buttons, in the person's language."""
    why = NOT_PLACED["ko" if language.code == "ko" else "en"].get(offer.not_placed or "failed")
    call_at = offer.at("call_at")
    if language.code == "ko":
        which = "요청하신 통화를" if not booked_call(offer) or call_at is None else f"{clock_label(call_at, language)} 통화를"
        body = f"{which} 걸지 못했어요. {why}"
    else:
        which = "the call you asked for" if not booked_call(offer) or call_at is None else \
            f"your {clock_label(call_at, language)} call"
        body = f"Couldn't place {which}: {why}"
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "thread-id": "offers"}}


# What an offer, or an AI call, is about when it has no topic of its own: the line its notification uses, in the
# person's language. The app shows the same line as the call's or the question's topic (contract sections 1 and 4).
NO_TOPIC = {"ko": {"voice note": "새 음성 메모 이야기", None: "오늘 하루 이야기"},
            "en": {"voice note": "Your new voice note", None: "Your day"}}


def topic_line(topic: str | None, kind: str | None, language: str) -> str:
    """The topic, or for none the notification's line for the kind ("voice note" or another), in `language`."""
    if isinstance(topic, str) and topic.strip():
        return topic.strip()
    lines = NO_TOPIC["ko" if language == "ko" else "en"]
    return lines["voice note"] if kind == "voice note" else lines[None]


def memo_booked_notification(offer: Offer, now: datetime, language: CallLanguage) -> dict[str, Any]:
    """The one plain notification for a call the user asked for in a Bee input, a voice memo or a conversation: when
    the AI will call, about what, and that the app can call it off. No buttons."""
    when = when_label(offer.at("call_at") or now, now, language)
    topic = topic_line(offer.topic, offer.kind, language.code)
    body = (f"{when}에 전화할게요: {topic}. 앱에서 취소할 수 있어요." if language.code == "ko"
            else f"I'll call you at {when}: {topic}. You can call it off in the app.")
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "thread-id": "offers"},
            "offer": offer.id}


def notification(offer: Offer, now: datetime, language: CallLanguage) -> dict[str, Any]:
    """The APNs payload for an offer. The app defines the buttons (CALL_OFFER, CALL_OFFER_KO)."""
    when = when_label(offer.at("suggested_at") or now, now, language)
    topic = (offer.topic or "").strip()
    line = topic_line(None, offer.kind, language.code)
    if language.code == "ko":
        about = f"“{topic}” 이야기" if topic else line
        body, category = f"{about}, {when}에 잠깐 통화 괜찮으세요?", "CALL_OFFER_KO"
    else:
        about = f"About “{topic}”" if topic else f"About {line[0].lower()}{line[1:]}"
        body, category = f"{about}: free for a quick call at {when}?", "CALL_OFFER"
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "category": category,
                    "thread-id": "offers"},
            "offer": offer.id}


def main(argv: list[str] | None = None) -> None:
    """For tests from outside (README: Testing from outside): make an offer the way the meta agent does, without its
    model. The offer asks; nothing rings until the user says yes."""
    import argparse
    import json as json_module
    import sys

    from voice.people import CALL_TIME, People

    parser = argparse.ArgumentParser(prog="python -m voice.offers", description=main.__doc__)
    parser.add_argument("--data", type=Path, help="data root (default: BEE_CALL_DATA or data/)")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="ask an approved person whether they want a call about one topic")
    ask.add_argument("person")
    ask.add_argument("--topic", required=True, help="what the call would be about, as the notification says it")
    ask.add_argument("--reason", required=True, help="why the AI would call (the call's opening reads it)")
    ask.add_argument("--conversation", type=int, action="append", default=[],
                     help="a Bee conversation id behind it (one offer per conversation, ever)")
    ask.add_argument("--at", help="the time to suggest, like 20:30 (default: the person's call_time)")
    args = parser.parse_args(argv)
    people = People(args.data)
    if not people.known(args.person):
        sys.exit(f"{args.person} is not approved")
    if args.at and not CALL_TIME.fullmatch(args.at):
        sys.exit("--at must look like 21:00")
    person = people.person(args.person)
    plan = Plan(call=True, reason=args.reason, inbox_dir=None, conversation_ids=args.conversation, topic=args.topic,
                kind="conversation" if args.conversation else None)
    try:
        offer = OfferStore(person.workspace).ask(plan, person.now(), at=args.at or person.settings().call_time)
    except (QuestionWaiting, AlreadyOffered) as exc:
        sys.exit(f"not asked: {exc}")
    print(json_module.dumps(offer.view(), ensure_ascii=False))


if __name__ == "__main__":
    main()
