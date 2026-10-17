"""The Bee context for one call: read before the AI picks up, then saved into <session>/inputs/, raw and unedited.

No fixed bundle of Bee reads (the user's decision, 2026-10-17): a call starts from the meta agent's notes and what
changed in Bee since the person's last call, and the call prep may take one round of reads with the Bee tools
(beelink/toolkit.py). fetch_call_inputs reads only what the call is about: the full conversation for each
conversation that led to the call, and the voice memos behind it (a call the user asked for in one). What changed
since the last call comes from the watcher's saved batches (inbox/<time>/changed.json, beelink/watcher.py), which
already leave out what Bee made of our own calls: no Bee read at all. The prep step (voice/prep.py) starts from it,
before the call has a session folder. save_call_inputs then writes it into the session folder, together with the
watcher's inbox snapshot that led to the call (if any), and builds the session screen's "why I'm calling" card.
A read that fails is noted and left out; it never stops a call.
"""

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from beelink import cli
from beelink.reads import one_transcription
from beelink.calls import OurCalls
from beelink.toolkit import without_dropped

MAX_CONVERSATIONS = 3
CARD_UTTERANCES = 6
READ_TIMEOUT_S = 6.0  # one Bee CLI call took 0.9-2.6 s on 2026-10-13; this bounds a hung one (arbitrary)
CHANGES_BATCHES = 60  # the newest saved Bee changes looked through for what changed since the last call (arbitrary)
FIRST_CALL_DAYS = 2  # before a person's first call, what changed in Bee in this many days (arbitrary)
CHANGE_KINDS = ("conversations", "journals", "facts", "todos", "dailies")


@dataclass
class CallInputs:
    inbox_dir: Path | None = None
    # (id, conversation, transcript lines) for each conversation that led to the call
    conversations: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = field(default_factory=list)
    # The voice memos behind the call (Bee journals: id, text, aiResponse), as Bee has them now
    memos: list[dict[str, Any]] = field(default_factory=list)
    # What changed in Bee since the person's last call (changes_since): the newest version of each item, by kind
    changes: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def _moment(ms: int | None, zone: tzinfo) -> str | None:
    """Bee's epoch milliseconds as a full time with the person's offset (contract section 0)."""
    return datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds") if ms else None


def changes_since(workspace: Path, since: float | None) -> dict[str, Any]:
    """What the watcher saw change in Bee since `since` (epoch seconds: the last call's start; None before a first
    call, the last FIRST_CALL_DAYS), from its saved batches, newest first: the newest version of each conversation
    (without its transcript), voice memo, fact, to-do and daily summary, without locations. {"since": epoch seconds,
    "batches": how many, and a list for each kind}."""
    since = since if since is not None else time.time() - FIRST_CALL_DAYS * 86400
    found: dict[str, dict[Any, Any]] = {kind: {} for kind in CHANGE_KINDS}
    batches = 0
    try:
        folders = sorted((p for p in (workspace / "inbox").iterdir() if p.is_dir()), reverse=True)[:CHANGES_BATCHES]
    except OSError:
        folders = []
    for folder in folders:
        path = folder / "changed.json"
        try:
            if path.stat().st_mtime < since:
                continue
            changed = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        batches += 1
        for kind in CHANGE_KINDS:
            for item in changed.get(kind) or []:
                if isinstance(item, dict) and item.get("id") is not None and item["id"] not in found[kind]:
                    found[kind][item["id"]] = without_dropped({k: v for k, v in item.items() if k != "transcriptions"})
    return {"since": since, "batches": batches, **{kind: list(items.values()) for kind, items in found.items()}}


