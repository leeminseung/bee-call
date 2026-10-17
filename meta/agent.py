"""The meta agent: Bee Call's background coordinator between calls.

It wakes only for events (a Bee change, a call that ended, an AI call missed or declined, the user's answer to an
offer, a booked call coming up), looks at what changed with read-only tools (the workspace's files and Bee), and
decides the user's next step.
It only proposes: a call offer is a question the user answers, its notes (state.md) are for the next call, and
Bee changes wait for the user's yes in a call. Every time it thinks, the events, each tool it used and its own
words go into the meta session (meta/session.py), which the app shows as they are.

One think() is one conversation with the model catalog's meta role (voice/model_catalog.py: Claude Sonnet 5.5, GLM 5
on Bedrock standing by), through its provider's adapter (voice/providers/), with at most MAX_ROUNDS model requests.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from beelink import cli, toolkit
from beelink.writer import BeeWrites
from beelink.reads import one_transcription
from beelink.calls import OurCalls
from beelink.clock import now_text
from beelink.planner import Plan
from meta import files
from meta.session import DAILY_NOTIFICATIONS, META_DIR, MetaSession, quiet, quiet_ends
from voice.offers import AlreadyOffered, Offer, OfferStore, QuestionWaiting
from voice.people import DEFAULT_CALL_TIME
from voice import model_catalog, providers
from voice.call_models import coordinator_llm
from voice.language import LANGUAGES
from voice.model_catalog import Role
from voice.providers import ToolResult, ToolSpec, Turn, Usage
from voice.said import TAKEN_BACK, said_in

log = logging.getLogger("bee-call.meta")

MAX_ROUNDS = 8  # model requests in one think (arbitrary)
MAX_RESULT_CHARS = 8000  # one tool result as the model sees it
META_READ_S = 15.0  # the longest one Bee read may take while the meta agent thinks (arbitrary)
ORGANIZE_ROUNDS = 14  # model requests in an organize run, which reads several days and adds item by item (arbitrary)
ORGANIZE_ADDS = 15  # items an organize run adds at most (arbitrary)
ORGANIZE_PAGES = 4  # pages of Bee's facts (in each list) and to-dos read to find what Bee has already (arbitrary)

# Its instructions, in sections (2026-10-18): for a person who connected Bee, three of them shared word for word with
# the call AI and the call prep (beelink/toolkit.py); for a person without Bee, nothing about Bee. The rules about
# offers and memos keep the wording measured on 2026-10-17.
WHO_YOU_ARE = """\
## Who you are
You are the meta agent of Bee Call, a voice assistant the user talks with on the phone. You never talk to the user \
yourself. You wake up when something changes in Bee or a call ends, look at what changed, and decide the user's next \
step. You only propose: nothing happens until the user says yes, and the user asking you for a call, in a voice memo \
or in a conversation Bee recorded, is that yes. You never change Bee, except when the user said yes to organizing \
their Bee memory (below)."""
WHO_YOU_ARE_WITHOUT_BEE = """\
## Who you are
You are the meta agent of Bee Call, a voice assistant the user talks with on the phone. You never talk to the user \
yourself. You wake up when something happens, look at what changed, and decide the user's next step. You only \
propose: nothing happens until the user says yes."""
BESIDES_BEE = """\
Besides Bee: list_files, read_file and grep over the workspace. Each call is recorded in <date>/<time>/ \
(transcript.md, prep.md, events.jsonl, jobs/); each Bee change the watcher saw is in inbox/<time>/changed.json; your \
own past entries are in meta/session.jsonl."""
LOOKING_THINGS_UP = """\
## Looking things up
Look things up when you need to: list_files, read_file and grep over the workspace. Each call is recorded in \
<date>/<time>/ (transcript.md, prep.md, events.jsonl, jobs/); your own past entries are in meta/session.jsonl."""
OFFER_CALL = """\
- offer_call: ask the user, with a notification, whether they want a short call about one topic, at the evening call \
time or right now. Offer a call only when talking would clearly help: a decision to make, something to sort out. \
Never for small talk, never about something the user turned down. Never offer a call because a background job \
finished or stopped after its call ended: the user continues that call from its record when they want to, so put the \
job's result on the agenda instead. One question at a time: while the user has not answered an offer, put new topics \
on the agenda instead."""
DECIDING = f"""\
## Deciding whether to call
{OFFER_CALL}
- book_call: book the call the user asks you for, in a voice memo or in a conversation Bee recorded, at the time they \
ask; the user gets one notification that says so, and can call it off in the app.
- Doing nothing is often the right call. Then say so.
When the user asks you to call them, in a voice memo or in a conversation Bee recorded (a note they record on their \
watch comes as a conversation), that is their yes: book the call with book_call at the time they ask, copying their \
own words that ask for it. A request counts when the wearer asks you, the assistant, to call them: "오늘 밤 9시에 \
전화해서 같이 정리하자", "call me at nine tonight". Words they say to another person ("나중에 전화해" to a friend), or \
a time mentioned without asking for a call, do not. In a conversation's lines Bee names the wearer, the user, and \
marks other speakers "Unknown", though it sometimes marks the wearer "Unknown" too: judge by what is said and to \
whom. When they ask for a call now or soon, offer one for now with offer_call, naming the memo or the conversation. \
When nothing asks for a call, decide as for any other change. A voice memo is the user speaking to you on purpose; \
Bee's reply to it shows what Bee made of it."""
DECIDING_WITHOUT_BEE = f"""\
## Deciding whether to call
{OFFER_CALL}
- Doing nothing is often the right call. Then say so."""
NOTES_FOR_THE_NEXT_CALL = """\
## Notes for the next call
write_state rewrites your notes, state.md. The next call's prep reads them first, so they are how what you learned \
reaches the call. Keep: the open threads, each with where it came from; the agenda for the next call; what the user \
turned down; what you learned the user prefers; and from Bee, what is worth bringing up next time: an insight worth \
talking about, a kept fact the next topic may touch, unconfirmed facts and suggestions to check, with their Bee ids; \
and what is worth getting to know about the user next: a gap in what Bee keeps, two facts that disagree, an insight \
the user might want to talk through. Changes to Bee go on the agenda too: the call's AI makes them in a call, after \
the user agrees."""
NOTES_WITHOUT_BEE = """\
## Notes for the next call
write_state rewrites your notes, state.md. The next call's prep reads them first, so they are how what you learned \
reaches the call. Keep: the open threads, each with where it came from; the agenda for the next call; what the user \
turned down; what you learned the user prefers; and what is worth getting to know about the user next."""
ORGANIZING = """\
## Organizing Bee's memory, only when asked
When the user said yes in a call to organizing what Bee recorded over the past days, the app wakes you with that \
request and the days to cover. Then read those days in Bee, pick what is worth keeping (lasting facts about the \
user, to-dos they mean to do), check each against what Bee already has (its facts, confirmed or not, its to-dos and \
the to-dos it suggests) so nothing is added twice, and add them with bee_add_fact and bee_add_todo, each one short \
statement in English. Never delete, merge away or rewrite what Bee \
already has. Finish with one sentence for the user's notification: how many you added, and that they can see and \
edit them in the Bee app. At any other time you have no tools that change Bee."""
FINISHING = """\
## Finishing
Finish with a few sentences for the user, who reads them in the app's timeline exactly as you write them: what you \
noticed, and what you did or why you did nothing. Plain words, no lists. The user's language is {language}: write \
offer_call's topic and why in {language}. The topic is the question's title on their screen; why is said to the \
user, as you would tell them why you would call (it shows on the call's card and the call's AI reads it)."""


