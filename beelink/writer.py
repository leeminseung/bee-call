"""Bee writes, made during the call right after the user's spoken yes (Matt, 2026-10-15; contract section 6).

The call AI says the item and asks the user ("Bee에 'Note the bug'를 할 일로 넣을까요?"); after the user's clear yes,
which the AI judges, it writes at once, with the person's own Bee login, through BeeWrites.begin() and apply(). Every
write is kept in the call folder, in bee-writes.jsonl, one line per step and only appended to:
  {"key": "w1", "status": "applying", "item": {...}, "question": ..., "answer": ..., "before": ..., "at": ...}
  {"key": "w1", "status": "ok", "response": {...}, "at": ...}          or "failed" / "unknown" with "error"
  {"key": "w1", "status": "undone", "undo": {...}, "at": ...}           the user took it back ("undo_failed": Bee
                                                                       refused; "undo_unknown": Bee did not answer;
                                                                       "undo_impossible": it cannot be taken back here)
  {"key": "w1", "status": "withdrawn", "at": ...}                       a failed write the user took back: never retried
`question` is what the AI said since the user's turn before their last words and `answer` those words, verbatim, by
position. The user takes a change back by its key (the call AI names it: every answer that reports a change gives
its key), never by its place in the call (third review of 2026-10-18). A deleted fact or a dismissed suggestion is
taken back by a write of its own (a fact_create or todo_create whose item says which write it "restores"), since Bee
cannot restore either; the removal keeps its own record, and taking the bring-back back removes what came back.
A write's state is its latest line. "failed" means the Bee CLI ended with an error: it is said out loud in the call
and tried once more after it (retry_failed). The CLI ends with an error for any answer that is not a success,
also when Bee stored a todo and only its answer was lost (a gateway error, a dropped connection), so before a todo
is written again Bee is asked whether it has it already (in_bee_after_all). "unknown" means Bee may have it: the
CLI did not answer in time, its answer could not be read, the write was cut off (its task cancelled), or Bee
could not be asked before a retry. Such a write, like one still "applying" because the process stopped, is never
written again by itself, since a todo must not be created twice; the user is told to check Bee. After the call,
one notification says how each Bee item the call touched ends it (summary_notification).

Items:
  {"kind": "fact_confirm", "fact_id": 12}
  {"kind": "fact_update", "fact_id": 12, "text": "Bonus is 10%"}
  {"kind": "fact_create", "text": "Allergic to peanuts"}          made, then confirmed if Bee made it unconfirmed
  {"kind": "fact_delete", "fact_id": 12}                          a wrong fact taken out; brought back by a write
  {"kind": "todo_create", "text": "Ask about bonus terms", "alarm_at": "2026-10-13T09:00:00+09:00"}
  {"kind": "todo_accept", "suggestion_id": 34}
  {"kind": "todo_complete", "todo_id": 51}                        undone by marking it open again
  {"kind": "todo_dismiss", "suggestion_id": 34}                   Bee's suggestion turned down; back as a to-do
(an item may also carry "text" for how it reads, as a todo suggestion or a confirmed fact does).

Run by hand: uv run python -m beelink.writer data/people/<id>/<date>/<time>            what was written, and how
             uv run python -m beelink.writer data/people/<id>/<date>/<time> --apply    tries the failed ones again
Items in a call's bee-queue.jsonl, from before writes were made in the call, carry no record of the user's yes;
they are listed and never written.
"""

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from beelink import cli
from beelink.clock import parse_time
from voice.language import CallLanguage

log = logging.getLogger("beelink.writer")

WRITES = "bee-writes.jsonl"
OLD_QUEUE = "bee-queue.jsonl"
KINDS = ("fact_confirm", "fact_update", "fact_create", "fact_delete", "todo_create", "todo_accept", "todo_complete",
         "todo_dismiss")
