"""Thin async wrapper around the Bee CLI (`bee`, @beeai/cli 0.7.3).

Every call adds --json and returns the parsed object. Reads are safe to call
at any time. Writes (facts and todos) are called only by beelink.writer, which
runs them from a call's confirmed queue and only when told to apply.

Set BEE_BIN to point at another executable (the tests use a fake one).
"""

import asyncio
import json
import os
from typing import Any

TIMEOUT = 60.0


class BeeError(RuntimeError):
    pass


def bee_bin() -> str:
    return os.environ.get("BEE_BIN", "bee")


async def run_json(*args: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        bee_bin(), *args, "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise BeeError(f"bee {' '.join(args[:2])} timed out after {timeout:.0f}s")
    except asyncio.CancelledError:
        proc.kill()  # the caller gave up (for example LiveKit cancelled a tool); leave no stray bee process
        raise
    if proc.returncode != 0:
        raise BeeError(f"bee {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[:200]}")
    return json.loads(out or b"{}")


# ---------- reads ----------

async def changed(cursor: str | None = None) -> dict[str, Any]:
    args = ["changed"] + (["--cursor", cursor] if cursor else [])
    return await run_json(*args)


async def unconfirmed_facts(limit: int = 20) -> list[dict[str, Any]]:
    return (await run_json("facts", "list", "--unconfirmed", "--limit", str(limit))).get("facts", [])


async def todo_suggestions(limit: int = 20) -> list[dict[str, Any]]:
    return (await run_json("todos", "suggestions", "--limit", str(limit))).get("todoSuggestions", [])


async def search_conversations(query: str, limit: int = 5, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    args = ("search", "--query", query, "--neural", "--limit", str(limit))
    return (await run_json(*args, timeout=timeout)).get("results", [])


async def conversation(conversation_id: int) -> dict[str, Any]:
    return await run_json("conversations", "get", str(conversation_id))


async def transcript(conversation_id: int) -> dict[str, Any]:
    return await run_json("conversations", "transcript", str(conversation_id))


async def recent() -> dict[str, Any]:
    return await run_json("now")


# ---------- writes (only beelink.writer calls these) ----------

async def confirm_fact(fact_id: int) -> dict[str, Any]:
    return await run_json("facts", "confirm", str(fact_id))


async def update_fact(fact_id: int, text: str, confirmed: bool = True) -> dict[str, Any]:
    return await run_json("facts", "update", str(fact_id), "--text", text,
                          "--confirmed", "true" if confirmed else "false")


async def create_todo(text: str, alarm_at: str | None = None) -> dict[str, Any]:
    args = ["todos", "create", "--text", text] + (["--alarm-at", alarm_at] if alarm_at else [])
    return await run_json(*args)


async def accept_suggestion(suggestion_id: int) -> dict[str, Any]:
    return await run_json("todos", "accept-suggestion", str(suggestion_id))
