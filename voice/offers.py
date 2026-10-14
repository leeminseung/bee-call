"""Asking before calling. The phone rings only when the user wants it, or at a time they agreed to (Matt,
2026-10-14).

When a Bee change is worth a call, the planner's plan becomes an offer instead of a call: an app notification
such as "About “Planned a Saturday hike”: free for a 5-minute call at 9:00 PM today?" with three buttons. The AI
calls only when the user taps "Call me now", at the suggested time once the user accepted it, or at a time agreed
out loud in a call (the coordinator's book_call tool). The notification also shows on the lock screen and on
Apple Watch, and the app lists open offers on its first screen, so a missed notification can still be answered.

One offer is one file, workspace/state/offers/<id>.json, and moves through these states:
  asked -> now | booked -> calling -> called    the user said yes
  asked -> declined | expired                    no call
  booked (agreed in a call) -> calling -> called
  calling -> failed                              the call could not be placed
voice/token_server.py takes the app's answers (POST /offers/<id>) and lists open offers (GET /offers);
voice/outbound.py sends new offers and places the calls that are due.
"""

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from beelink.planner import OFFERS_DIR, Plan
from voice.language import CallLanguage

CALL_TIME = "21:00"  # the time an offer suggests when CALL_TIME is unset (arbitrary default; a setting later)
MIN_LEAD = timedelta(minutes=10)  # closer than this to today's call time, an offer suggests tomorrow (arbitrary)
LATE_BY = timedelta(minutes=10)  # a booked call this late is not placed any more (arbitrary)
STUCK = timedelta(minutes=5)  # "calling" for longer than this means the placing process died (arbitrary)
OPEN = ("asked", "now", "booked", "calling")
ANSWERS = {"now": "now", "later": "booked", "decline": "declined"}
OFFER_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")


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
    error: str | None = None
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


class OfferStore:
    def __init__(self, workspace: Path) -> None:
        self.dir = workspace / OFFERS_DIR

    def _path(self, offer_id: str) -> Path:
        if not OFFER_ID.fullmatch(offer_id or ""):
            raise KeyError(offer_id)
        return self.dir / f"{offer_id}.json"

    def save(self, offer: Offer) -> Offer:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(offer.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(offer), ensure_ascii=False, indent=1))
        tmp.replace(path)  # the token server and voice/outbound.py both write; never leave half a file
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

    def mark(self, offer: Offer, state: str, now: datetime, **changes: Any) -> Offer:
        for key, value in changes.items():
            setattr(offer, key, value)
        offer.state = state
        offer.history.append({"at": _iso(now), "state": state})
        return self.save(offer)

    def ask(self, plan: Plan, now: datetime, at: str | None = None) -> Offer:
        """A plan the planner wants to call about becomes a question."""
        suggested = call_time(now, at)
        offer = Offer(id=self._new_id(now), state="asked", reason=plan.reason, topic=plan.topic, kind=plan.kind,
                      suggested_at=_iso(suggested), expires_at=_iso(suggested),
                      inbox=str(plan.inbox_dir) if plan.inbox_dir else None,
                      plan=str(plan.path) if plan.path else None, conversation_ids=list(plan.conversation_ids),
                      history=[{"at": _iso(now), "state": "asked"}])
        return self.save(offer)

    def book(self, topic: str, call_at: datetime, now: datetime, continue_from: str | None = None) -> Offer:
        """A call the user asked for out loud, such as "call me tomorrow at 8"."""
        offer = Offer(id=self._new_id(now), state="booked", source="call", topic=topic,
                      reason=f"In a call at {now:%H:%M}, the user asked to be called back: {topic}",
                      call_at=_iso(call_at), continue_from=continue_from,
                      history=[{"at": _iso(now), "state": "booked"}])
        return self.save(offer)

    def answer(self, offer_id: str, choice: str, now: datetime) -> Offer:
        """The user's answer from a notification button or the app. KeyError: no such offer. ValueError: the
        offer is no longer waiting for an answer, or the choice is unknown."""
        if choice not in ANSWERS:
            raise ValueError(f"choice must be one of {', '.join(ANSWERS)}")
        self.expire(now)
        offer = self.get(offer_id)
        if offer is None:
            raise KeyError(offer_id)
        if offer.state == "booked" and choice == "decline":  # a booked call can still be called off
            return self.mark(offer, "declined", now)
        if offer.state != "asked":
            raise ValueError(f"the offer is {offer.state}")
        state = ANSWERS[choice]
        call_at = {"now": _iso(now), "booked": offer.suggested_at}.get(state)
        return self.mark(offer, state, now, call_at=call_at)

    def expire(self, now: datetime) -> None:
        for offer in self.all():
            if offer.state == "asked" and offer.at("expires_at") and now >= offer.at("expires_at"):
                self.mark(offer, "expired", now)
            elif offer.state == "booked" and offer.at("call_at") and now > offer.at("call_at") + LATE_BY:
                self.mark(offer, "expired", now, error="the booked time passed before it could be called")
            elif offer.state == "calling" and offer.history and \
                    now - datetime.fromisoformat(offer.history[-1]["at"]) > STUCK:
                self.mark(offer, "failed", now, error="placing the call did not finish")

    def open(self, now: datetime) -> list[Offer]:
        self.expire(now)
        return [o for o in self.all() if o.state in OPEN]

    def due(self, now: datetime) -> list[Offer]:
        """Offers to call now: the user tapped "Call me now", or a booked time has come."""
        self.expire(now)
        return [o for o in self.all() if o.state in ("now", "booked") and o.at("call_at") and o.at("call_at") <= now]


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
    if offer.state in ("now", "calling") and offer.call_at == said:
        return f"the user tapped \"Call me now\" on your notification at {when}"
    return f"at {when} the user accepted the time your notification suggested"


def when_label(moment: datetime, now: datetime, language: CallLanguage) -> str:
    tomorrow = moment.date() != now.date()
    if language.code == "ko":
        hour = moment.hour
        period = "밤" if hour >= 20 or hour < 5 else "저녁" if hour >= 17 else "오후" if hour >= 12 else "오전"
        clock = f"{hour % 12 or 12}시" + (f" {moment.minute}분" if moment.minute else "")
        return f"{'내일' if tomorrow else '오늘'} {period} {clock}"
    clock = moment.strftime("%-I:%M %p")
    return f"{clock} {'tomorrow' if tomorrow else 'today'}"


def notification(offer: Offer, now: datetime, language: CallLanguage) -> dict[str, Any]:
    """The APNs payload for an offer. The app defines the buttons (CALL_OFFER, CALL_OFFER_KO)."""
    when = when_label(offer.at("suggested_at") or now, now, language)
    topic = (offer.topic or "").strip()
    if language.code == "ko":
        about = f"“{topic}” 이야기" if topic else ("새 음성 메모 이야기" if offer.kind == "voice note" else "오늘 하루 이야기")
        body, category = f"{about}, {when}에 5분 통화 괜찮으세요?", "CALL_OFFER_KO"
    else:
        about = f"About “{topic}”" if topic else ("About your new voice note" if offer.kind == "voice note"
                                                 else "About your day")
        body, category = f"{about}: free for a 5-minute call at {when}?", "CALL_OFFER"
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "category": category,
                    "thread-id": "offers"},
            "offer": offer.id}