# Bee cannot bring a deleted fact or a dismissed suggestion back, so taking one back makes it again from what the
# write kept of it, as a write of its own: the fact, confirmed (a new id), or the suggestion as an open to-do (the
# user's decisions of 2026-10-18; restore_item).
REMOVING = ("fact_delete", "todo_dismiss")
CREATING = ("fact_create", "todo_create", "todo_accept")  # what makes an item Bee did not have
IN_EFFECT = ("ok", "undo_failed", "undo_impossible")  # its change stands: Bee has it as written, as far as known
MAYBE_WRITTEN = ("unknown", "applying")  # Bee may or may not have the item; never written again by itself
# Changes to something Bee has that go by its id: what the id points to is read from Bee before the write, so the write
# keeps it (taking it back needs it) and the AI hears what Bee had, to say it to the user (review of 2026-10-17,
# finding 1: a delete went by the id the model named, whatever text the user heard).
AS_BEE_HAS_IT = ("fact_delete", "todo_complete", "todo_dismiss")
CLOCK_SLACK_S = 120  # Bee's clock and this machine's may differ; a todo made this much before a write is older
TODO_PAGES = 10  # pages of 50 todos looked through for a write that may have gone in; more cannot be told
FACT_PAGES = 10  # the same for facts, in each of Bee's two lists (confirmed, unconfirmed)


@dataclass
class BeeWrite:
    key: str
    item: dict[str, Any]
    question: str | None
    answer: str | None
    status: str  # applying, ok, failed, unknown, undone, undo_failed, undo_unknown, undo_impossible, withdrawn
    response: dict[str, Any] | None = None
    before: dict[str, Any] | None = None  # the fact as it was, so an update can be taken back
    error: str | None = None
    began: float | None = None  # when it was first recorded, epoch seconds
    undo: dict[str, Any] | None = None  # Bee's answer when it was taken back

    @property
    def kind(self) -> str:
        return str(self.item.get("kind"))

    @property
    def text(self) -> str:
        return str(self.item.get("text") or "")


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
    if kind == "fact_create":
        response = await cli.create_fact(item["text"])
        made = the_fact(response)
        if made.get("confirmed") is False and isinstance(made.get("id"), int):  # the user's yes is its confirmation
            try:
                await cli.confirm_fact(made["id"])
            except Exception as exc:  # the fact is in Bee either way; Bee shows it to be confirmed there
                log.warning("fact %s was made but not confirmed: %s", made["id"], exc)
                return {**response, "confirm_error": str(exc)[:200]}
        return response
    if kind == "fact_delete":
        return await cli.delete_fact(item["fact_id"])
    if kind == "todo_complete":
        return await cli.complete_todo(item["todo_id"])
    if kind == "todo_dismiss":
        return await cli.dismiss_suggestion(item["suggestion_id"])
    raise ValueError(f"unknown kind: {kind!r}")


def made_todo(response: Any) -> dict[str, Any]:
    """The todo Bee made, from its answer: {"todo": {...}} or the todo itself, which the Bee CLI's own parser
    (parseTodoResponse in bee 0.7.3) takes only when it has a numeric id and created_at, a text and a completed
    flag. Anything else is no todo ({}), so an undo never deletes by some other id."""
    if not isinstance(response, dict):
        return {}
    if isinstance(response.get("todo"), dict):
        return response["todo"]
    shaped = (isinstance(response.get("id"), int) and isinstance(response.get("text"), str)
              and isinstance(response.get("created_at"), (int, float)) and isinstance(response.get("completed"), bool))
    return response if shaped else {}


def the_fact(response: Any) -> dict[str, Any]:
    """The fact from Bee's answer: {"fact": {...}} or the fact itself, which the Bee CLI's own parser
    (parseFactResponse in bee 0.7.3) takes only with a numeric id and created_at, a text, a confirmed flag and tags.
    Anything else is no fact ({})."""
    if not isinstance(response, dict):
        return {}
    if isinstance(response.get("fact"), dict):
        return response["fact"]
    shaped = (isinstance(response.get("id"), int) and isinstance(response.get("text"), str)
              and isinstance(response.get("created_at"), (int, float)) and isinstance(response.get("confirmed"), bool)
              and isinstance(response.get("tags"), list))
    return response if shaped else {}