async def fetch_call_inputs(inbox_dir: Path | None = None, conversation_ids: list[int] | None = None,
                            timeout: float = READ_TIMEOUT_S, *, ours: OurCalls | None = None,
                            memo_ids: list[str] | None = None) -> CallInputs:
    """What a call starts from, or nothing at all for a person with Use Bee off (no reads, no errors): the
    conversations and voice memos behind it, read from Bee, and what changed in Bee since the person's last call,
    from the watcher's batches. `ours`: the person's own calls (their folder, and when the last one began)."""
    if not cli.connected():
        return CallInputs(inbox_dir=inbox_dir)
    ids = (conversation_ids or [])[:MAX_CONVERSATIONS]
    memos = [str(m) for m in (memo_ids or [])][:MAX_CONVERSATIONS]
    reads = [asyncio.gather(cli.conversation(i, timeout=timeout), cli.transcript(i, timeout=timeout)) for i in ids]
    reads += [cli.journal(m, timeout=timeout) for m in memos]
    names = [f"conversation {i}" for i in ids] + [f"voice memo {m}" for m in memos]
    results = await asyncio.gather(*reads, return_exceptions=True)
    inputs = CallInputs(inbox_dir=inbox_dir)
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            inputs.errors.append(f"{name}: {result}")
    results = [None if isinstance(r, BaseException) else r for r in results]
    convs, memo_reads = results[:len(ids)], results[len(ids):]
    inputs.memos = [m for m in memo_reads if isinstance(m, dict) and m.get("id") is not None]
    for conv_id, pair in zip(ids, convs):
        if pair is not None:
            conv_raw, transcript_raw = pair
            conv = (conv_raw or {}).get("conversation") or {}
            lines = one_transcription(conv)  # one transcription's lines; the transcript read joins them all
            inputs.conversations.append((conv_id, {k: v for k, v in conv.items() if k != "transcriptions"},
                                         lines if lines is not None else (transcript_raw or {}).get("transcript") or []))
    if ours is not None and ours.workspace is not None:
        last = max((start for start, _ in ours.windows), default=None)
        inputs.changes = changes_since(ours.workspace, last)
    return inputs


def save_call_inputs(session_dir: Path, inputs: CallInputs, zone: tzinfo) -> dict[str, Any]:
    """Writes the raw inputs into <session>/inputs/ and returns the "why I'm calling" card, its times in `zone`
    (the person's)."""
    folder = session_dir / "inputs"
    folder.mkdir(parents=True, exist_ok=True)
    inbox_dir = inputs.inbox_dir
    if inbox_dir and (inbox_dir / "changed.json").exists():
        shutil.copy2(inbox_dir / "changed.json", folder / "changed.json")
    if inbox_dir and (inbox_dir / "plan.md").exists():
        shutil.copy2(inbox_dir / "plan.md", session_dir / "plan.md")  # why the AI called, as decided before the call
    if inputs.changes is not None:
        _write(folder / "changes_since_last_call.json", inputs.changes)
    for memo in inputs.memos:
        _write(folder / f"memo-{memo['id']}.json", memo)

    card: dict[str, Any] = {"summary": None, "conversation_id": None, "started_at": None, "utterances": [],
                            "pending_fact_ids": []}
    if inputs.memos and not inputs.conversations:  # the voice memo behind an AI call
        reply = inputs.memos[0].get("aiResponse") if isinstance(inputs.memos[0].get("aiResponse"), dict) else {}
        card["summary"] = reply.get("cleanedUpText") or inputs.memos[0].get("text")
    for conv_id, conv, lines in inputs.conversations:
        _write(folder / f"conversation-{conv_id}.json", conv)
        _write(folder / f"transcript-{conv_id}.json", lines)
        if card["conversation_id"] is None:
            card["conversation_id"] = conv_id
            card["summary"] = conv.get("short_summary")
            card["started_at"] = _moment(conv.get("start_time"), zone)
            card["utterances"] = [
                {"at": _moment(u.get("spoken_at") or u.get("start"), zone), "speaker": u.get("speaker"),
                 "text": u.get("text")}
                for u in lines[-CARD_UTTERANCES:]
            ]
    return card