def instructions(bee: bool, language: str = "English") -> str:
    """The meta agent's instructions for a person who connected Bee (`bee`) or not, the user's language named."""
    if bee:
        sections = [WHO_YOU_ARE, toolkit.WHAT_BEE_KNOWS, toolkit.reading_bee("meta") + "\n" + BESIDES_BEE, DECIDING,
                    NOTES_FOR_THE_NEXT_CALL, ORGANIZING, FINISHING, toolkit.INFORMATION]
    else:
        sections = [WHO_YOU_ARE_WITHOUT_BEE, LOOKING_THINGS_UP, DECIDING_WITHOUT_BEE, NOTES_WITHOUT_BEE, FINISHING,
                    toolkit.INFORMATION_WITHOUT_BEE]
    return "\n\n".join(sections).replace("{language}", language)


INSTRUCTIONS = instructions(bee=True)  # a person who connected Bee, writing in English


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> ToolSpec:
    schema = {"type": "object", "properties": properties, "required": required if required is not None else list(properties)}
    return ToolSpec(name, description, schema)


TOOLS = [
    _tool("list_files", "Files and folders in a workspace folder, newest first.",
          {"path": {"type": "string", "description": "Folder, relative to the workspace. Default \".\"."},
           "pattern": {"type": "string", "description": "Name pattern like *.md. Default *."}}, []),
    _tool("read_file", "Lines of a text file in the workspace, numbered.",
          {"path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}},
          ["path"]),
    _tool("grep", "Lines matching a regular expression (case-insensitive) in workspace files.",
          {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Folder or file. Default \".\"."},
           "glob": {"type": "string", "description": "File name pattern like transcript.md. Default *."}},
          ["pattern"]),
    *(_tool(t.name, t.description, t.parameters["properties"], t.parameters["required"]) for t in toolkit.TOOLS),
    _tool("write_state", "Replace state.md, your notes, with this markdown.", {"text": {"type": "string"}}),
    _tool("offer_call", "Ask the user, with a notification, whether they want a short call about one topic.",
          {"topic": {"type": "string", "description": "What the call is about, in a few words, in the user's language."},
           "why": {"type": "string", "description": "Why a call would help now, in one or two sentences in the user's "
                                                    "language, said to the user."},
           "conversation_ids": {"type": "array", "items": {"type": "integer"},
                                "description": "The Bee conversations behind it, if any."},
           "inbox": {"type": "string", "description": "The inbox/<time> folder of the Bee change, if any."},
           "memo_id": {"type": "string", "description": "The voice memo behind it, if any (one memo, one call)."}},
          ["topic", "why"]),
    _tool("book_call", "Book the call the user asks you for, in a voice memo or in a conversation Bee recorded, at "
                       "the time they ask. The AI calls then; the user gets one notification that says so and can "
                       "call it off in the app.",
          {"memo_id": {"type": "string", "description": "The voice memo's id, when a memo asks for the call."},
           "conversation_id": {"type": "integer", "description": "The Bee conversation's id, when the user asks for "
                                                                 "the call in a conversation."},
           "asked": {"type": "string", "description": "The user's own words asking for the call, copied word for word "
                                                      "from the memo or from the wearer's lines: their sentence, or "
                                                      "most of it."},
           "at": {"type": "string", "description": "When to call: the user's local date and time in ISO 8601, like "
                                                   "2026-10-17T21:00."},
           "topic": {"type": "string", "description": "What the call is about, in a few words, in the user's language."},
           "why": {"type": "string", "description": "Why you call then, in one sentence in the user's language, said "
                                                    "to the user."}},
          ["asked", "at", "topic", "why"]),
]
# offer_call for a person without Bee: no Bee conversations or Bee changes to name.
OFFER_CALL_WITHOUT_BEE = _tool(
    "offer_call", "Ask the user, with a notification, whether they want a short call about one topic.",
    {"topic": {"type": "string", "description": "What the call is about, in a few words, in the user's language."},
     "why": {"type": "string", "description": "Why a call would help now, in one or two sentences in the user's "
                                              "language, said to the user."}})

BEE_TOOLS = (*toolkit.NAMES, "book_call")  # for Bee people only; all but book_call read only
# The only tools that change Bee, offered only in a run the user said yes to in a call (organize_bee): each adds one
# item, never deletes or rewrites one.
ADD_TOOLS = [
    _tool("bee_add_fact", "Add one fact about the user to Bee, as a confirmed fact: one short statement in English, "
                          "that Bee does not have yet.",
          {"text": {"type": "string", "description": "The fact, one short statement in English."}}),
    _tool("bee_add_todo", "Add one to-do to Bee, open: one short statement in English, that Bee does not have yet.",
          {"text": {"type": "string", "description": "The to-do, one short statement in English."}}),
]
ORGANIZE_TOOLS = ("list_files", "read_file", "grep", *toolkit.NAMES)  # what else an organize run has: it reads
Send = Callable[[Offer, Path, datetime], Awaitable[str]]
Notify = Callable[[dict[str, Any]], Awaitable[str]]  # sends a plain notification; says what happened, in words
MAX_TOPIC, MAX_WHY = 80, 300  # characters


def clean(value: Any, limit: int) -> str:
    """One line of plain text, at most `limit` characters."""
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


MEMO_CHARS = 1500  # of a voice memo's text, in the agent's first message
MEMO_READ_S = 8.0  # one memo read from Bee before a think (arbitrary)
MEMOS_SHOWN = 3  # voice memos of one Bee change shown to the agent, and read from Bee for it (arbitrary)
# A memo recorded longer ago than this books no call: "tonight" in it meant another night (review of 2026-10-17,
# finding 2: a memo four days old, back in a later batch, booked a call for tonight). Arbitrary.
MEMO_BOOK_HOURS = 12
# A conversation's lines shown to the agent: those spoken since the change before that had the conversation, the
# newest, with Bee's speaker names and their times (2026-10-17: a note recorded on the watch comes as a conversation,
# and a call asked for in it is booked as one a memo asks for). Arbitrary.
CONVERSATIONS_READ = 3  # conversations of one think whose lines are read
LINES_SHOWN, LINES_CHARS = 20, 1500  # of one conversation's lines
EARLIER_BATCHES = 50  # earlier changes looked through for when a conversation was last shown
TAKE_BACK_LINES, TAKE_BACK_S = 3, 120  # the lines after a request, and how soon, in which a take-back cancels it