async def as_bee_has_it(item: dict[str, Any]) -> dict[str, Any] | None:
    """What a change in AS_BEE_HAS_IT acts on, as Bee shows it now: the fact, the todo or the todo suggestion; None
    when Bee shows no such thing. Raises when Bee cannot be read."""
    kind = item["kind"]
    if kind == "fact_delete":
        return the_fact(await cli.fact(item["fact_id"], timeout=cli.CALL_WAIT_S)) or None
    if kind == "todo_complete":
        return made_todo(await cli.todo(item["todo_id"], timeout=cli.CALL_WAIT_S)) or None
    if kind == "todo_dismiss":
        pending = await cli.todo_suggestions(limit=50, timeout=cli.CALL_WAIT_S)
        return next((s for s in pending if isinstance(s, dict) and s.get("id") == item["suggestion_id"]), None)
    raise ValueError(f"{kind} is not read before it is written")


async def take_back(write: BeeWrite) -> dict[str, Any]:
    """Undoes a write that went in: Bee's answer to the undo. A deleted fact is made again, confirmed, and a dismissed
    suggestion comes back as an open to-do, both from what the write kept (`before`), since Bee cannot restore
    either."""
    if write.kind in ("todo_create", "todo_accept"):
        todo = made_todo(write.response)
        if "id" not in todo:
            raise ValueError("Bee did not say which todo it made")
        return await cli.delete_todo(int(todo["id"]))
    if write.kind == "fact_confirm":
        return await cli.unconfirm_fact(write.item["fact_id"])
    if write.kind == "fact_update":
        before = write.before or {}
        if not isinstance(before.get("text"), str):
            raise ValueError("the fact's earlier text is not known")
        return await cli.update_fact(write.item["fact_id"], before["text"], confirmed=bool(before.get("confirmed")))
    if write.kind == "fact_create":
        made = the_fact(write.response)
        if not isinstance(made.get("id"), int):
            raise ValueError("Bee did not say which fact it made")
        return await cli.delete_fact(made["id"])
    if write.kind == "todo_complete":
        return await cli.reopen_todo(write.item["todo_id"])
    if write.kind in REMOVING:
        raise ValueError(f"a {write.kind} is brought back by a write of its own (restore_item)")
    raise ValueError(f"unknown kind: {write.kind!r}")


def restore_item(write: BeeWrite) -> dict[str, Any]:
    """The write that brings a removal back, from what the removal kept of it (`before`): a deleted fact as a new fact
    (fact_create, confirmed as the user's), a dismissed suggestion as an open to-do (todo_create). ValueError when
    what it removed is not known (records from before 2026-10-18 kept only the id)."""
    text = (write.before or {}).get("text")
    if write.kind not in REMOVING:
        raise ValueError(f"a {write.kind} is taken back, not brought back")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("what it removed is not known")
    return {"kind": "fact_create" if write.kind == "fact_delete" else "todo_create", "text": text.strip(),
            "restores": write.key}


def restores_of(writes: list[BeeWrite], key: str) -> list[BeeWrite]:
    """The writes that brought the removal `key` back, oldest first."""
    return [w for w in writes if w.item.get("restores") == key]


