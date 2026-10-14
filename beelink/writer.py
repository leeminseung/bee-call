"""Bee writes, made during the call right after the user's spoken yes (Matt, 2026-10-15; contract section 6).

The call AI asks a question that names Bee and the item ("Bee에 할 일로 넣을까요?"); after a yes it writes at
once, with the person's own Bee login, through BeeWrites.begin() and apply(). Every write is kept in the call
folder, in bee-writes.jsonl, one line per step and only appended to:
  {"key": "w1", "status": "applying", "item": {...}, "question": ..., "answer": ..., "before": ..., "at": ...}
  {"key": "w1", "status": "ok", "response": {...}, "at": ...}          or "failed" / "unknown" with "error"
  {"key": "w1", "status": "undone", "at": ...}                          the user said "take it out" (or "undo_failed")
A write's state is its latest line. "failed" means the Bee CLI ended with an error: it is said out loud in the call
and tried once more after it (retry_failed). The CLI ends with an error for any answer that is not a success,
also when Bee stored a todo and only its answer was lost (a gateway error, a dropped connection), so before a todo
is written again Bee is asked whether it has it already (in_bee_after_all). "unknown" means Bee may have it: the
CLI did not answer in time, its answer could not be read, the write was cut off (its task cancelled), or Bee
could not be asked before a retry. Such a write, like one still "applying" because the process stopped, is never
written again by itself, since a todo must not be created twice; the user is told to check Bee. After the call,
one notification says what went into Bee (summary_notification).

Items:
  {"kind": "fact_confirm", "fact_id": 12}
  {"kind": "fact_update", "fact_id": 12, "text": "Bonus is 10%"}
  {"kind": "todo_create", "text": "Ask about bonus terms", "alarm_at": "2026-10-13T09:00:00+09:00"}
  {"kind": "todo_accept", "suggestion_id": 34}
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
KINDS = ("fact_confirm", "fact_update", "todo_create", "todo_accept")
MAYBE_WRITTEN = ("unknown", "applying")  # Bee may or may not have the item; never written again by itself
CLOCK_SLACK_S = 120  # Bee's clock and this machine's may differ; a todo made this much before a write is older


@dataclass
class BeeWrite:
    key: str
    item: dict[str, Any]
    question: str | None
    answer: str | None
    status: str  # applying, ok, failed, unknown, undone, undo_failed
    response: dict[str, Any] | None = None
    before: dict[str, Any] | None = None  # the fact as it was, so an update can be taken back
    error: str | None = None
    began: float | None = None  # when it was first recorded, epoch seconds

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


async def take_back(write: BeeWrite) -> dict[str, Any]:
    """Undoes a write that went in."""
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
    raise ValueError(f"unknown kind: {write.kind!r}")


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
            if "error" in line:
                write.error = line["error"]
        return list(writes.values())

    def _append(self, line: dict[str, Any]) -> None:
        line = {**line, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        with self.path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    async def begin(self, item: dict[str, Any], question: str, answer: str) -> BeeWrite:
        """Records a write the user said yes to, as "applying", before anything reaches Bee; `question` is what the
        AI asked, `answer` the user's reply. apply() then writes it."""
        before = None
        if item.get("kind") == "fact_update":  # kept so the user can take the fix back
            try:
                before = (await cli.fact(item["fact_id"])).get("fact")
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

    async def undo(self, key: str) -> BeeWrite:
        """Takes a write that went in back out of Bee."""
        (write,) = [w for w in self.all() if w.key == key]
        if write.status != "ok":
            raise ValueError(f"write {key} is {write.status}")
        try:
            await take_back(write)
            write.status = "undone"
            self._append({"key": key, "status": "undone"})
        except Exception as exc:
            write.status, write.error = "undo_failed", str(exc)[:300]
            self._append({"key": key, "status": "undo_failed", "error": write.error})
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


async def in_bee_after_all(write: BeeWrite) -> dict[str, Any] | bool | None:
    """Whether Bee has a write the CLI reported as failed: what Bee shows of it (it went in after all), False (it
    did not), or None (it cannot be told, so it must not be written again)."""
    if write.kind == "todo_create":
        todos = (await cli.todos()).get("todos")
        if not isinstance(todos, list):
            return None
        for todo in todos:
            if not isinstance(todo, dict) or _plain(todo.get("text")) != _plain(write.text):
                continue
            made = todo.get("created_at")
            if isinstance(made, (int, float)) and write.began is not None and made / 1000 < write.began - CLOCK_SLACK_S:
                continue  # an older todo of the same name
            return {"todo": todo}
        return False
    if write.kind == "todo_accept":
        pending = await cli.todo_suggestions(limit=50)
        if any(isinstance(s, dict) and s.get("id") == write.item.get("suggestion_id") for s in pending):
            return False  # still suggested: not accepted yet
        return None  # accepted, or dismissed in the Bee app meanwhile
    return False  # a fact set to a value: setting it again changes nothing


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


def summary_notification(writes: list[BeeWrite], language: CallLanguage) -> dict[str, Any] | None:
    """The one plain notification after a call (contract section 5): what went into Bee, and what did not; None
    when the call wrote nothing."""
    went_in = [w for w in writes if w.status == "ok"]
    left_out = [w for w in writes if w.status == "failed"]
    maybe = [w for w in writes if w.status in MAYBE_WRITTEN]
    if not went_in and not left_out and not maybe:
        return None
    korean = language.code == "ko"

    def some(found: list[BeeWrite]) -> str:
        more = len(found) - 1
        return label(found[0], language) + ((f" 외 {more}개" if korean else f" and {more} more") if more else "")

    parts = []
    if went_in:
        parts.append(f"Bee에 넣었어요: {some(went_in)}" if korean else f"Added to Bee: {some(went_in)}")
    if left_out:
        parts.append(f"넣지 못했어요: {some(left_out)}" if korean else f"Couldn't add: {some(left_out)}")
    if maybe:
        parts.append(f"들어갔는지 모르니 Bee에서 확인해 주세요: {some(maybe)}" if korean
                     else f"Not sure it went in, check Bee: {some(maybe)}")
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
