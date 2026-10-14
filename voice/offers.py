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
  booked (agreed in a call) -> calling -> placing -> called
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from beelink.planner import OFFERS_DIR, Plan
from voice.language import CallLanguage

CALL_TIME = "21:00"  # the time an offer suggests when CALL_TIME is unset (arbitrary default; a setting later)
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


@dataclass
class Offer:
    id: str
    state: str
    reason: str
    topic: str | None = None
    kind: str | None = None  # "conversation" or "voice note" (from the plan); None for a call agreed in a call
    source: str = "bee"  # "bee": asked by notification; "call": agreed out loud in a call
    suggested_at: str | None = None  # local time the notification suggests (ISO)
    call_at: str | None = None  # when to call, once the user said yes (ISO)
    expires_at: str | None = None  # an unanswered offer closes here (ISO)
    inbox: str | None = None
    plan: str | None = None
    conversation_ids: list[int] = field(default_factory=list)
    continue_from: str | None = None  # the call it was agreed in (voice/continuation.py)
    room: str | None = None
    claim: str | None = None  # the runner's claim while it is calling or placing (Claim.token)
    error: str | None = None
    notified_at: str | None = None  # when its notification went out; None while held (quiet hours, daily limit)
    notice: str | None = None  # its notification: "sending" (recorded before the push), then "sent"
    # Why a call the user agreed to was not placed: "budget", "late" (more than LATE_BY) or "failed"; the user is
    # told once, by a plain notification (voice/outbound.py tell_not_placed), at told_at.
    not_placed: str | None = None
    told_at: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)  # [{"at": ISO, "state": ...}]

    def at(self, name: str) -> datetime | None:
        value = getattr(self, name)
        return datetime.fromisoformat(value) if value else None

    def view(self) -> dict[str, Any]:
        """What the app shows: the times also as epochs, for a phone in another time zone."""
        data = {k: getattr(self, k) for k in ("id", "state", "topic", "kind", "source", "suggested_at", "call_at")}
        for name in ("suggested_at", "call_at"):
            moment = self.at(name)
            data[name.replace("_at", "_epoch")] = int(moment.timestamp()) if moment else None
        return data


def call_time(now: datetime, at: str | None = None) -> datetime:
    """Today's call time, or tomorrow's when today's is past or too close. CALL_TIME is read here, after the
    processes have loaded .env.local."""
    hour, minute = (int(x) for x in (at or os.environ.get("CALL_TIME") or CALL_TIME).split(":"))
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
            if offer is None or offer.state != "asked" or offer.notified_at:
                return "it no longer waits for its notification"
            if self.notified_on(now) >= daily_limit:
                return f"{daily_limit} notifications went out today already"
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
        """How many offer notifications went out on that day (counted from the moment they started to go out)."""
        prefix = day.date().isoformat()
        return sum(1 for o in self.all() if o.notified_at and o.notified_at.startswith(prefix))

    def unsent(self, now: datetime) -> list[Offer]:
        """Open questions whose notification is still held."""
        return [o for o in self.open(now) if o.state == "asked" and not o.notified_at]

    def ask(self, plan: Plan, now: datetime, at: str | None = None) -> Offer:
        """A plan the planner wants to call about becomes a question. An older question still unanswered is
        closed: the newer one is about the user's latest day, and two would ring at the same suggested time."""
        suggested = call_time(now, at)
        offer = Offer(id=self._new_id(now), state="asked", reason=plan.reason, topic=plan.topic, kind=plan.kind,
                      suggested_at=_iso(suggested), expires_at=_iso(suggested),
                      inbox=str(plan.inbox_dir) if plan.inbox_dir else None,
                      plan=str(plan.path) if plan.path else None, conversation_ids=list(plan.conversation_ids),
                      history=[{"at": _iso(now), "state": "asked"}])
        with self._lock():
            for older in self.all():
                if older.state == "asked":
                    self._save(self._set(older, "expired", now, error="replaced by a newer question"))
            return self._save(offer)

    def book(self, topic: str, call_at: datetime, now: datetime, continue_from: str | None = None) -> Offer:
        """A call the user asked for out loud, such as "call me tomorrow at 8"."""
        offer = Offer(id=self._new_id(now), state="booked", source="call", topic=topic,
                      reason=f"In a call at {now:%H:%M}, the user asked to be called back: {topic}",
                      call_at=_iso(call_at), continue_from=continue_from,
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
                    now - datetime.fromisoformat(offer.history[-1]["at"]) > STUCK:
                self._save(self._set(offer, "failed", now, error="placing the call did not finish",
                                     not_placed="failed"))

    def expire(self, now: datetime) -> None:
        with self._lock():
            self._expire(now)

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


def booked_call(offer: Offer) -> bool:
    """A call agreed for a time (the suggested time, or a time booked in a call), not "Call me now"."""
    return not any(h["state"] == "now" for h in offer.history)


def plan_of(offer: Offer) -> Plan:
    """The plan voice/outbound.py places the call with."""
    return Plan(call=True, reason=offer.reason, inbox_dir=Path(offer.inbox) if offer.inbox else None,
                conversation_ids=list(offer.conversation_ids), topic=offer.topic, kind=offer.kind,
                path=Path(offer.plan) if offer.plan else None)


def agreed(offer: Offer) -> str:
    """How the user agreed to this call, for the AI's opening."""
    said = next((h["at"] for h in offer.history if h["state"] in ("now", "booked")), None)
    when = datetime.fromisoformat(said).strftime("%H:%M") if said else "earlier"
    if offer.source == "call":
        return f"the user asked for this call back during an earlier call, at {when}"
    if any(h["state"] == "now" for h in offer.history):
        return f"the user tapped \"Call me now\" on your notification at {when}"
    return f"at {when} the user accepted the time your notification suggested"


def clock_label(moment: datetime, language: CallLanguage) -> str:
    """The time of day as the user reads it: "밤 9시", "9:00 PM"."""
    if language.code == "ko":
        hour = moment.hour
        period = "밤" if hour >= 20 or hour < 5 else "저녁" if hour >= 17 else "오후" if hour >= 12 else "오전"
        return f"{period} {hour % 12 or 12}시" + (f" {moment.minute}분" if moment.minute else "")
    return moment.strftime("%-I:%M %p")


def when_label(moment: datetime, now: datetime, language: CallLanguage) -> str:
    tomorrow = moment.date() != now.date()
    if language.code == "ko":
        return f"{'내일' if tomorrow else '오늘'} {clock_label(moment, language)}"
    return f"{clock_label(moment, language)} {'tomorrow' if tomorrow else 'today'}"


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


def notification(offer: Offer, now: datetime, language: CallLanguage) -> dict[str, Any]:
    """The APNs payload for an offer. The app defines the buttons (CALL_OFFER, CALL_OFFER_KO)."""
    when = when_label(offer.at("suggested_at") or now, now, language)
    topic = (offer.topic or "").strip()
    if language.code == "ko":
        about = f"“{topic}” 이야기" if topic else ("새 음성 메모 이야기" if offer.kind == "voice note" else "오늘 하루 이야기")
        body, category = f"{about}, {when}에 잠깐 통화 괜찮으세요?", "CALL_OFFER_KO"
    else:
        about = f"About “{topic}”" if topic else ("About your new voice note" if offer.kind == "voice note"
                                                 else "About your day")
        body, category = f"{about}: free for a quick call at {when}?", "CALL_OFFER"
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "category": category,
                    "thread-id": "offers"},
            "offer": offer.id}
