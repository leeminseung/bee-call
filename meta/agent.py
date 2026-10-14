"""The meta agent: Bee Call's background coordinator between calls.

It wakes only for events (a Bee change, a call that ended, the user's answer to an offer, a booked call coming
up), looks at what changed with read-only tools (the workspace's files and Bee), and decides the user's next step.
It only proposes: a call offer is a question the user answers, its notes (state.md) are for the next call, and
Bee changes wait for the user's yes in a call. Every time it thinks, the events, each tool it used and its own
words go into the meta session (meta/session.py), which the app shows as they are.

One think() is one Converse conversation with the coordinator's model (GLM 5 while Claude on Bedrock is closed),
with at most MAX_ROUNDS model requests.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from beelink import cli, reads
from beelink.clock import now_text
from beelink.planner import Plan
from meta import files
from meta.session import DAILY_NOTIFICATIONS, MetaSession, quiet, quiet_ends
from voice.offers import CALL_TIME, Offer, OfferStore
from voice.prep import bedrock_client, prep_model

log = logging.getLogger("bee-call.meta")

MAX_ROUNDS = 8  # model requests in one think (arbitrary)
MAX_RESULT_CHARS = 8000  # one tool result as the model sees it

INSTRUCTIONS = """\
You are the meta agent of Bee Call, a voice assistant the user talks with on the phone. The user wears Bee, a \
wearable that records and summarizes their conversations. You never talk to the user yourself. You wake up when \
something happens, look at what changed, and decide the user's next step. You only propose: nothing happens \
until the user says yes.

What you can do:
- offer_call: ask the user, with a notification, whether they want a short call about one topic, at the evening \
call time or right now. Offer a call only when talking would clearly help: a decision to make, something to sort \
out, a result to go over. Never for small talk, never about something the user turned down, never while an offer \
is open about the same thing.
- write_state: rewrite your notes, state.md. Keep the open threads (each with where it came from), the agenda for \
the next call (the next call's AI reads it before it picks up, whoever calls), what the user turned down, and what \
you learned the user prefers. Changes to Bee (confirming or fixing a fact, adding a todo) go on the agenda: the AI \
makes them in a call, after the user agrees.
- Look things up when you need to: list_files, read_file and grep over the workspace, and the bee_ tools. Each call \
is recorded in <date>/<time>/ (transcript.md, prep.md, events.jsonl, jobs/); each Bee change the watcher saw is in \
inbox/<time>/changed.json; your own past entries are in meta/session.jsonl.
- Doing nothing is often the right call. Then say so.

Finish with a few sentences for the user, who reads them in the app's timeline exactly as you write them: what \
you noticed, and what you did or why you did nothing. Plain words, no lists."""


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "required": required if required is not None else list(properties)}
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


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
    _tool("bee_today", "Bee's context for today: daily summary, active todos, notes, captured conversations.", {}),
    _tool("bee_conversation", "One Bee conversation: its summary and its last lines.",
          {"conversation_id": {"type": "integer"}}),
    _tool("search_bee", "Search, by meaning, the conversations Bee recorded.", {"query": {"type": "string"}}),
    _tool("bee_day", "Bee's daily summary for one date (YYYY-MM-DD).", {"date": {"type": "string"}}),
    _tool("bee_pending", "Bee's unconfirmed facts and todo suggestions, with their ids.", {}),
    _tool("write_state", "Replace state.md, your notes, with this markdown.", {"text": {"type": "string"}}),
    _tool("offer_call", "Ask the user, with a notification, whether they want a short call about one topic.",
          {"topic": {"type": "string", "description": "What the call is about, in a few words."},
           "why": {"type": "string", "description": "Why a call would help now, in one sentence."},
           "conversation_ids": {"type": "array", "items": {"type": "integer"},
                                "description": "The Bee conversations behind it, if any."},
           "inbox": {"type": "string", "description": "The inbox/<time> folder of the Bee change, if any."}},
          ["topic", "why"]),
]