def memo_ids(workspace: Path, events: list[dict[str, Any]]) -> list[str]:
    """The voice memos (Bee journals) the Bee changes among these events name, from their saved batches."""
    found: list[str] = []
    for event in events:
        if event.get("kind") != "bee_change":
            continue
        try:
            changed = json.loads((workspace / str(event.get("inbox")) / "changed.json").read_text())
        except (OSError, ValueError):
            continue
        for journal in (changed.get("journals") or [])[:MEMOS_SHOWN]:
            if isinstance(journal, dict) and journal.get("id") is not None and str(journal["id"]) not in found:
                found.append(str(journal["id"]))
    return found


def recorded(memo: dict[str, Any], now: datetime) -> datetime | None:
    """When a voice memo was recorded, on the person's clock: Bee's epoch milliseconds, or seconds, or an ISO time,
    as a number or a string; None if it does not say (second review of 2026-10-17, finding 6)."""
    made = memo.get("created_at")
    if isinstance(made, bool):
        return None
    if isinstance(made, str):
        text = made.strip()
        try:
            made = float(text)
        except ValueError:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return (moment if moment.tzinfo else moment.replace(tzinfo=now.tzinfo)).astimezone(now.tzinfo)
    if not isinstance(made, (int, float)) or made <= 1e9:  # as the Bee CLI: no time (a line's start in seconds)
        return None
    try:
        return datetime.fromtimestamp(made / 1000 if made > 1e11 else made, now.tzinfo)  # milliseconds, or seconds
    except (OverflowError, OSError, ValueError):
        return None


def batch_memo(workspace: Path, events: list[dict[str, Any]], memo_id: str) -> dict[str, Any]:
    """The watcher's copy of a voice memo (its saved batch), or {}."""
    for event in events:
        if event.get("kind") != "bee_change":
            continue
        try:
            changed = json.loads((workspace / str(event.get("inbox")) / "changed.json").read_text())
        except (OSError, ValueError):
            continue
        for journal in changed.get("journals") or []:
            if isinstance(journal, dict) and str(journal.get("id")) == memo_id:
                return journal
    return {}


def ago(then: datetime, now: datetime) -> str:
    """How long ago, in words: "1 minute ago", "3 hours ago", "4 days ago"."""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' * (minutes != 1)} ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hour{'s' * (minutes // 60 != 1)} ago"
    return f"{minutes // 1440} days ago"


def spoken_at(utterance: dict[str, Any], now: datetime) -> datetime | None:
    """When a line of a Bee conversation was said (spoken_at, else start), on the person's clock."""
    return recorded({"created_at": utterance.get("spoken_at") or utterance.get("start")}, now)


def last_shown(workspace: Path, conv_id: Any, inbox: str) -> datetime | None:
    """Up to when the latest change before `inbox` that had this conversation held its lines: its end_time, or,
    for a conversation still recording then, that change's own time (its folder's name, the watcher's local clock);
    None if no earlier change had it. Lines spoken after that are new."""
    root = workspace / "inbox"
    current = Path(str(inbox)).name
    try:
        earlier = sorted((p for p in root.iterdir() if p.is_dir() and p.name < current), reverse=True)
    except OSError:
        return None
    for folder in earlier[:EARLIER_BATCHES]:
        try:
            changed = json.loads((folder / "changed.json").read_text())
        except (OSError, ValueError):
            continue
        for c in changed.get("conversations") or []:
            if isinstance(c, dict) and c.get("id") == conv_id:
                ended = recorded({"created_at": c.get("end_time")}, datetime.now().astimezone())
                if ended is not None:
                    return ended
                try:  # the watcher names a change's folder by its local time (beelink/watcher.py catch_up)
                    return datetime.strptime(folder.name, "%Y-%m-%d-%H%M%S").astimezone()
                except ValueError:
                    return None
    return None


def _conversation_lines(seen: dict[str, Any], now: datetime) -> str:
    """A conversation's new lines, as the agent reads them: "[18:28] Minseung: ..."."""
    lines = seen.get("lines") or []
    text = ""
    if seen.get("earlier"):
        text += f"\n    ({seen['earlier']} earlier lines were in an earlier change)"
    if seen.get("more"):
        text += f"\n    ({seen['more']} more new lines before these are not shown)"
    if seen.get("ours"):
        text += f"\n    ({seen['ours']} lines said during your calls with the user are left out: those calls' records hold them)"
    if not lines:
        return text + "\n    no new lines"
    shown = []
    for u in lines:
        when = spoken_at(u, now)
        stamp = "" if when is None else f"[{when:%H:%M}] " if when.date() == now.date() else f"[{when:%m-%d %H:%M}] "
        shown.append(f"\n    {stamp}{u.get('speaker') or 'Unknown'}: {' '.join(str(u.get('text') or '').split())}")
    return text + "".join(shown)


def _memo_lines(memo: dict[str, Any], now: datetime | None = None) -> str:
    text = str(memo.get("text") or memo.get("transcript") or "").strip()
    made = recorded(memo, now) if now is not None else None
    when = f" (recorded {made:%A, %B} {made.day} at {made:%H:%M}, {ago(made, now)})" if made and now else \
        " (recorded at a time Bee did not give)" if now is not None else ""
    line = f"\n  voice memo {memo.get('id')}{when}: \"{text[:MEMO_CHARS]}\""
    reply = memo.get("aiResponse") if isinstance(memo.get("aiResponse"), dict) else {}
    if reply.get("message"):
        line += f"\n    Bee's reply to it: {reply['message']}"
    if reply.get("todos"):
        line += f"\n    Bee's to-dos from it: {', '.join(str(t) for t in reply['todos'][:5])}"
    return line