class BeeWrites:
    """One call's Bee writes (bee-writes.jsonl in its folder). The Bee login is the one chosen for the work that
    calls these (beelink/cli.py use_bee): the person's own."""

    def __init__(self, call_dir: Path) -> None:
        self.dir = call_dir
        self.path = call_dir / WRITES

    def all(self) -> list[BeeWrite]:
        """Each write as it stands now, in the order they were made."""
        writes: dict[str, BeeWrite] = {}
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        for raw in lines:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or not isinstance(line.get("key"), str):
                continue
            key = line["key"]
            if key not in writes:
                if not isinstance(line.get("item"), dict):
                    continue
                writes[key] = BeeWrite(key, line["item"], line.get("question"), line.get("answer"), line["status"],
                                       before=line.get("before"), began=_epoch(line.get("at")))
            write = writes[key]
            write.status = line.get("status", write.status)
            if "response" in line:
                write.response = line["response"]
            if "undo" in line:
                write.undo = line["undo"]
            if "error" in line:
                write.error = line["error"]
        return list(writes.values())

    def _append(self, line: dict[str, Any]) -> None:
        line = {**line, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        with self.path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    async def begin(self, item: dict[str, Any], question: str | None, answer: str | None,
                    before: dict[str, Any] | None = None) -> BeeWrite:
        """Records a write the user said yes to, as "applying", before anything reaches Bee; `question` is what the
        AI asked, `answer` the user's reply, `before` what it changes as Bee had it (AS_BEE_HAS_IT). apply() then
        writes it."""
        if before is None and item.get("kind") == "fact_update":  # kept so the user can take the fix back
            try:
                before = the_fact(await cli.fact(item["fact_id"], timeout=cli.CALL_WAIT_S)) or None
            except Exception:
                before = None
        # The key is taken and recorded with nothing awaited in between, so writes made at the same time (one
        # reply's tool calls run together) each get their own.
        write = BeeWrite(f"w{len(self.all()) + 1}", item, question, answer, "applying", before=before)
        self._append({"key": write.key, "status": "applying", "item": item, "question": question, "answer": answer,
                      "before": before})
        return write

    async def apply(self, write: BeeWrite) -> BeeWrite:
        """Writes it to Bee and records how that went: ok, failed (Bee said no) or unknown (Bee may have it)."""
        try:
            response = await apply_one(write.item)
        except cli.BeeOutcomeUnknown as exc:
            return self._ended(write, "unknown", str(exc)[:300])
        except asyncio.CancelledError:  # cut off: the session tore its tools down, or the retry ran out of time
            self._ended(write, "unknown", "cut off before Bee answered")
            raise
        except Exception as exc:
            return self._ended(write, "failed", str(exc)[:300] or type(exc).__name__)
        write.response, write.status, write.error = response, "ok", None
        self._append({"key": write.key, "status": "ok", "response": response})
        return write

    def _ended(self, write: BeeWrite, status: str, error: str) -> BeeWrite:
        write.status, write.error = status, error
        self._append({"key": write.key, "status": status, "error": error})
        return write

    def cannot_take_back(self, key: str, error: str) -> None:
        """Records that the write `key` cannot be taken back from the call (undo_impossible): final, and its change
        stands."""
        self._append({"key": key, "status": "undo_impossible", "error": error[:300]})

    async def undo(self, key: str) -> BeeWrite:
        """Takes the write `key` back. A failed one is withdrawn: never tried again after the call, and Bee is not
        asked. One that went in is taken back out of Bee (take_back): undone; undo_failed when Bee refused and still
        has it as written (it may be tried again); undo_unknown when Bee did not answer, was cut off, or cannot say
        whether it still has it (never tried again by itself); undo_impossible when what taking it back needs is not
        known (Bee did not say which item it made, or the fact's earlier text was not read): it is changed in the Bee
        app. A removal is brought back by a write of its own instead (restore_item)."""
        (write,) = [w for w in self.all() if w.key == key]
        if write.status == "failed":
            write.status = "withdrawn"
            self._append({"key": key, "status": "withdrawn"})
            return write
        if write.status not in ("ok", "undo_failed"):
            raise ValueError(f"write {key} is {write.status}")
        try:
            response = await take_back(write)
        except ValueError as exc:  # never a Bee command that failed: what it needs is not known
            write.status, write.error = "undo_impossible", str(exc)[:300]
            self._append({"key": key, "status": "undo_impossible", "error": write.error})
            return write
        except cli.BeeOutcomeUnknown as exc:
            write.status, write.error = "undo_unknown", str(exc)[:300]
            self._append({"key": key, "status": "undo_unknown", "error": write.error})
            return write
        except asyncio.CancelledError:
            write.status, write.error = "undo_unknown", "cut off before Bee answered"
            self._append({"key": key, "status": "undo_unknown", "error": write.error})
            raise
        except Exception as exc:  # Bee said no; or did it, and only its answer was an error: Bee is asked
            error = str(exc)[:300]
            try:  # held to the longest the call waits on Bee (third review of 2026-10-18, finding 5)
                still = await asyncio.wait_for(still_as_written(write), cli.CALL_WAIT_S)
            except asyncio.TimeoutError:
                log.warning("asking Bee whether %s is still there took too long", key)
                still = None
            except asyncio.CancelledError:
                write.status, write.error = "undo_unknown", f"{error}; cut off while asking Bee"
                self._append({"key": key, "status": "undo_unknown", "error": write.error})
                raise
            except Exception as asking:
                log.warning("could not ask Bee whether %s is still there: %s", key, asking)
                still = None
            if still is False:  # Bee no longer has it as written: the take-back went through after all
                write.status, write.undo = "undone", {"found_later": True}
                self._append({"key": key, "status": "undone", "undo": write.undo, "error": error})
                return write
            write.status, write.error = ("undo_failed" if still else "undo_unknown"), error
            self._append({"key": key, "status": write.status, "error": error})
            return write
        write.status, write.undo = "undone", response if isinstance(response, dict) else None
        self._append({"key": key, "status": "undone", "undo": write.undo})
        return write

    async def retry_failed(self, done: Callable[[BeeWrite], None] | None = None) -> list[BeeWrite]:
        """Tries each failed write once more (after the call), unless Bee has it already or cannot say; `done` hears
        each one as it ends, also one cut off. A write Bee may have ("unknown", or "applying" because the process
        stopped) is left alone."""
        retried = []
        for write in self.all():
            if write.status != "failed":
                continue
            try:
                found = await in_bee_after_all(write)
            except asyncio.CancelledError:  # cut off while asking: it may be there
                self._ended(write, "unknown", f"{write.error}; cut off while asking Bee whether it has it")
                if done is not None:
                    done(write)
                raise
            except Exception as exc:
                log.warning("could not ask Bee whether %s is there: %s", write.key, exc)
                found = None
            if found is None:  # writing it again could make it twice
                self._ended(write, "unknown", f"{write.error}; Bee could not be asked whether it has it")
            elif found:
                write.response, write.status, write.error = found, "ok", None
                self._append({"key": write.key, "status": "ok", "response": found, "found_later": True})
            else:
                self._append({"key": write.key, "status": "applying"})
                write.status = "applying"
                try:
                    await self.apply(write)
                finally:
                    if done is not None:
                        done(write)
                retried.append(write)
                continue
            retried.append(write)
            if done is not None:
                done(write)
        return retried


async def still_as_written(write: BeeWrite) -> bool | None:
    """After a take-back Bee answered with an error: whether Bee still has the write as it made it (True), no longer
    has it (False: the take-back went through after all), or cannot say (None). By what Bee shows, never by its
    error's words (follow-up review of 2026-10-18, finding 3: a delete Bee carried out came back as an error and
    every later take-back tried it again). Every read waits at most as long as the call waits on Bee; an item is read
    by its id first, and its list only to tell that it is gone; undo() holds the whole check to the same ceiling
    (third review of 2026-10-18, finding 5)."""
    wait = cli.CALL_WAIT_S
    if write.kind in ("todo_create", "todo_accept"):
        todo_id = made_todo(write.response).get("id")
        if await _shown(cli.todo, todo_id, made_todo, wait):
            return True
        cursor = None
        for _ in range(TODO_PAGES):
            page = await cli.todos(cursor=cursor, timeout=wait)
            todos = page.get("todos")
            if not isinstance(todos, list):
                return None
            if any(isinstance(t, dict) and t.get("id") == todo_id for t in todos):
                return True
            cursor = page.get("next_cursor")
            if not cursor:
                return False
        return None
    if write.kind == "fact_create":
        fact_id = the_fact(write.response).get("id")
        if await _shown(cli.fact, fact_id, the_fact, wait):
            return True
        for unconfirmed in (False, True):
            cursor = None
            for _ in range(FACT_PAGES):
                page = await cli.facts(limit=50, cursor=cursor, unconfirmed=unconfirmed, timeout=wait)
                listed = page.get("facts")
                if not isinstance(listed, list):
                    return None
                if any(isinstance(f, dict) and f.get("id") == fact_id for f in listed):
                    return True
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                return None
        return False
    if write.kind in ("fact_confirm", "fact_update"):
        fact = the_fact(await cli.fact(write.item["fact_id"], timeout=wait))
        if write.kind == "fact_confirm":
            return None if not isinstance(fact.get("confirmed"), bool) else fact["confirmed"]
        text = _plain(fact.get("text"))
        return True if text == _plain(write.text) else False if text == _plain((write.before or {}).get("text")) \
            else None
    if write.kind == "todo_complete":
        todo = made_todo(await cli.todo(write.item["todo_id"], timeout=wait))
        return None if not isinstance(todo.get("completed"), bool) else todo["completed"]
    return None


async def _shown(read: Callable[..., Any], item_id: Any, shape: Callable[[Any], dict[str, Any]], wait: float) -> bool:
    """Whether Bee shows the item when asked for it by its id. An error tells nothing either way (Bee answers one
    when the item is gone and when it cannot be reached), so only an item shown counts."""
    if not isinstance(item_id, int):
        return False
    try:
        return shape(await read(item_id, timeout=wait)).get("id") == item_id
    except cli.BeeError:
        return False


async def in_bee_after_all(write: BeeWrite) -> dict[str, Any] | bool | None:
    """Whether Bee has a write the CLI reported as failed: what Bee shows of it (it went in after all), False (it
    did not), or None (it cannot be told, so it must not be written again)."""
    if write.kind == "todo_create":
        cursor = None
        for _ in range(TODO_PAGES):  # every page: Bee promises no order
            page = await cli.todos(cursor=cursor)
            todos = page.get("todos")
            if not isinstance(todos, list):
                return None
            for todo in todos:
                if not isinstance(todo, dict) or _plain(todo.get("text")) != _plain(write.text):
                    continue
                made = todo.get("created_at")
                if isinstance(made, (int, float)) and write.began is not None \
                        and made / 1000 < write.began - CLOCK_SLACK_S:
                    continue  # an older todo of the same name
                return {"todo": todo}
            cursor = page.get("next_cursor")
            if not cursor:
                return False
        return None  # more todos than are looked through
    if write.kind in ("todo_accept", "todo_dismiss"):
        pending = await cli.todo_suggestions(limit=50)
        if any(isinstance(s, dict) and s.get("id") == write.item.get("suggestion_id") for s in pending):
            return False  # still suggested: not accepted (or dismissed) yet
        return None  # accepted or dismissed, here or in the Bee app meanwhile
    if write.kind == "fact_create":  # as for a todo: Bee may have made it before its error
        for unconfirmed in (False, True):  # made unconfirmed, and confirmed unless that failed too
            cursor = None
            for _ in range(FACT_PAGES):  # every page: Bee promises no order (review of 2026-10-17, finding 5)
                page = await cli.facts(limit=50, cursor=cursor, unconfirmed=unconfirmed)
                listed = page.get("facts")
                if not isinstance(listed, list):
                    return None
                for fact in listed:
                    if not isinstance(fact, dict) or _plain(fact.get("text")) != _plain(write.text):
                        continue
                    made = fact.get("created_at")
                    if isinstance(made, (int, float)) and write.began is not None \
                            and made / 1000 < write.began - CLOCK_SLACK_S:
                        continue  # an older fact of the same words
                    return {"fact": fact}
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                return None  # more facts than are looked through
        return False
    if write.kind == "todo_complete":
        todo = made_todo(await cli.todo(write.item["todo_id"]))
        return {"todo": todo} if todo.get("completed") is True else False
    return False  # a fact set to a value, or one deleted: doing it again changes nothing


def _plain(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _epoch(at: Any) -> float | None:
    try:
        return parse_time(str(at)).timestamp()
    except ValueError:
        return None


# ---------- what the user reads during and after the call ----------

def event_fields(write: BeeWrite) -> dict[str, Any]:
    """A write as the call's record shows it (the bee_write event, contract section 8): `result` is "ok" or
    "failed"; a write Bee may have taken without saying so also carries may_have_gone_in."""
    fields = {"key": write.key, "kind": write.kind, "text": write.text, "item": write.item, "question": write.question,
              "answer": write.answer, "result": "ok" if write.status == "ok" else "failed", "error": write.error}
    if write.status in MAYBE_WRITTEN:
        fields["may_have_gone_in"] = True
    return fields


def label(write: BeeWrite, language: CallLanguage) -> str:
    """One write as the user reads it: "todo 'Note the bug'", "할 일 '버그 기록'"."""
    return item_label(write.item, language)


def item_label(item: dict[str, Any], language: CallLanguage) -> str:
    kind, text = str(item.get("kind")), str(item.get("text") or "")
    korean = language.code == "ko"
    if kind.startswith("todo"):
        name = "할 일" if korean else "todo"
    else:
        name = "사실" if korean else "fact"
    if text:
        return f"{name} '{text}'"
    return ("제안된 할 일" if korean else "a suggested todo") if kind == "todo_accept" else \
        ("사실 하나" if korean else "a fact")


EXISTED = ("fact_confirm", "fact_update", "fact_delete", "todo_complete", "todo_dismiss")  # act on what Bee had


@dataclass
class _Item:
    existed: bool  # Bee had it when the call began
    exists: bool
    shown: BeeWrite  # the write it is named by: the last one whose change stands
    confirmed: bool = False
    changed: bool = False
    done: bool = False
    brought_back: bool = False


def item_outcomes(writes: list[BeeWrite]) -> list[tuple[str, BeeWrite]]:
    """How each Bee item the call touched ends it, as (outcome, the write it is named by), in the order the call first
    touched them; an item that ends as it began is left out. Outcomes: added, confirmed, changed, done, removed,
    brought_back (removed and made again, with the same text), not_added, not_brought_back, not_changed (a write that
    did not go through, after its retry too), maybe_in (Bee did not confirm a write), maybe_out (Bee did not confirm
    a take-back). An item is followed by its Bee id, also to the new id a bring-back or an accepted suggestion gave
    it; a write whose take-back cannot be done here counts as in Bee (third review of 2026-10-18)."""
    by_key = {w.key: w for w in writes}
    alias: dict[tuple, tuple] = {}
    items: dict[tuple, _Item] = {}
    flags: dict[tuple, list[tuple[str, BeeWrite]]] = {}

    def made(w: BeeWrite) -> tuple | None:
        if w.kind not in CREATING:
            return None
        found = (the_fact if w.kind == "fact_create" else made_todo)(w.response).get("id")
        return ("fact" if w.kind == "fact_create" else "todo", found) if isinstance(found, int) else None

    def identity(w: BeeWrite, depth: int = 0) -> tuple:
        restores = w.item.get("restores")
        if restores in by_key and depth < len(writes):
            return identity(by_key[restores], depth + 1)
        if w.kind in ("fact_confirm", "fact_update", "fact_delete"):
            found = ("fact", w.item.get("fact_id"))
        elif w.kind == "todo_complete":
            found = ("todo", w.item.get("todo_id"))
        elif w.kind in ("todo_accept", "todo_dismiss"):
            found = ("suggestion", w.item.get("suggestion_id"))
        else:  # a new item is its own, whatever id Bee gave it; later changes by that id find it (alias)
            return ("write", w.key)
        return alias.get(found, found)

    for w in writes:
        key = identity(w)
        new = made(w)
        if new is not None:  # what it made is the same item from here on
            alias.setdefault(new, key)
        item = items.setdefault(key, _Item(existed=w.kind in EXISTED, exists=w.kind in EXISTED, shown=w))
        if w.status in ("withdrawn", "undone"):  # never made, or taken back: as if it were not there
            continue
        if w.status == "failed":
            outcome = "not_brought_back" if w.item.get("restores") else "not_added" if w.kind in CREATING \
                else "not_changed"
            flags.setdefault(key, []).append((outcome, w))
            continue
        if w.status in MAYBE_WRITTEN or w.status == "undo_unknown":
            flags.setdefault(key, []).append(("maybe_in" if w.status in MAYBE_WRITTEN else "maybe_out", w))
            continue
        if w.status not in IN_EFFECT:
            continue
        item.shown = w  # its change stands
        if w.kind in REMOVING:
            item.exists, item.brought_back = False, False
        elif w.item.get("restores"):
            item.exists, item.brought_back = True, True
        elif w.kind in CREATING:
            item.exists = True
        elif w.kind == "fact_confirm":
            item.confirmed = True
        elif w.kind == "fact_update":
            item.changed = True
        elif w.kind == "todo_complete":
            item.done = True
    outcomes: list[tuple[str, BeeWrite]] = []
    for key, item in items.items():
        unsure = [f for f in flags.get(key, []) if f[0] in ("maybe_in", "maybe_out")]
        if unsure:  # what Bee has of it is not known: only that it is to be checked
            outcomes += unsure
            continue
        if item.exists and not item.existed:
            outcomes.append(("added", item.shown))
        elif item.existed and not item.exists:
            outcomes.append(("removed", item.shown))
        elif item.exists:
            for flag, outcome in ((item.brought_back, "brought_back"), (item.changed, "changed"),
                                  (item.confirmed, "confirmed"), (item.done, "done")):
                if flag:
                    outcomes.append((outcome, item.shown))
                    break
        outcomes += flags.get(key, [])
    return outcomes


NOTIFICATION_LINES = {  # outcome: (Korean, English), in the order they are said
    "added": ("Bee에 넣었어요", "Added to Bee"),
    "confirmed": ("Bee에서 맞다고 확인했어요", "Confirmed in Bee"),
    "changed": ("Bee에서 고쳤어요", "Changed in Bee"),
    "done": ("Bee에서 끝냈어요", "Marked done in Bee"),
    "removed": ("Bee에서 뺐어요", "Removed from Bee"),
    "brought_back": ("Bee에 되돌려 놓았어요", "Brought back in Bee"),
    "not_added": ("넣지 못했어요", "Couldn't add"),
    "not_brought_back": ("되돌려 놓지 못했어요", "Couldn't bring back"),
    "not_changed": ("Bee에서 바꾸지 못했어요", "Couldn't change in Bee"),
    "maybe_in": ("들어갔는지 모르니 Bee에서 확인해 주세요", "Not sure it went in, check Bee"),
    "maybe_out": ("빠졌는지 모르니 Bee에서 확인해 주세요", "Not sure it was taken back, check Bee"),
}


def summary_notification(writes: list[BeeWrite], language: CallLanguage) -> dict[str, Any] | None:
    """The one plain notification after a call (contract section 5): how each Bee item the call touched ends it
    (item_outcomes), never the list of writes by kind; None when nothing ends changed or to check. An item removed
    and brought back is one "brought back" line, one brought back and removed again is "removed" (third review of
    2026-10-18, findings 2 and 3)."""
    outcomes = item_outcomes(writes)
    if not outcomes:
        return None
    korean = language.code == "ko"

    def some(found: list[BeeWrite]) -> str:
        more = len(found) - 1
        return label(found[0], language) + ((f" 외 {more}개" if korean else f" and {more} more") if more else "")

    parts = []
    for outcome, (ko, en) in NOTIFICATION_LINES.items():
        found = [w for o, w in outcomes if o == outcome]
        if found:
            parts.append(f"{ko if korean else en}: {some(found)}")
    return {"aps": {"alert": {"title": "Bee Call", "body": ". ".join(parts)}, "sound": "default", "thread-id": "bee"}}


# ---------- by hand ----------

async def run(session_dir: Path, apply: bool) -> list[BeeWrite]:
    """Prints the call's Bee writes; with `apply`, tries the failed ones again (retry_failed). Returns them."""
    writes = BeeWrites(session_dir)
    for write in writes.all():
        heard = f'asked "{write.question}", heard "{write.answer}"'
        print(f"{write.status}: {write.kind} {json.dumps(write.item, ensure_ascii=False)} ({heard})"
              + (" - Bee may have it: check before writing it again" if write.status in MAYBE_WRITTEN else ""))
    old = session_dir / OLD_QUEUE
    if old.exists():
        for raw in old.read_text().splitlines():
            if raw.strip():
                print(f"not written: {raw.strip()} (queued before writes were made in the call; no spoken yes was "
                      "recorded)")
    if not apply:
        return []
    retried = await writes.retry_failed()
    for write in retried:
        print(f"{write.status}: {write.kind} {write.text}"
              + (" - Bee may have it: check before writing it again" if write.status in MAYBE_WRITTEN else ""))
    return retried


def person_of(session_dir: Path, people) -> str:
    """The person whose call folder this is: data/people/<id>/<date>/<time>, inside the data root."""
    session_dir = session_dir.resolve()
    person = session_dir.parent.parent.name
    if session_dir.parent.parent != people.folder(person).resolve():
        raise SystemExit(f"{session_dir} is not a call folder under {people.root.resolve()}/people/<id>/")
    return person


def main() -> None:
    from voice.people import People

    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path, help="a call folder, data/people/<id>/<date>/<time>")
    parser.add_argument("--apply", action="store_true", help="try the failed writes again (default: only list)")
    args = parser.parse_args()
    people = People()
    person = person_of(args.session_dir, people)
    if not people.settings(person).bee:
        raise SystemExit(f"{person} has not connected Bee; nothing can be written")
    print(f"{person}'s Bee writes")

    async def as_person() -> None:
        cli.use_bee(people.bee_dir(person))  # only this person's Bee account
        await run(args.session_dir, args.apply)

    asyncio.run(as_person())


if __name__ == "__main__":
    main()