Send = Callable[[Offer, Path, datetime], Awaitable[str]]


def _event_line(workspace: Path, event: dict[str, Any]) -> str:
    """One event as the agent reads it, with the Bee change's essentials so it need not open the file for them."""
    kind, at = event.get("kind"), event.get("at")
    if kind == "bee_change":
        line = f"[{at}] Bee change, saved in {event.get('inbox')}"
        try:
            changed = json.loads((workspace / str(event.get("inbox")) / "changed.json").read_text())
        except (OSError, ValueError):
            return line
        for c in changed.get("conversations", [])[:6]:
            line += (f"\n  conversation {c.get('id')} ({c.get('utterances_count') or '?'} utterances, "
                     f"{c.get('state', '')}): {c.get('short_summary') or 'no summary yet'}")
        for j in changed.get("journals", [])[:3]:
            text = str(j.get("text") or j.get("transcript") or "").strip()
            line += f"\n  voice note {j.get('id')}: {text[:200]}"
        others = {k: len(v) for k, v in changed.items() if isinstance(v, list) and k not in ("conversations", "journals") and v}
        return line + (f"\n  also changed: {others}" if others else "")
    if kind == "call_ended":
        return f"[{at}] A call ended: {event.get('call')} ({event.get('direction', '?')}); record in {event.get('call')}/"
    if kind == "offer_answered":
        return f"[{at}] The user answered offer {event.get('offer')} about \"{event.get('topic')}\": {event.get('choice')}"
    if kind == "booked_soon":
        return f"[{at}] A booked call is coming up at {event.get('call_at')}: \"{event.get('topic')}\""
    return f"[{at}] {kind}: {json.dumps({k: v for k, v in event.items() if k not in ('at', 'kind')}, ensure_ascii=False)}"


def context(workspace: Path, events: list[dict[str, Any]], now: datetime) -> str:
    """The first message: the time and limits, open offers, the agent's notes and past entries, and the events."""
    session, store = MetaSession(workspace), OfferStore(workspace)
    left = max(0, DAILY_NOTIFICATIONS - store.notified_on(now))
    limits = f"Notifications left today: {left} of {DAILY_NOTIFICATIONS}."
    if quiet(now):
        limits += f" It is quiet time: a notification waits until {quiet_ends(now):%H:%M}."
    offers = [f"- {o.id} {o.state}: \"{o.topic}\"" + (f", call at {o.call_at}" if o.call_at else "")
              + (f", suggested {o.suggested_at}" if o.state == "asked" else "") for o in store.open(now)]
    return "\n\n".join([
        f"It is now {now_text(now)}. The evening call time is {os.environ.get('CALL_TIME') or CALL_TIME}. {limits}",
        "Open offers:\n" + ("\n".join(offers) if offers else "none"),
        "Your notes (state.md):\n" + session.state(),
        "Your recent entries (meta session):\n" + session.recent_text(),
        "What just happened:\n" + "\n".join(_event_line(workspace, e) for e in events),
    ])