def _event_line(workspace: Path, event: dict[str, Any], memos: dict[str, dict[str, Any]] | None = None,
                now: datetime | None = None, conversations: dict[Any, dict[str, Any]] | None = None) -> str:
    """One event as the agent reads it, with the Bee change's essentials so it need not open the file for them: a
    voice memo whole, as Bee has it now (`memos`, read with bee journals get), with Bee's reply to it."""
    kind, at = event.get("kind"), event.get("at")
    if kind == "bee_change":
        line = f"[{at}] Bee change, saved in {event.get('inbox')}"
        try:
            changed = json.loads((workspace / str(event.get("inbox")) / "changed.json").read_text())
        except (OSError, ValueError):
            return line
        kept = set(event.get("conversation_ids") or [])  # the watcher leaves out what Bee heard of our own calls
        for c in [c for c in changed.get("conversations", []) if not kept or c.get("id") in kept][:6]:
            line += (f"\n  conversation {c.get('id')} ({c.get('utterances_count') or '?'} utterances, "
                     f"{c.get('state', '')}): {c.get('short_summary') or 'no summary yet'}")
            seen = (conversations or {}).get(c.get("id"))
            if seen is not None and now is not None:
                line += _conversation_lines(seen, now)
        for j in changed.get("journals", [])[:MEMOS_SHOWN]:
            line += _memo_lines((memos or {}).get(str(j.get("id"))) or j, now)
        others = {k: len(v) for k, v in changed.items() if isinstance(v, list) and k not in ("conversations", "journals") and v}
        return line + (f"\n  also changed: {others}" if others else "")
    if kind == "call_ended":
        return f"[{at}] A call ended: {event.get('call')} ({event.get('direction', '?')}); record in {event.get('call')}/"
    if kind == "job_ended":  # a background job that outlived its call (voice/agent.py job_end_alert)
        return (f"[{at}] A background job of call {event.get('call')} ended after the call: \"{event.get('title')}\" "
                f"({event.get('outcome')}); its result is in {event.get('call')}/jobs/{event.get('job')}/")
    if kind == "offer_answered":
        return f"[{at}] The user answered offer {event.get('offer')} about \"{event.get('topic')}\": {event.get('choice')}"
    if kind == "call_missed":
        return f"[{at}] An AI call was not answered: \"{event.get('topic')}\""
    if kind == "call_declined":
        return f"[{at}] The user declined an AI call while it rang: \"{event.get('topic')}\""
    if kind == "booked_soon":
        return f"[{at}] A booked call is coming up at {event.get('call_at')}: \"{event.get('topic')}\""
    return f"[{at}] {kind}: {json.dumps({k: v for k, v in event.items() if k not in ('at', 'kind')}, ensure_ascii=False)}"


def context(workspace: Path, events: list[dict[str, Any]], now: datetime, call_time: str | None = None,
            memos: dict[str, dict[str, Any]] | None = None,
            conversations: dict[Any, dict[str, Any]] | None = None) -> str:
    """The first message: the time and limits, open offers, the agent's notes and past entries, and the events."""
    session, store = MetaSession(workspace), OfferStore(workspace)
    left = max(0, DAILY_NOTIFICATIONS - store.notified_on(now))
    limits = f"Notifications left today: {left} of {DAILY_NOTIFICATIONS}."
    if quiet(now):
        limits += f" It is quiet time: a notification waits until {quiet_ends(now):%H:%M}."
    offers = [f"- {o.id} {o.state}: \"{o.topic}\"" + (f", call at {o.call_at}" if o.call_at else "")
              + (f", suggested {o.suggested_at}" if o.state == "asked" else "") for o in store.open(now)]
    return "\n\n".join([
        f"It is now {now_text(now)}. The evening call time is {call_time or DEFAULT_CALL_TIME}. {limits}",
        "Open offers:\n" + ("\n".join(offers) if offers else "none"),
        "Your notes (state.md):\n" + session.state(),
        "Your recent entries (meta session):\n" + session.recent_text(),
        "What just happened:\n" + "\n".join(_event_line(workspace, e, memos, now, conversations) for e in events),
    ])


