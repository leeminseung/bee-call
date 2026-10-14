"""Time as the user reads it.

Each person's time comes from one clock, voice/people.py Person.now(): now, in the time zone their phone sent
(Asia/Seoul until it does). The servers pass that time on, and the code here only words it or reads it back.

Neither the coordinator nor the worker knew the date in the first real call (2026-10-13): the coordinator said it
had no access to it and the worker assumed the UTC date. So every model that answers for the user is told the
time, in the user's zone (now_text).
"""

from datetime import datetime


def now_text(now: datetime) -> str:
    """For example "Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul)"; `now` is a person's time (Person.now())."""
    if now.tzinfo is None:
        raise ValueError("now_text needs a time with its time zone (Person.now())")
    hour = now.hour % 12 or 12
    return f"{now:%A, %B} {now.day}, {now.year}, {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'} ({now.tzinfo})"


def parse_time(text: str) -> datetime:
    """A stored time, with its zone. Times stored before each person had a time zone carry none: they were written
    on this machine's clock (the server's is Asia/Seoul), so they are read as such."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.astimezone()