class Thinker:
    """One think(): a Converse tool loop. `client` and `send` can be fakes in tests."""

    def __init__(self, workspace: Path, model: tuple[str, str] | None = None, client=None, send: Send | None = None):
        self.workspace = workspace
        self.model = model or prep_model(os.environ.get("COORDINATOR_LLM", "bedrock")) or \
            ("zai.glm-5", os.environ.get("BEDROCK_REGION", "ap-southeast-2"))
        self.client = client
        self.send = send

    async def _tool(self, name: str, args: dict[str, Any], now: datetime, actions: list[dict[str, Any]]) -> str:
        ws = self.workspace
        try:
            if name == "list_files":
                return files.list_files(ws, str(args.get("path") or "."), str(args.get("pattern") or "*"))
            if name == "read_file":
                return files.read_file(ws, str(args["path"]), int(args.get("start_line") or 1),
                                       int(args.get("max_lines") or 200))
            if name == "grep":
                return files.grep(ws, str(args["pattern"]), str(args.get("path") or "."), str(args.get("glob") or "*"))
            if name == "bee_today":
                return await reads.today()
            if name == "bee_conversation":
                return await reads.conversation(int(args["conversation_id"]))
            if name == "search_bee":
                return await reads.search(str(args.get("query", "")))
            if name == "bee_day":
                return await reads.day(str(args.get("date", "")))
            if name == "bee_pending":
                facts, suggestions = await asyncio.gather(cli.unconfirmed_facts(), cli.todo_suggestions(),
                                                          return_exceptions=True)
                parts = []
                parts.append("Unconfirmed facts:\n" + (reads.facts_text(facts) or "none") if not isinstance(facts, BaseException)
                             else f"Unconfirmed facts: read failed ({facts})")
                parts.append("Todo suggestions:\n" + (reads.suggestions_text(suggestions) or "none")
                             if not isinstance(suggestions, BaseException) else f"Todo suggestions: read failed ({suggestions})")
                return "\n\n".join(parts)
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
        topic, why = str(args.get("topic", "")).strip(), str(args.get("why", "")).strip()
        if not topic or not why:
            return "Error: offer_call needs a topic and a why."
        store = OfferStore(self.workspace)
        if any(o.topic == topic for o in store.open(now)):
            return f"An offer about \"{topic}\" is already open; not asked again."
        inbox = str(args.get("inbox") or "").strip()
        inbox_dir = files._inside(self.workspace, inbox) if inbox else None  # never outside the workspace
        ids = [int(i) for i in args.get("conversation_ids") or []]
        plan = Plan(call=True, reason=why, inbox_dir=inbox_dir, conversation_ids=ids, topic=topic,
                    kind="conversation" if ids else None)
        offer = store.ask(plan, now)
        sent = await self.send(offer, self.workspace, now) if self.send else "not sent (no sender)"
        actions.append({"tool": "offer_call", "offer": offer.id, "topic": topic, "result": sent})
        return f"Asked: offer {offer.id}, suggesting {offer.suggested_at}. {sent}"

    async def _converse(self, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        model_id, region = self.model
        client = self.client or bedrock_client(region)
        return await asyncio.to_thread(client.converse, modelId=model_id, system=[{"text": system}],
                                       messages=messages, toolConfig={"tools": TOOLS},
                                       inferenceConfig={"maxTokens": 2000})

    async def think(self, events: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
        """Thinks once about these events and logs the entry in the meta session. Never raises."""
        now = now or datetime.now()
        started = time.monotonic()
        entry: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "events": events, "model": self.model[0],
                                 "tools": [], "actions": [], "decision": ""}
        messages = [{"role": "user", "content": [{"text": context(self.workspace, events, now)}]}]
        try:
            for _ in range(MAX_ROUNDS):
                reply = (await self._converse(INSTRUCTIONS, messages))["output"]["message"]
                messages.append(reply)
                text = "".join(block.get("text", "") for block in reply["content"]).strip()
                uses = [block["toolUse"] for block in reply["content"] if "toolUse" in block]
                if not uses:
                    entry["decision"] = text
                    break
                results = []
                for use in uses:
                    result = (await self._tool(use.get("name", ""), use.get("input") or {}, now, entry["actions"]))
                    result = result if len(result) <= MAX_RESULT_CHARS else result[:MAX_RESULT_CHARS] + "\n... (cut)"
                    entry["tools"].append({"name": use.get("name"), "input": use.get("input"), "result": result[:600]})
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": result}]}})
                messages.append({"role": "user", "content": results})
            else:
                entry["decision"] = "(stopped after the most rounds allowed without a final answer)"
        except Exception as exc:  # the next event wakes it again; nothing was proposed
            log.exception("the meta agent failed")
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
        entry["seconds"] = round(time.monotonic() - started, 2)
        MetaSession(self.workspace).log(entry)
        return entry
