"""The Bee context for one call: read before the AI picks up, then saved into <session>/inputs/, raw and unedited.

fetch_call_inputs reads everything at once into memory: unconfirmed facts, todo suggestions, today's
context, the recent conversations, the user's name, and the full conversation and transcript for each
conversation that led to the call. The prep step (voice/prep.py) starts from it, before the call has a
session folder. save_call_inputs then writes it into the session folder, together with the watcher's inbox
snapshot that led to the call (if any), and builds the session screen's "why I'm calling" card.
A read that fails is noted and left out; it never stops a call.
"""

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from beelink import cli

MAX_CONVERSATIONS = 3
CARD_UTTERANCES = 6
RECENT_CONVERSATIONS = 8
READ_TIMEOUT_S = 6.0  # one Bee CLI call took 0.9-2.6 s on 2026-10-13; this bounds a hung one (arbitrary)


@dataclass
class CallInputs:
    inbox_dir: Path | None = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    today: dict[str, Any] | None = None
    recent: list[dict[str, Any]] = field(default_factory=list)
    first_name: str | None = None
    # (id, conversation, transcript lines) for each conversation that led to the call
    conversations: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def _clock(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M") if ms else None


async def fetch_call_inputs(inbox_dir: Path | None = None, conversation_ids: list[int] | None = None,
                            timeout: float = READ_TIMEOUT_S) -> CallInputs:
    """Everything a call reads from Bee, or nothing at all for a person with Use Bee off (no reads, no errors)."""
    if not cli.connected():
        return CallInputs(inbox_dir=inbox_dir)
    # Each Bee CLI call takes about a second, so everything is read at once.
    ids = (conversation_ids or [])[:MAX_CONVERSATIONS]
    reads = [cli.unconfirmed_facts(timeout=timeout), cli.todo_suggestions(timeout=timeout),
             cli.today_context(timeout=timeout), cli.conversations(RECENT_CONVERSATIONS, timeout=timeout),
             cli.me(timeout=timeout)]
    reads += [asyncio.gather(cli.conversation(i, timeout=timeout), cli.transcript(i, timeout=timeout)) for i in ids]
    names = ["unconfirmed facts", "todo suggestions", "today", "recent conversations", "profile"]
    names += [f"conversation {i}" for i in ids]
    results = await asyncio.gather(*reads, return_exceptions=True)
    inputs = CallInputs(inbox_dir=inbox_dir)
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            inputs.errors.append(f"{name}: {result}")
    facts, suggestions, today, recent, me, *convs = (None if isinstance(r, BaseException) else r for r in results)
    inputs.facts = facts or []
    inputs.suggestions = suggestions or []
    inputs.today = today
    inputs.recent = recent or []
    inputs.first_name = (me or {}).get("first_name") or None
    for conv_id, pair in zip(ids, convs):
        if pair is not None:
            conv_raw, transcript_raw = pair
            inputs.conversations.append((conv_id, (conv_raw or {}).get("conversation") or {},
                                         (transcript_raw or {}).get("transcript") or []))
    return inputs


def save_call_inputs(session_dir: Path, inputs: CallInputs) -> dict[str, Any]:
    """Writes the raw inputs into <session>/inputs/ and returns the "why I'm calling" card."""
    folder = session_dir / "inputs"
    folder.mkdir(parents=True, exist_ok=True)
    inbox_dir = inputs.inbox_dir
    if inbox_dir and (inbox_dir / "changed.json").exists():
        shutil.copy2(inbox_dir / "changed.json", folder / "changed.json")
    if inbox_dir and (inbox_dir / "plan.md").exists():
        shutil.copy2(inbox_dir / "plan.md", session_dir / "plan.md")  # why the AI called, as decided before the call
    _write(folder / "unconfirmed_facts.json", inputs.facts)
    _write(folder / "todo_suggestions.json", inputs.suggestions)
    if inputs.today is not None:
        _write(folder / "today.json", inputs.today)
    _write(folder / "recent_conversations.json", inputs.recent)

    card: dict[str, Any] = {"summary": None, "conversation_id": None, "utterances": [],
                            "pending_fact_ids": [f["id"] for f in inputs.facts if "id" in f]}
    for conv_id, conv, lines in inputs.conversations:
        _write(folder / f"conversation-{conv_id}.json", conv)
        _write(folder / f"transcript-{conv_id}.json", lines)
        if card["conversation_id"] is None:
            card["conversation_id"] = conv_id
            card["summary"] = conv.get("short_summary")
            card["utterances"] = [
                {"at": _clock(u.get("spoken_at") or u.get("start")), "speaker": u.get("speaker"), "text": u.get("text")}
                for u in lines[-CARD_UTTERANCES:]
            ]
    return card
