"""A Bee conversation as short text for a model (the one behind a call, in the call prep's first message:
voice/prep.py), and how one transcription of a conversation is picked. The agents read Bee with the Bee read toolkit
(beelink/toolkit.py). Times are in the user's time zone (`zone`, from the person's settings: voice/people.py).
"""

from datetime import datetime, tzinfo
from typing import Any

# The longest a Bee read takes in a call and in the call prep: under LiveKit's 5 s wait for tools after an
# interruption, so a read is not lost (see voice/coordinator.py).
READ_TIMEOUT_S = 4.0
MAX_CHARS = 2500  # per read, so one read cannot crowd out the rest of a prompt (arbitrary)
LAST_LINES = 12  # transcript lines shown for one conversation (arbitrary)


def clock(ms: int | None, zone: tzinfo) -> str:
    if not ms:
        return "time unknown"
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%a %b %-d, %-I:%M %p")


def cap(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " (cut short)"


def conversation_line(c: dict[str, Any], zone: tzinfo) -> str:
    lines = f" ({c['utterances_count']} lines)" if c.get("utterances_count") else ""
    return (f"[conversation {c.get('id')}] {clock(c.get('start_time'), zone)}: "
            f"{c.get('short_summary') or 'no summary yet'}{lines}")


def one_transcription(conversation: Any) -> list[dict[str, Any]] | None:
    """A Bee conversation's lines from one of its transcriptions, as the Bee CLI shows them (the first that is not
    live, else the first): bee conversations transcript joins them all, so a live transcription's lines came twice
    (review of 2026-10-17, finding 3). None when the answer holds no transcriptions."""
    found = conversation.get("transcriptions") if isinstance(conversation, dict) else None
    if not isinstance(found, list):
        return None
    usable = [t for t in found if isinstance(t, dict)]
    picked = next((t for t in usable if not t.get("realtime")), usable[0] if usable else None)
    lines = picked.get("utterances") if picked else []
    return [u for u in lines or [] if isinstance(u, dict)]


def conversation_text(conversation: dict[str, Any], utterances: list[dict[str, Any]], zone: tzinfo) -> str:
    lines = [conversation_line(conversation, zone)]
    if conversation.get("summary"):
        lines.append(f"Summary: {conversation['summary']}")
    if utterances:
        lines.append("Last lines:")
        lines += [f"{u.get('speaker') or 'someone'}: {u.get('text')}" for u in utterances[-LAST_LINES:]]
    return cap("\n".join(lines))
