"""Thin async wrapper around the Bee CLI (`bee`, @beeai/cli 0.7.3).

Every call adds --json and returns the parsed object. Reads are safe to call
at any time. Writes (facts and todos) are called only by beelink.writer, during a
call right after the user's spoken yes to a question that named Bee, and to retry
or take back those writes.

Whose Bee: one server serves several people (voice/people.py), each with their own
Bee login in their own folder. use_bee() sets the folder for the work that follows in
the current task (a context variable, copied into tasks started from it); the CLI then
keeps its login in files there (BEE_CONFIG_DIR, BEE_FORCE_FILE_STORE). use_bee(None)
means this person has not connected Bee. Every call fails with BeeError then, and also
where use_bee() was never called, so a Bee call cannot land in the wrong person's account
by accident. use_machine_bee() opts into this machine's own Bee login (tests, probes).

Set BEE_BIN to point at another executable (the tests use a fake one).
"""

import asyncio
import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TIMEOUT = 60.0
WRITE_TIMEOUT = TIMEOUT  # how long a write waits for Bee's answer
LOGIN_LINK = re.compile(r"Authentication link:\s*(https://\S+)")


class BeeError(RuntimeError):
    pass


class BeeOutcomeUnknown(BeeError):
    """Bee may have done what it was asked: the CLI did not answer in time, or its answer could not be read."""


@dataclass(frozen=True)
class BeeLogin:
    folder: Path | None  # None: this person has not connected Bee
    machine: bool = False  # this machine's own Bee login instead of a person's folder


_login: ContextVar[BeeLogin | None] = ContextVar("bee_login", default=None)


def use_bee(folder: Path | None) -> None:
    """Whose Bee the CLI calls in this task and the tasks it starts use from now on."""
    _login.set(BeeLogin(folder))


def use_machine_bee() -> None:
    """This machine's own Bee login (the keychain on a Mac), for tests and probes run by hand."""
    _login.set(BeeLogin(None, machine=True))


def connected() -> bool:
    """Whether the work in this task has a Bee to read: a person's login (or this machine's) was chosen. For a
    person with Use Bee off it is False, and there is nothing to read, rather than a read that failed."""
    login = _login.get()
    return login is not None and (login.machine or login.folder is not None)


def login_env(folder: Path) -> dict[str, str]:
    return {**os.environ, "BEE_CONFIG_DIR": str(folder), "BEE_FORCE_FILE_STORE": "1"}


def run_env() -> dict[str, str] | None:
    """The environment for running the CLI as the current person (None: this machine's own login)."""
    login = _login.get()
    if login is None:
        raise BeeError("no person's Bee login was chosen for this work (beelink/cli.py use_bee)")
    if login.machine:
        return None
    if login.folder is None:
        raise BeeError("Bee is not connected for this person")
    return login_env(login.folder)


def bee_bin() -> str:
    return os.environ.get("BEE_BIN", "bee")


