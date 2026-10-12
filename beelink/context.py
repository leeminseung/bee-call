"""Saves the Bee context for one call into <session>/inputs/, raw and unedited.

Called when a call starts. It copies the watcher's inbox snapshot that led to
the call (if any), then reads fresh: unconfirmed facts, todo suggestions, and
the full conversation and transcript for each triggering conversation.
It also builds the payload for the session screen's "why I'm calling" card.
"""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from beelink import cli

MAX_CONVERSATIONS = 3
CARD_UTTERANCES = 6


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def _clock(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M") if ms else None


async def save_call_inputs(session_dir: Path, inbox_dir: Path | None = None,
                           conversation_ids: list[int] | None = None) -> dict[str, Any]:
    """Returns {"card": <why-calling payload>, "facts": [...], "suggestions": [...]}."""
    inputs = session_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    if inbox_dir and (inbox_dir / "changed.json").exists():
        shutil.copy2(inbox_dir / "changed.json", inputs / "changed.json")
    if inbox_dir and (inbox_dir / "plan.md").exists():
        shutil.copy2(inbox_dir / "plan.md", session_dir / "plan.md")  # why the AI called, as decided before the call

    # Each Bee CLI call takes about a second, so read everything at once (2.2 s -> 1.5 s for the first two).
    ids = (conversation_ids or [])[:MAX_CONVERSATIONS]
    facts, suggestions, *convs = await asyncio.gather(
        cli.unconfirmed_facts(), cli.todo_suggestions(),
        *(asyncio.gather(cli.conversation(i), cli.transcript(i)) for i in ids))
    _write(inputs / "unconfirmed_facts.json", facts)
    _write(inputs / "todo_suggestions.json", suggestions)

    card: dict[str, Any] = {"summary": None, "conversation_id": None, "utterances": [],
                            "pending_fact_ids": [f["id"] for f in facts if "id" in f]}
    for conv_id, (conv_raw, transcript_raw) in zip(ids, convs):
        conv = conv_raw.get("conversation", {})
        lines = transcript_raw.get("transcript", [])
        _write(inputs / f"conversation-{conv_id}.json", conv)
        _write(inputs / f"transcript-{conv_id}.json", lines)
        if card["conversation_id"] is None:
            card["conversation_id"] = conv_id
            card["summary"] = conv.get("short_summary")
            card["utterances"] = [
                {"at": _clock(u.get("spoken_at") or u.get("start")), "speaker": u.get("speaker"), "text": u.get("text")}
                for u in lines[-CARD_UTTERANCES:]
            ]
    return {"card": card, "facts": facts, "suggestions": suggestions}
