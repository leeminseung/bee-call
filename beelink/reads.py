"""Bee data as short text for a model: before a call (voice/prep.py) and during it (the coordinator's tools).

The reads never raise: a failed read comes back as a line that says so, and the model carries on
without it. Times are in the user's time zone (`zone`, from the person's settings: voice/people.py). Given the
person's own calls (`ours`), what Bee made of them is left out (BC-060, beelink/calls.py).
"""

import asyncio
import json
from datetime import datetime, tzinfo
from typing import Any

from beelink import cli
from beelink.calls import OurCalls

# Under LiveKit's 5 s wait for tools after an interruption, so a read is not lost (see voice/coordinator.py).
READ_TIMEOUT_S = 4.0
MAX_CHARS = 2500  # per read, so one read cannot crowd out the rest of a prompt (arbitrary)
LAST_LINES = 12  # transcript lines shown for one conversation (arbitrary)


def clock(ms: int | None, zone: tzinfo) -> str:
    if not ms:
        return "time unknown"
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%a %b %-d, %-I:%M %p")


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


def conversation_line(c: dict[str, Any], zone: tzinfo) -> str:
    lines = f" ({c['utterances_count']} lines)" if c.get("utterances_count") else ""
    return (f"[conversation {c.get('id')}] {clock(c.get('start_time'), zone)}: "
            f"{c.get('short_summary') or 'no summary yet'}{lines}")


def today_text(data: dict[str, Any], zone: tzinfo) -> str:
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
        lines.append(conversation_line(conversation, zone))
    if len(lines) == 1:
        lines.append("Nothing recorded today yet.")
    return cap("\n".join(lines))


def recent_text(conversations: list[dict[str, Any]], zone: tzinfo) -> str:
    if not conversations:
        return "Recent Bee conversations: none."
    return cap("Recent Bee conversations, newest first:\n"
               + "\n".join(conversation_line(c, zone) for c in conversations))


def conversation_text(conversation: dict[str, Any], utterances: list[dict[str, Any]], zone: tzinfo) -> str:
    lines = [conversation_line(conversation, zone)]
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


async def today(zone: tzinfo, ours: OurCalls | None = None) -> str:
    try:
        data = await cli.today_context(timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee read failed: {exc}"
    if ours is not None and isinstance(data, dict):
        data = {**data, "recentConversations": ours.conversations(data.get("recentConversations"))}
    return today_text(data, zone)


async def recent_conversations(zone: tzinfo, limit: int = 8, ours: OurCalls | None = None) -> str:
    try:
        found = await cli.conversations(limit, timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee read failed: {exc}"
    return recent_text(ours.conversations(found) if ours is not None else found, zone)


async def conversation(conversation_id: int, zone: tzinfo, ours: OurCalls | None = None) -> str:
    try:
        got, said = await asyncio.gather(cli.conversation(conversation_id, timeout=READ_TIMEOUT_S),
                                         cli.transcript(conversation_id, timeout=READ_TIMEOUT_S))
    except Exception as exc:
        return f"Bee read failed: {exc}"
    found = got.get("conversation") or {"id": conversation_id}
    if ours is not None and ours.conversation(found):
        return (f"Bee conversation {conversation_id} is left out: Bee recorded it during one of the user's calls with "
                "this assistant, and that call's own record is the source.")
    return conversation_text(found, said.get("transcript") or [], zone)


async def day(date: str) -> str:
    try:
        data = await cli.daily(date, timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee read failed: {exc}"
    summary = data.get("daily_summary") or data.get("dailySummary") or data.get("summary")
    return cap(f"Bee daily summary for {date}: {text_of(summary)}") if summary else f"No Bee daily summary for {date}."


async def search(query: str, limit: int = 3, ours: OurCalls | None = None) -> str:
    try:
        results = await cli.search_conversations(query, limit=limit, timeout=READ_TIMEOUT_S)
    except Exception as exc:
        return f"Bee search failed: {exc}"
    if ours is not None:
        results = ours.conversations(results)  # results that carry their times; Bee's search may not
    if not results:
        return "No matching Bee conversations."
    return json.dumps(results, ensure_ascii=False)[:2000]
