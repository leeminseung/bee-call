"""Writes a call's confirmed items from bee-queue.jsonl to Bee, after the call.

Every queue line was agreed to out loud by the user during the call:
  {"kind": "fact_confirm", "fact_id": 12}
  {"kind": "fact_update", "fact_id": 12, "text": "Bonus is 10%"}
  {"kind": "todo_create", "text": "Ask about bonus terms", "alarm_at": "2026-10-13T09:00:00+09:00"}
  {"kind": "todo_accept", "suggestion_id": 34}

Dry run by default: prints the Bee commands and writes nothing to Bee.
With --apply it runs them and appends each result to bee-writes.jsonl, which
only this tool writes. Lines already applied are skipped on a rerun. Each result
also goes into the call's events.jsonl as a bee_write_done line, which is where
the session screen shows Bee writes; the call is over by then, so the voice
server no longer writes that file.

Run: uv run python -m beelink.writer workspace/2026-10-12/1530 [--apply]
"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from beelink import cli


def line_key(index: int, item: dict[str, Any]) -> str:
    return f"{index}:{hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]}"


def describe(item: dict[str, Any]) -> list[str]:
    kind = item.get("kind")
    if kind == "fact_confirm":
        return ["facts", "confirm", str(item["fact_id"])]
    if kind == "fact_update":
        return ["facts", "update", str(item["fact_id"]), "--text", item["text"], "--confirmed", "true"]
    if kind == "todo_create":
        return ["todos", "create", "--text", item["text"]] + (["--alarm-at", item["alarm_at"]] if item.get("alarm_at") else [])
    if kind == "todo_accept":
        return ["todos", "accept-suggestion", str(item["suggestion_id"])]
    raise ValueError(f"unknown queue kind: {kind!r}")


async def apply_one(item: dict[str, Any]) -> dict[str, Any]:
    kind = item["kind"]
    if kind == "fact_confirm":
        return await cli.confirm_fact(item["fact_id"])
    if kind == "fact_update":
        return await cli.update_fact(item["fact_id"], item["text"], confirmed=True)
    if kind == "todo_create":
        return await cli.create_todo(item["text"], item.get("alarm_at"))
    if kind == "todo_accept":
        return await cli.accept_suggestion(item["suggestion_id"])
    raise ValueError(f"unknown queue kind: {kind!r}")


def note_on_screen(session_dir: Path, item: dict[str, Any], ok: bool, error: str | None = None) -> None:
    events = session_dir / "events.jsonl"
    if not events.exists():
        return
    lines = [json.loads(l) for l in events.read_text().splitlines() if l.strip()]
    started = next((e.get("started_at") for e in lines if e.get("type") == "call_started"), None)
    try:  # seconds since the call started, like every other line in the file
        t = round((datetime.now() - datetime.fromisoformat(started)).total_seconds(), 3)
    except (TypeError, ValueError):
        t = lines[-1].get("t") if lines else 0.0
    line = {"t": t, "type": "bee_write_done", "kind": item.get("kind"), "text": item.get("text", ""), "ok": ok, "item": item}
    if error:
        line["error"] = error
    with events.open("a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


async def run(session_dir: Path, apply: bool) -> list[dict[str, Any]]:
    queue_file = session_dir / "bee-queue.jsonl"
    if not queue_file.exists():
        print("no bee-queue.jsonl in this session; nothing to write")
        return []
    results_file = session_dir / "bee-writes.jsonl"
    done = set()
    if results_file.exists():
        done = {json.loads(l)["key"] for l in results_file.read_text().splitlines() if json.loads(l).get("ok")}

    results = []
    for index, raw in enumerate(queue_file.read_text().splitlines()):
        if not raw.strip():
            continue
        item = json.loads(raw)
        key = line_key(index, item)
        command = describe(item)
        if key in done:
            print(f"skip (already written): bee {' '.join(command)}")
            continue
        if not apply:
            print(f"dry run: bee {' '.join(command)}")
            results.append({"key": key, "item": item, "dry_run": True})
            continue
        record: dict[str, Any] = {"key": key, "item": item, "at": datetime.now().isoformat(timespec="seconds")}
        try:
            record["response"] = await apply_one(item)
            record["ok"] = True
        except Exception as exc:
            record["ok"] = False
            record["error"] = str(exc)
        with results_file.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        note_on_screen(session_dir, item, record["ok"], record.get("error"))
        print(f"{'ok' if record['ok'] else 'FAILED'}: bee {' '.join(command)}")
        results.append(record)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="really write to Bee (default is a dry run)")
    args = parser.parse_args()
    asyncio.run(run(args.session_dir, args.apply))


if __name__ == "__main__":
    main()
