"""The user's local time, for words like "today" and "this Saturday".

Neither the coordinator nor the worker knew the date in the first real call (2026-10-13):
the coordinator said it had no access to it and the worker assumed the UTC date.
Bee reports the account's time zone (`bee me`, `bee changed` meta); BEE_TIMEZONE overrides it.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

USER_TIMEZONE = ZoneInfo(os.environ.get("BEE_TIMEZONE", "Asia/Seoul"))


def local_now() -> datetime:
    return datetime.now(USER_TIMEZONE)


def now_text(now: datetime | None = None) -> str:
    """For example "Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul)"."""
    now = now or local_now()
    hour = now.hour % 12 or 12
    return f"{now:%A, %B} {now.day}, {now.year}, {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'} ({now.tzinfo})"