async def run_json(*args: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        bee_bin(), *args, "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=run_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise BeeOutcomeUnknown(f"bee {' '.join(args[:2])} timed out after {timeout:g}s")
    except asyncio.CancelledError:
        proc.kill()  # the caller gave up (for example LiveKit cancelled a tool); leave no stray bee process
        raise
    if proc.returncode != 0:
        raise BeeError(f"bee {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[:200]}")
    try:
        return json.loads(out or b"{}")
    except ValueError:
        raise BeeOutcomeUnknown(f"bee {' '.join(args[:2])} gave an answer that could not be read") from None


# ---------- reads ----------

async def changed(cursor: str | None = None) -> dict[str, Any]:
    args = ["changed"] + (["--cursor", cursor] if cursor else [])
    return await run_json(*args)


async def unconfirmed_facts(limit: int = 20, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    return (await run_json("facts", "list", "--unconfirmed", "--limit", str(limit), timeout=timeout)).get("facts", [])


async def todo_suggestions(limit: int = 20, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    return (await run_json("todos", "suggestions", "--limit", str(limit), timeout=timeout)).get("todoSuggestions", [])


async def search_conversations(query: str, limit: int = 5, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    args = ("search", "--query", query, "--neural", "--limit", str(limit))
    return (await run_json(*args, timeout=timeout)).get("results", [])


async def conversation(conversation_id: int, timeout: float = TIMEOUT) -> dict[str, Any]:
    return await run_json("conversations", "get", str(conversation_id), timeout=timeout)


async def transcript(conversation_id: int, timeout: float = TIMEOUT) -> dict[str, Any]:
    return await run_json("conversations", "transcript", str(conversation_id), timeout=timeout)


async def recent() -> dict[str, Any]:
    return await run_json("now")


async def today_context(timeout: float = TIMEOUT) -> dict[str, Any]:
    """Today's Bee context: daily summary, active todos, notes and recent conversations."""
    return await run_json("today", "--context", timeout=timeout)


async def conversations(limit: int = 10, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    return (await run_json("conversations", "list", "--limit", str(limit), timeout=timeout)).get("conversations", [])


async def daily(date: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    """The daily summary for one date, YYYY-MM-DD."""
    return await run_json("daily", "find", date, timeout=timeout)


async def me(timeout: float = TIMEOUT) -> dict[str, Any]:
    return await run_json("me", timeout=timeout)


async def fact(fact_id: int) -> dict[str, Any]:
    return await run_json("facts", "get", str(fact_id))


async def todos(limit: int = 50, timeout: float = TIMEOUT) -> dict[str, Any]:
    """The newest todos, open and completed: {"todos": [{"id", "text", "created_at" (ms), "completed"}, ...]}."""
    return await run_json("todos", "list", "--limit", str(limit), timeout=timeout)


# ---------- writes (only beelink.writer calls these) ----------

async def confirm_fact(fact_id: int) -> dict[str, Any]:
    return await run_json("facts", "confirm", str(fact_id), timeout=WRITE_TIMEOUT)


async def unconfirm_fact(fact_id: int) -> dict[str, Any]:
    return await run_json("facts", "update", str(fact_id), "--confirmed", "false", timeout=WRITE_TIMEOUT)


async def update_fact(fact_id: int, text: str, confirmed: bool = True) -> dict[str, Any]:
    return await run_json("facts", "update", str(fact_id), "--text", text,
                          "--confirmed", "true" if confirmed else "false", timeout=WRITE_TIMEOUT)


async def delete_todo(todo_id: int) -> dict[str, Any]:
    return await run_json("todos", "delete", str(todo_id), timeout=WRITE_TIMEOUT)


async def create_todo(text: str, alarm_at: str | None = None) -> dict[str, Any]:
    args = ["todos", "create", "--text", text] + (["--alarm-at", alarm_at] if alarm_at else [])
    return await run_json(*args, timeout=WRITE_TIMEOUT)


async def accept_suggestion(suggestion_id: int) -> dict[str, Any]:
    return await run_json("todos", "accept-suggestion", str(suggestion_id), timeout=WRITE_TIMEOUT)


# ---------- one person's Bee login (voice/token_server.py /bee/...) ----------

async def _run_text(folder: Path, *args: str, timeout: float) -> tuple[int, str]:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    proc = await asyncio.create_subprocess_exec(
        bee_bin(), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=login_env(folder))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def start_login(folder: Path, timeout: float = 30.0) -> str:
    """Starts a Bee login kept in `folder` and returns the approval link the person opens on their phone. The
    request lasts about 5 minutes; finish_login() completes it after they approve."""
    code, out = await _run_text(folder, "login", "--no-wait", timeout=timeout)
    found = LOGIN_LINK.search(out)
    if code != 0 or not found:
        raise BeeError(f"bee login did not give an approval link (exit {code})")
    return found.group(1)


async def finish_login(folder: Path, timeout: float) -> bool:
    """Waits up to `timeout` for the approval and completes the login. `bee status` alone does not pick up an
    approved request; running `bee login` again does (friction log 3)."""
    try:
        await _run_text(folder, "login", timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return await logged_in(folder)


async def logged_in(folder: Path, timeout: float = 15.0) -> bool:
    code, out = await _run_text(folder, "status", timeout=timeout)
    return code == 0 and "not logged in" not in out.lower()


async def logout(folder: Path, timeout: float = 15.0) -> None:
    await _run_text(folder, "logout", timeout=timeout)
