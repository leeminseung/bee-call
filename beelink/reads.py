"""Bee data as short text for a model: before a call (voice/prep.py) and during it (the coordinator's tools).

The reads never raise: a failed read comes back as a line that says so, and the model carries on
without it. Times are in the user's time zone.
"""

import asyncio
import json
from datetime import datetime
from typing import Any

from beelink import cli
from beelink.clock import USER_TIMEZONE

# Under LiveKit's 5 s wait for tools after an interruption, so a read is not lost (see voice/coordinator.py).
READ_TIMEOUT_S = 4.0
MAX_CHARS = 2500  # per read, so one read cannot crowd out the rest of a prompt (arbitrary)
LAST_LINES = 12  # transcript lines shown for one conversation (arbitrary)


def clock(ms: int | None) -> str:
    if not ms:
        return "time unknown"
    return datetime.fromtimestamp(ms / 1000, USER_TIMEZONE).strftime("%a %b %-d, %-I:%M %p")


def text_of(item: Any) -> str:
    """The readable text of a Bee item whose exact shape varies (a summary, a todo, a note, an event)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("text", "summary", "short_summary", "content", "title", "name"):
            if isinstance(item.get(key), str) and item[key].strip():
                return item[key]
        return json.dumps(item, ensure_ascii=False)
    return str(item)


def cap(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " (cut short)"


def conversation_line(c: dict[str, Any]) -> str:
    lines = f" ({c['utterances_count']} lines)" if c.get("utterances_count") else ""
    return f"[conversation {c.get('id')}] {clock(c.get('start_time'))}: {c.get('short_summary') or 'no summary yet'}{lines}"


def today_text(data: dict[str, Any]) -> str:
    lines = [f"Today ({data.get('date') or 'date unknown'}) in Bee:"]
    if data.get("dailySummary"):
        lines.append(f"Daily summary: {text_of(data['dailySummary'])}")
    # Calendar events only: the brief's emails are not what a call is about, and they can be sensitive.
    for event in (data.get("todayBrief") or {}).get("calendar_events") or []:
        lines.append(f"Calendar: {text_of(event)}")
    for todo in data.get("activeTodos") or []:
        lines.append(f"[todo {todo.get('id')}] {text_of(todo)}")
    for note in data.get("recentNotes") or []:
        lines.append(f"[note {note.get('id')}] {text_of(note)}")
    for conversation in data.get("recentConversations") or []:
        lines.append(conversation_line(conversation))
    if len(lines) == 1:
        lines.append("Nothing recorded today yet.")
    return cap("\n".join(lines))


def recent_text(conversations: list[dict[str, Any]]) -> str:
    if not conversations:
        return "Recent Bee conversations: none."
    return cap("Recent Bee conversations, newest first:\n" + "\n".join(conversation_line(c) for c in conversations))


def conversation_text(conversation: dict[str, Any], utterances: list[dict[str, Any]]) -> str:
    lines = [conversation_line(conversation)]
    if conversation.get("summary"):
        lines.append(f"Summary: {conversation['summary']}")
    if utterances:
        lines.append("Last lines:")
        lines += [f"{u.get('speaker') or 'someone'}: {u.get('text')}" for u in utterances[-LAST_LINES:]]
    return cap("\n".join(lines))


def facts_text(facts: list[dict[str, Any]]) -> str:
    return "\n".join(f"[unconfirmed fact {f.get('id')}] {f.get('text')}" for f in facts[:10])


def suggestions_text(suggestions: list[dict[str, Any]]) -> str:
    return "\n".join(f"[todo suggestion {s.get('id')}] {s.get('text')}" for s in suggestions[:10])


async def today() -> str:
    try:
        return today_text(await cli.today_context(timeout=READ_TIMEOUT_S))
    except Exception as exc:
        return f"Bee read failed: {exc}"


async def recent_conversations(limit: int = 8) -> str:
    try:
        return recent_text(await cli.conversations(limit, timeout=READ_TIMEOUT_S))
    except Exception as exc:
        return f"Bee read failed: {exc}"


async def conversation(conversation_id: int) -> str:
    try:
        got, said = await asyncio.gather(cli.conversation(conversation_id, timeout=READ_TIMEOUT_S),
                                         cli.transcript(conversation_id, timeout=READ_TIMEOUT_S))
    except Exception as exc:
        return f"Bee read failed: {exc}"
    return conversation_text(got.get("conversation") or {"id": conversation_id}, said.get("transcript") or [])


async def day(date: str) -> str:
    try:
        data = await cli.daily(date, timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee read failed: {exc}"
    summary = data.get("daily_summary") or data.get("dailySummary") or data.get("summary")
    return cap(f"Bee daily summary for {date}: {text_of(summary)}") if summary else f"No Bee daily summary for {date}."


async def search(query: str, limit: int = 3) -> str:
    try:
        results = await cli.search_conversations(query, limit=limit, timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee search failed: {exc}"
    if not results:
        return "No matching Bee conversations."
    return json.dumps(results, ensure_ascii=False)[:2000]