class Thinker:
    """One think(): a tool loop with the meta role's model. `role` and `send` can be stand-ins in tests."""

    def __init__(self, workspace: Path, role: Role | None = None, send: Send | None = None,
                 call_time: str | None = None, *, bee: bool, language: str = "en", tell: Send | None = None,
                 notify: Notify | None = None):
        self.workspace = workspace
        # The person uses Bee: the Bee tools and the words about Bee. Without Bee, neither (BC-057).
        self.bee = bee
        self.tools = TOOLS if bee else [OFFER_CALL_WITHOUT_BEE if t.name == "offer_call" else t
                                         for t in TOOLS if t.name not in BEE_TOOLS]
        # An organize run (organize_bee): it reads, and it adds to Bee; it offers or books no call.
        self.organize_tools = [t for t in TOOLS if t.name in ORGANIZE_TOOLS] + ADD_TOOLS
        # What the person reads (an offer's topic and why) is in their language (`language`, their setting).
        self.instructions = instructions(bee, LANGUAGES.get(language, LANGUAGES["en"]).name)
        # The meta role as the COORDINATOR_LLM mode runs it (model_catalog.mode_role).
        self.role = role or model_catalog.mode_role("meta", coordinator_llm())
        self.send = send
        self.tell = tell  # the one plain notification for a call booked from a Bee input (a memo or a conversation)
        self.notify = notify  # a plain notification: the one at the end of an organize run
        self._adding: Adding | None = None  # the organize run in progress: its request and what it added
        self._memos: dict[str, dict[str, Any]] = {}  # the memos of this think's events, as Bee has them now
        # The conversations of this think's events whose lines were read: {"conversation": as the change had it,
        # "lines": the lines shown, "earlier": how many earlier ones were left out}.
        self._conversations: dict[Any, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self.call_time = call_time  # the evening time an offer suggests (the person's setting; the default when None)
        self.language = language if language in LANGUAGES else "en"

    async def _tool(self, name: str, args: dict[str, Any], now: datetime, actions: list[dict[str, Any]]) -> str:
        ws = self.workspace
        if name in BEE_TOOLS and not self.bee:
            return f"There is no tool named {name}."
        try:
            if name == "list_files":  # file tools run in a thread, so a slow one never stalls placing calls
                return await asyncio.to_thread(files.list_files, ws, str(args.get("path") or "."),
                                               str(args.get("pattern") or "*"))
            if name == "read_file":
                return await asyncio.to_thread(files.read_file, ws, str(args["path"]), int(args.get("start_line") or 1),
                                               int(args.get("max_lines") or 200))
            if name == "grep":
                return await asyncio.to_thread(files.grep, ws, str(args["pattern"]), str(args.get("path") or "."),
                                               str(args.get("glob") or "*"))
            if name in toolkit.NAMES:
                found = await toolkit.read(name, args, ours=OurCalls.of(ws), zone=now.tzinfo, timeout=META_READ_S,
                                           max_chars=MAX_RESULT_CHARS - 200)
                return found.text
            if name in ("bee_add_fact", "bee_add_todo"):
                return await self._add("fact_create" if name == "bee_add_fact" else "todo_create", args, actions)
            if name == "book_call":
                return await self._book_call(args, now, actions)
            if name == "write_state":
                MetaSession(ws).write_state(str(args.get("text", "")))
                actions.append({"tool": "write_state", "result": "notes updated"})
                return "Saved."
            if name == "offer_call":
                return await self._offer_call(args, now, actions)
        except files.FileError as exc:
            return f"Error: {exc}"
        except (KeyError, TypeError, ValueError) as exc:
            return f"Error: bad arguments for {name}: {exc}"
        return f"There is no tool named {name}."

    async def _offer_call(self, args: dict[str, Any], now: datetime, actions: list[dict[str, Any]]) -> str:
        topic, why = clean(args.get("topic"), MAX_TOPIC), clean(args.get("why"), MAX_WHY)  # topic shows on the lock screen
        if not topic or not why:
            return "Error: offer_call needs a topic and a why."
        store = OfferStore(self.workspace)
        if any(o.topic == topic for o in store.open(now)):
            return f"An offer about \"{topic}\" is already open; not asked again."
        inbox = str(args.get("inbox") or "").strip() if self.bee else ""  # without Bee there is no Bee change
        inbox_dir = files._inside(self.workspace, inbox) if inbox else None  # never outside the workspace
        ids = [int(i) for i in args.get("conversation_ids") or []] if self.bee else []
        memo = str(args.get("memo_id") or "").strip() if self.bee else ""
        plan = Plan(call=True, reason=why, inbox_dir=inbox_dir, conversation_ids=ids, topic=topic,
                    kind="voice note" if memo else "conversation" if ids else None, memo_ids=[memo] if memo else [])
        try:
            offer = store.ask(plan, now, at=self.call_time)
        except QuestionWaiting as exc:
            return f"Not asked: {exc}. Put this on the agenda instead, or offer it after they answer."
        except AlreadyOffered as exc:
            return (f"Not asked: {exc}; one Bee conversation or voice memo leads to at most one offer. Put it on the "
                    "agenda for the next call instead.")
        sent = await self.send(offer, self.workspace, now) if self.send else "not sent (no sender)"
        actions.append({"tool": "offer_call", "offer": offer.id, "topic": topic, "result": sent})
        return f"Asked: offer {offer.id}, suggesting {offer.suggested_at}. {sent}"

    async def _book_call(self, args: dict[str, Any], now: datetime, actions: list[dict[str, Any]]) -> str:
        """The call the user asks for, in a voice memo or in a conversation Bee recorded, at the time they ask.
        Checked in code like book_call in a call: the input's own words, the user's, must ask for it
        (voice/said.py), the input is recent and not Bee's recording of one of our calls, and the time is in the
        coming week; one memo or conversation books at most one call."""
        memo_id = str(args.get("memo_id") or "").strip()
        conv_id = args.get("conversation_id")
        if bool(memo_id) == (conv_id is not None and str(conv_id).strip() != ""):
            return ("Not booked: give one of memo_id and conversation_id: the voice memo or the Bee conversation in "
                    "which the user asks for the call.")
        asked = args.get("asked")
        refused = self._memo_asks(memo_id, asked, now) if memo_id else self._conversation_asks(conv_id, asked, now)
        if refused:
            return refused
        try:
            when = datetime.fromisoformat(str(args.get("at") or "").strip())
        except ValueError:
            return "Not booked: give at in ISO 8601, like 2026-10-17T21:00."
        when = when.replace(tzinfo=now.tzinfo) if when.tzinfo is None else when.astimezone(now.tzinfo)
        if when < now + timedelta(minutes=1):
            return "Not booked: that time has passed. For a call now or soon, offer one with offer_call."
        if when > now + timedelta(days=7):
            return "Not booked: calls can be booked at most a week ahead."
        topic, why = clean(args.get("topic"), MAX_TOPIC), clean(args.get("why"), MAX_WHY)
        if not topic or not why:
            return "Not booked: book_call needs a topic and a why."
        inbox = next((e.get("inbox") for e in self._events if e.get("kind") == "bee_change"), None)
        source = f"voice memo {memo_id}" if memo_id else f"Bee conversation {conv_id}"
        try:
            offer, joined = OfferStore(self.workspace).book_from_input(
                topic, when, now, reason=why, asked=clean(asked, MAX_WHY), memo_id=memo_id or None,
                conversation_id=None if memo_id else int(conv_id),
                inbox=str(files._inside(self.workspace, str(inbox))) if inbox else None)
        except AlreadyOffered as exc:
            return f"Not booked: {exc}."
        if joined:  # a call is booked for about that time: the input goes into it, and the user was told of it
            at = offer.at("call_at") or when
            actions.append({"tool": "book_call", "offer": offer.id, "call_at": offer.call_at, "topic": offer.topic,
                            "result": f"joined the call booked before, with {source}"})
            return (f"Not booked again: {source} joined the call already booked at {at:%H:%M} on {at:%A, %B} "
                    f"{at.day} (offer {offer.id}, \"{offer.topic}\"), which will read it too. No second "
                    "notification goes out.")
        told = await self.tell(offer, self.workspace, now) if self.tell else "not told (no sender)"
        actions.append({"tool": "book_call", "offer": offer.id, "call_at": offer.call_at, "topic": topic,
                        "result": told})
        return f"Booked: offer {offer.id}, the call at {when:%H:%M} on {when:%A, %B %-d}. {told}"

    def _memo_asks(self, memo_id: str, asked: Any, now: datetime) -> str | None:
        """Why a voice memo books no call (None: its own words ask for it, and it is recent)."""
        memo = self._memos.get(memo_id)
        if memo is None:
            return f"Not booked: there is no voice memo {memo_id} among the changes you were told about."
        made = recorded(memo, now) or recorded(batch_memo(self.workspace, self._events, memo_id), now)
        if made is None:
            return (f"Not booked: Bee did not say when voice memo {memo_id} was recorded, so its times cannot be read. "
                    "If it still matters, offer a call with offer_call or put it on the agenda for the next call.")
        if now - made > timedelta(hours=MEMO_BOOK_HOURS):
            return (f"Not booked: voice memo {memo_id} was recorded {ago(made, now)}, and a memo that old books no "
                    "call: its times meant another day. If it still matters, offer a call with offer_call or put it "
                    "on the agenda for the next call.")
        text = str(memo.get("text") or "").strip()
        reply = memo.get("aiResponse") if isinstance(memo.get("aiResponse"), dict) else {}
        cleaned = reply.get("cleanedUpText") if isinstance(reply.get("cleanedUpText"), str) else ""
        # Bee's cleaned-up text of the same words has sentences where the raw text may have none (review of
        # 2026-10-17); the words still have to be the memo's, in one or the other, never across the two.
        if not (said_in([text], asked) or (cleaned.strip() and said_in([cleaned], asked))):
            return ("Not booked: asked must be the memo's own words asking for the call, copied word for word from it: "
                    "its sentence, or most of it with nothing left out that changes it. Book only a call the memo "
                    "asks for.")
        return None

    def _conversation_asks(self, conv_id: Any, asked: Any, now: datetime) -> str | None:
        """Why a Bee conversation books no call (None: its own words, in the lines the agent was shown, ask for it;
        they were said recently, outside our calls, and no line soon after takes them back). Who said them is the
        model's reading of Bee's speaker names, which are often wrong (FINDINGS 2026-10-09: 3 of 17 lines the user
        said alone were "Unknown")."""
        try:
            conv_id = int(conv_id)
        except (TypeError, ValueError):
            return "Not booked: conversation_id is a Bee conversation's number."
        seen = self._conversations.get(conv_id)
        if seen is None:
            return (f"Not booked: there is no Bee conversation {conv_id} among the changes you were told about with "
                    "their lines.")
        conv, lines = seen["conversation"], seen["lines"]
        if OurCalls.of(self.workspace).conversation(conv):  # the watcher leaves these out; never book from one
            return (f"Not booked: Bee recorded conversation {conv_id} during one of the user's calls with you; that "
                    "call's own record is the source, and it books no call.")
        texts = [str(u.get("text") or "") for u in lines]
        found = next(((first, last) for last in range(len(texts)) for first in range(last, max(-1, last - 3), -1)
                      if said_in(["\n".join(texts[first:last + 1])], asked)), None)  # in one line, or up to three
        if found is None:
            return ("Not booked: asked must be the user's own words asking for the call, copied word for word from the "
                    "conversation's lines you were shown: their sentence, or most of it with nothing left out that "
                    "changes it. Book only a call the user asks for.")
        first, last = found
        times = [t for t in (spoken_at(u, now) for u in lines[first:last + 1]) if t is not None]
        if not times:
            return ("Not booked: Bee did not say when those words were said, so their times cannot be read. If it "
                    "still matters, offer a call with offer_call or put it on the agenda.")
        said = max(times)
        if now - said > timedelta(hours=MEMO_BOOK_HOURS):  # the line's age, not the conversation's (finding 2)
            return (f"Not booked: those words were said {ago(said, now)}, and words that old book no call: their "
                    "times meant another day. If it still matters, offer a call with offer_call or put it on the "
                    "agenda.")
        for line in lines[last + 1:last + 1 + TAKE_BACK_LINES]:  # a take-back on a line soon after (finding 6)
            when = spoken_at(line, now)
            if when is not None and when - said > timedelta(seconds=TAKE_BACK_S):
                break
            if TAKEN_BACK.search(str(line.get("text") or "")):
                return (f"Not booked: a line soon after takes it back (\"{line.get('text')}\"), so the user did not "
                        "ask for the call in the end. If you think they still want one, offer it with offer_call.")
        return None

    async def _read_memos(self, events: list[dict[str, Any]], now: datetime) -> None:
        """What these events' Bee changes hold, as Bee has it now: each voice memo (bee journals get: the finished
        text and Bee's reply) and each conversation's new lines (bee conversations transcript). A memo that cannot
        be read is shown as the watcher saved it; a conversation, with its summary only."""
        self._events = events
        ids = memo_ids(self.workspace, events)[:MEMOS_SHOWN] if self.bee else []
        convs = self._changed_conversations(events)[:CONVERSATIONS_READ] if self.bee else []
        found, got_conversations = await asyncio.gather(
            asyncio.gather(*(cli.journal(i, timeout=MEMO_READ_S) for i in ids), return_exceptions=True),
            asyncio.gather(*(cli.conversation(c["id"], timeout=MEMO_READ_S) for c, _ in convs), return_exceptions=True))
        self._memos = {i: memo for i, memo in zip(ids, found) if isinstance(memo, dict) and memo.get("id") is not None}
        self._conversations = {}
        ours = OurCalls.of(self.workspace)
        for (conv, inbox), got in zip(convs, got_conversations):
            lines = one_transcription(got.get("conversation")) if isinstance(got, dict) else None
            if lines is None:  # an answer without its transcriptions: every transcription's lines, read on their own
                try:
                    lines = (await cli.transcript(conv["id"], timeout=MEMO_READ_S)).get("transcript") or []
                except Exception:
                    continue
            lines = [u for u in lines if isinstance(u, dict) and str(u.get("text") or "").strip()]
            lines.sort(key=lambda u: spoken_at(u, now).timestamp() if spoken_at(u, now) else float("inf"))
            # What Bee heard during our calls is the user talking to us: those calls' records hold it (finding 1).
            kept = [u for u in lines if not (spoken_at(u, now) and ours.at(spoken_at(u, now).timestamp()))]
            before = last_shown(self.workspace, conv["id"], inbox)
            new = [u for u in kept if before is None or (spoken_at(u, now) is not None and spoken_at(u, now) > before)]
            shown, size = [], 0
            for u in reversed(new):  # the newest, within LINES_SHOWN and LINES_CHARS
                size += len(str(u.get("text")))
                if len(shown) >= LINES_SHOWN or (shown and size > LINES_CHARS):
                    break
                shown.insert(0, u)
            self._conversations[conv["id"]] = {"conversation": conv, "lines": shown, "earlier": len(kept) - len(new),
                                               "more": len(new) - len(shown), "ours": len(lines) - len(kept)}

    def _changed_conversations(self, events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
        """The conversations these events' Bee changes hold, as the agent is shown them, with their change's inbox."""
        found: list[tuple[dict[str, Any], str]] = []
        for event in events:
            if event.get("kind") != "bee_change":
                continue
            try:
                changed = json.loads((self.workspace / str(event.get("inbox")) / "changed.json").read_text())
            except (OSError, ValueError):
                continue
            kept = set(event.get("conversation_ids") or [])
            for c in [c for c in changed.get("conversations", []) if not kept or c.get("id") in kept]:
                if isinstance(c, dict) and isinstance(c.get("id"), int) and \
                        all(c["id"] != seen["id"] for seen, _ in found):
                    found.append((c, str(event.get("inbox"))))
        # The newest first: those still recording, then by their end (review of 2026-10-17, finding 7).
        found.sort(key=lambda pair: pair[0].get("end_time") if isinstance(pair[0].get("end_time"), (int, float))
                   else float("inf"), reverse=True)
        return found

    async def think(self, events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        """Thinks once about these events and logs the entry in the meta session. Never raises. `now` is the
        person's time (Person.now()). A request to organize Bee (organize_bee) gets a run of its own first, the only
        one with tools that add to Bee, done once even when the events come back after a think that failed; the other
        events then get the usual think. The entry returned is the last one, with an error if a run failed."""
        entry = None
        for event in [e for e in events if e.get("kind") == "organize_bee"]:
            if self.bee and not self._organized_before(event):
                entry = await self._organize(event, now)
                if entry.get("error"):
                    return entry
        rest = [e for e in events if e.get("kind") != "organize_bee"]
        if rest or entry is None and not events:
            entry = await self._think(rest, now)
        return entry or {"at": now.isoformat(timespec="seconds"), "events": events, "decision": "already done",
                         "actions": [], "tools": [], "seconds": 0.0}

    async def _think(self, events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        await self._read_memos(events, now)
        first = context(self.workspace, events, now, self.call_time, self._memos, self._conversations)
        entry = await self._loop(events, first, self.tools, MAX_ROUNDS, now)
        MetaSession(self.workspace).log(entry)
        return entry

    async def _loop(self, events: list[dict[str, Any]], first: str, tools: list[ToolSpec], rounds: int,
                    now: datetime) -> dict[str, Any]:
        """One conversation with the model from `first`, the tools it may use, at most `rounds` requests: the entry
        for the meta session, not logged yet. Never raises but CancelledError."""
        started = time.monotonic()
        usage = Usage()
        entry: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "events": events,
                                 "model": self.role.choices[0].offering.model, "tools": [], "actions": [],
                                 "decision": "", "usage": {"input_tokens": 0, "output_tokens": 0}}  # voice/costs.py
        turns = [Turn("user", text=first)]
        answering = None  # the choice that answered: the rest of the think goes to it
        stopped = False
        try:
            for _ in range(rounds):
                if answering is None:
                    reply = await providers.answer_with(self.role, self.instructions, turns, tools, usage=usage)
                    answering, entry["model"] = reply.choice, reply.model
                    if reply.instead_of:
                        entry["standby_reason"] = reply.reason
                else:
                    reply = await providers.reply(answering, self.instructions, turns, tools, usage=usage)
                turns.append(reply.turn)
                if not reply.turn.calls:
                    entry["decision"] = reply.turn.text.strip()
                    break
                results = []
                offered = {tool.name for tool in tools}
                for call in reply.turn.calls:
                    result = await self._tool(call.name, call.input, now, entry["actions"]) \
                        if call.name in offered else f"There is no tool named {call.name}."
                    result = result if len(result) <= MAX_RESULT_CHARS else result[:MAX_RESULT_CHARS] + "\n... (cut)"
                    entry["tools"].append({"name": call.name, "input": call.input, "result": result[:600]})
                    results.append(ToolResult(call.id, result))
                turns.append(Turn("user", results=results))
            else:
                entry["decision"] = "(stopped after the most rounds allowed without a final answer)"
        except Exception as exc:  # serve_meta tries these events again; nothing was proposed
            log.exception("the meta agent failed")
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
        except asyncio.CancelledError:  # the process is stopping: the events stay in the inbox for next time
            entry["error"] = "stopped before it finished (the process was stopping)"
            stopped = True
            raise
        finally:
            entry["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                              "cache_read": usage.cache_read, "cache_write": usage.cache_write}
            entry["seconds"] = round(time.monotonic() - started, 2)
            if stopped:  # logged here, with what it used (the day's budget counts it): the caller never gets it
                MetaSession(self.workspace).log(entry)
        return entry

    # ---------- organizing Bee's memory, on the user's yes in a call ----------

    def _organized_before(self, event: dict[str, Any]) -> bool:
        """Whether this request to organize already had a run that worked (its events came back with others)."""
        return any(e.get("kind") == "organize" and not e.get("error") and e.get("events") == [event]
                   for e in MetaSession(self.workspace).entries())

    async def _organize(self, event: dict[str, Any], now: datetime) -> dict[str, Any]:
        """The run the user said yes to in a call (organize_bee): it reads the days asked for in Bee and adds what is
        worth keeping, each item checked against what Bee has (_add); then one plain notification says how many went
        in, counted from the adds that went through. The only run with tools that change Bee, and those only add."""
        language = event.get("language") if event.get("language") in LANGUAGES else self.language
        self._adding = Adding(key=f"{event.get('call')}@{event.get('at')}", question=str(event.get("question") or ""),
                              answer=str(event.get("answer") or ""))
        try:
            entry = await self._loop([event], organize_context(event, now), self.organize_tools, ORGANIZE_ROUNDS, now)
            entry["kind"] = "organize"
            if not entry.get("error"):
                added = self._added(self._adding.key)
                payload = organized_notification(added, language)
                sent = await self.notify(payload) if self.notify else "not sent (no sender)"
                entry["actions"].append({"tool": "notification", "added": added,
                                         "body": payload["aps"]["alert"]["body"], "result": sent})
        finally:
            self._adding = None
        MetaSession(self.workspace).log(entry)
        return entry

    def _added(self, key: str) -> int:
        """How many items the organize request `key` put into Bee (its writes that went through)."""
        return sum(1 for w in BeeWrites(self.workspace / META_DIR).all()
                   if w.item.get("request") == key and w.status == "ok")

    async def _add(self, kind: str, args: dict[str, Any], actions: list[dict[str, Any]]) -> str:
        """bee_add_fact or bee_add_todo, in an organize run only: one item, checked in code before it is written, as
        every change to Bee is (a side effect): one short statement, at most ORGANIZE_ADDS a run, and not one Bee has
        already in the same words (its facts, confirmed or not, or its to-dos, open or done) or this run added. A
        fact goes in confirmed (made, then confirmed if Bee made it unconfirmed), a to-do open. Each write is kept in
        meta/bee-writes.jsonl with the question the user said yes to and their answer."""
        tool = "bee_add_fact" if kind == "fact_create" else "bee_add_todo"
        adding = self._adding
        if adding is None:
            return f"There is no tool named {tool}."
        text = " ".join(str(args.get("text") or "").split())
        what = f"{'fact' if kind == 'fact_create' else 'to-do'} '{text}'"
        if not text:
            return "Not added: give the item, one short statement in English."
        if len(text) > 200:
            return "Not added: one short statement, under 200 characters."
        writes = BeeWrites(self.workspace / META_DIR)
        mine = [w for w in writes.all() if w.item.get("request") == adding.key]
        if sum(1 for w in mine if w.status != "failed") >= ORGANIZE_ADDS:
            return f"Not added: at most {ORGANIZE_ADDS} items in one organizing. Finish now."
        try:
            have = await self._bee_has(adding)
        except Exception as exc:
            return (f"Not added: Bee's facts and to-dos could not be read to check that it is new ({str(exc)[:120]}). "
                    "Try once more, or finish.")
        if _plain(text) in have[kind] or any(w.kind == kind and _plain(w.text) == _plain(text) for w in mine):
            actions.append({"tool": tool, "text": text, "result": "already in Bee"})
            return f"Not added: Bee already has {what}."
        writes.dir.mkdir(parents=True, exist_ok=True)
        write = await writes.begin({"kind": kind, "text": text, "request": adding.key}, adding.question, adding.answer)
        await writes.apply(write)
        if write.status == "ok":
            have[kind].add(_plain(text))
            actions.append({"tool": tool, "text": text, "result": "added"})
            return f"Added to Bee: {what}."
        if write.status == "failed":
            actions.append({"tool": tool, "text": text, "result": f"failed: {write.error}"})
            return f"Not added: Bee said no ({write.error}). It is not tried again; carry on with the others."
        actions.append({"tool": tool, "text": text, "result": f"not sure it went in: {write.error}"})
        return f"Bee did not confirm {what} ({write.error}): it may be in Bee. Do not add it again."

    async def _bee_has(self, adding: "Adding") -> dict[str, set[str]]:
        """What Bee has, in plain words, read once per organize run: its facts (confirmed and unconfirmed), its to-dos
        (open and done), ORGANIZE_PAGES pages of each list, and the to-dos it suggests. Raises when the first page of
        a list fails."""
        if adding.have is None:
            facts: set[str] = set()
            for unconfirmed in (False, True):
                facts |= {_plain(f.get("text")) for f in await _pages(lambda c: cli.facts(
                    limit=50, cursor=c, unconfirmed=unconfirmed, timeout=META_READ_S), "facts") if isinstance(f, dict)}
            todos = {_plain(t.get("text")) for t in await _pages(lambda c: cli.todos(
                limit=50, cursor=c, timeout=META_READ_S), "todos") if isinstance(t, dict)}
            try:  # what Bee suggests counts too: a to-do in the same words would be added twice
                todos |= {_plain(t.get("text")) for t in await cli.todo_suggestions(limit=50, timeout=META_READ_S)
                          if isinstance(t, dict)}
            except Exception:
                pass
            adding.have = {"fact_create": facts, "todo_create": todos}
        return adding.have


@dataclass
class Adding:
    """An organize run in progress: its request (the call and when it was asked, the question and the user's yes),
    and what Bee has, read at its first add."""
    key: str
    question: str
    answer: str
    have: dict[str, set[str]] | None = None


async def _pages(read: Callable[[Any], Awaitable[dict[str, Any]]], key: str) -> list[Any]:
    """The items of a Bee list read a page at a time, at most ORGANIZE_PAGES pages; a page that fails after the first
    ends it with what was read."""
    found, cursor = [], None
    for page in range(ORGANIZE_PAGES):
        try:
            answer = await read(cursor)
        except Exception:
            if page == 0:
                raise
            break
        found += [item for item in answer.get(key) or []]
        cursor = answer.get("next_cursor")
        if not cursor:
            break
    return found


def _plain(text: Any) -> str:
    """Words compared loosely: spaces collapsed, case and end punctuation ignored."""
    return " ".join(str(text or "").split()).strip(" .!?。").casefold()


def organize_context(event: dict[str, Any], now: datetime) -> str:
    """The first message of an organize run: the time, and the request the user said yes to."""
    try:
        days = max(1, min(7, int(event.get("days") or 3)))
    except (TypeError, ValueError):
        days = 3
    first = now.date() - timedelta(days=days - 1)
    return "\n\n".join([
        f"It is now {now_text(now)}. {toolkit.epoch_now(now)}",
        f"In a call that just ended ({event.get('call')}), the user said yes to organizing what Bee recorded over the "
        f"past {days} days, {first:%A, %B} {first.day} to today. The call's AI asked: \"{event.get('question')}\" "
        f"The user answered: \"{event.get('answer')}\"",
        "Organize those days now, as your instructions say. Each item you add is checked against what Bee has, and "
        "the user gets one notification when you finish, with how many went in.",
    ])


# The one notification at the end of an organize run, in the person's language: how many went in.
ORGANIZED = {"ko": ("Bee에 기억 {n}개를 넣었어요. Bee 앱에서 보고 고칠 수 있어요.", "Bee에 새로 넣을 기억은 없었어요."),
             "en": ("Added {n} memories to Bee. You can see and edit them in the Bee app.", "Nothing new to add to Bee.")}


def organized_notification(added: int, language: str) -> dict[str, Any]:
    some, none = ORGANIZED.get(language, ORGANIZED["en"])
    body = some.format(n=added) if added else none
    if added == 1 and language != "ko":
        body = body.replace("1 memories", "1 memory").replace("see and edit them", "see and edit it")
    return {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "thread-id": "bee"}}


# ---------- the meta agent without a model (META_AGENT=rules) ----------

# Why the stand-in offers a call, said to the person (an offer's reason), in their language.
RULE_WHY = {"ko": "Bee에 기록된 오늘 대화예요. 짧게 이야기해 볼까요?",
            "en": "It came up in a conversation Bee recorded today. Want to talk it through?"}
RULE_DECISION = {"ko": ("“{topics}”에 대해 통화를 제안했어요.", "제안할 통화가 없어요."),
                 "en": ("Offered a call about “{topics}”.", "Nothing to offer a call about.")}


RULE_ORGANIZE = {"ko": "Bee 정리는 모델이 해야 해요. 아무것도 넣지 않았어요.",
                 "en": "Organizing Bee needs the model; nothing was added."}


class RuleThinker(Thinker):
    """The meta agent without a model, for tests from outside and machines without one (META_AGENT=rules): every
    event gets its think with no model and no network, logged in meta/session.jsonl as the model's are (model
    "rules"). A Bee conversation of at least MIN_UTTERANCES utterances (beelink/planner.py's rule) gets one offer
    about it, by its short summary, through the same offer_call as the model's, so the same rules hold (one question
    at a time, one offer per conversation, quiet hours); anything else gets nothing."""

    async def think(self, events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        from beelink.planner import MIN_UTTERANCES

        started = time.monotonic()
        entry: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "events": events, "model": "rules",
                                 "tools": [], "actions": [], "decision": "",
                                 "usage": {"input_tokens": 0, "output_tokens": 0}}
        offered: list[str] = []
        for event in [e for e in events if e.get("kind") == "organize_bee"]:  # it needs the model: nothing is added
            MetaSession(self.workspace).log({"at": now.isoformat(timespec="seconds"), "events": [event], "kind": "organize",
                                             "model": "rules", "tools": [], "actions": [],
                                             "decision": RULE_ORGANIZE[self.language], "seconds": 0.0,
                                             "usage": {"input_tokens": 0, "output_tokens": 0}})
        events = [e for e in events if e.get("kind") != "organize_bee"]
        entry["events"] = events
        try:
            for event in events:
                if event.get("kind") != "bee_change" or not self.bee:
                    continue
                inbox = str(event.get("inbox") or "")
                try:
                    changed = json.loads((files._inside(self.workspace, inbox) / "changed.json").read_text())
                except (files.FileError, OSError, ValueError):
                    continue
                kept = set(event.get("conversation_ids") or [])  # the watcher left out what Bee heard of our calls
                for c in changed.get("conversations") or []:
                    summary = str(c.get("short_summary") or "").strip()
                    if (kept and c.get("id") not in kept) or not summary or \
                            (c.get("utterances_count") or 0) < MIN_UTTERANCES:
                        continue
                    args = {"topic": summary, "why": RULE_WHY[self.language], "conversation_ids": [c.get("id")],
                            "inbox": inbox}
                    result = await self._tool("offer_call", args, now, entry["actions"])
                    entry["tools"].append({"name": "offer_call", "input": args, "result": result[:600]})
                    if result.startswith("Asked"):
                        offered.append(summary)
            did, nothing = RULE_DECISION[self.language]
            entry["decision"] = did.format(topics="”, “".join(offered)) if offered else nothing
        except Exception as exc:  # serve_meta tries these events again; nothing was proposed
            log.exception("the meta agent's rules failed")
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            entry["seconds"] = round(time.monotonic() - started, 2)
            MetaSession(self.workspace).log(entry)
        return entry


def thinker_for(workspace: Path, *, send: Send | None, call_time: str | None, bee: bool,
                language: str = "en", tell: Send | None = None, notify: Notify | None = None) -> Thinker:
    """The meta agent a person's loops run: on its model, or with META_AGENT=rules on the rules (RuleThinker)."""
    import os

    kind = RuleThinker if os.environ.get("META_AGENT", "").strip().lower() == "rules" else Thinker
    return kind(workspace, send=send, call_time=call_time, bee=bee, language=language, tell=tell, notify=notify)

