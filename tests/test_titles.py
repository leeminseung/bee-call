"""A call's title, written after the call (voice/titles.py; contract section 1 and decision 7): one Claude Haiku 5.5
request in the call's language, only when the person spoke, within 5 s, recorded as call_titled; on any failure no
title, and the call view falls back."""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from voice import call_view, titles
from voice.costs import SpendLog
from voice.language import call_language

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
SEOUL = ZoneInfo("Asia/Seoul")


class Messages:
    def __init__(self, reply: str | BaseException, wait: float = 0.0, stop: str = "end_turn") -> None:
        self.reply, self.wait, self.stop, self.sent = reply, wait, stop, []

    async def create(self, **params):
        self.sent.append(params)
        await asyncio.sleep(self.wait)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)], stop_reason=self.stop,
                               usage=SimpleNamespace(input_tokens=812, output_tokens=17))


def client(reply, **kwargs):
    return SimpleNamespace(messages=Messages(reply, **kwargs))


def untitled_copy(tmp_path: Path, folder: str = "2026-10-15/0123") -> Path:
    """A fixture call without its title line."""
    copy = tmp_path / folder
    shutil.copytree(FIXTURES / folder, copy)
    lines = (copy / "events.jsonl").read_text().splitlines(keepends=True)
    (copy / "events.jsonl").write_text("".join(l for l in lines if json.loads(l)["type"] != "call_titled"))
    return copy


def last_event(folder: Path) -> dict:
    return json.loads((folder / "events.jsonl").read_text().splitlines()[-1])


def test_a_call_gets_one_title_in_its_language_from_what_was_said(tmp_path):
    folder = untitled_copy(tmp_path)
    fake = client("  “맥북 에어와 프로, 코딩용으로 뭐가 나을까.”\n")
    title = asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path, client=fake))
    assert title == "맥북 에어와 프로, 코딩용으로 뭐가 나을까"
    (sent,) = fake.messages.sent
    assert sent["model"] == "claude-haiku-5-5" and "Korean" in sent["system"]
    said = sent["messages"][0]["content"]
    assert "Person: 맥북 에어랑 프로 중에 코딩용으로 고민 중이에요." in said and "AI: 요즘 어떤 게 제일 불편하세요?" in said
    event = last_event(folder)
    assert (event["type"], event["title"], event["model"]) == ("call_titled", title, "claude-haiku-5-5")
    (spent,) = [json.loads(l) for l in (tmp_path / "state" / "spend.jsonl").read_text().splitlines()]
    assert (spent["what"], spent["input_tokens"], spent["output_tokens"]) == ("call_title", 812, 17)
    assert SpendLog(tmp_path).path.exists()
    view = call_view.call_view(tmp_path, "call-minseung-20261015-012301", zone=SEOUL, language="ko",
                               now=datetime(2026, 10, 15, 15, 0, tzinfo=SEOUL))
    assert (view["call"]["title"], view["call"]["title_source"]) == (title, "summary")


def test_no_title_is_asked_for_when_the_person_never_spoke(tmp_path):
    folder = untitled_copy(tmp_path, "2026-10-14/2145")
    fake = client("A title")
    assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path, client=fake)) is None
    assert fake.messages.sent == [] and last_event(folder)["type"] != "call_titled"


def test_any_failure_leaves_the_call_without_a_title_within_the_limit(tmp_path, monkeypatch):
    """A slow model (the hard 5 s limit), an error, a refusal or an empty answer: nothing is written, and the view's
    title falls back to the person's first sentence."""
    monkeypatch.setattr(titles, "TITLE_LIMIT_S", 0.1)
    folder = untitled_copy(tmp_path)
    for fake in (client("늦은 제목", wait=1.0), client(RuntimeError("overloaded")), client("", stop="end_turn"),
                 client("제목", stop="refusal")):
        assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path, client=fake)) is None
    assert last_event(folder)["type"] != "call_titled"
    assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path, client=None)) is None


def test_older_calls_get_their_title_from_the_one_off_command(tmp_path, monkeypatch):
    """`python -m voice.titles <person folder>` titles each ended call that has none; a call without an end (a
    stopped voice server) is left alone, so the command never makes it look alive."""
    for folder in ("2026-10-15/0123", "2026-10-14/2145"):
        untitled_copy(tmp_path, folder)
    unended = tmp_path / "2026-10-15" / "0900"
    unended.mkdir(parents=True)
    (unended / "events.jsonl").write_text(
        json.dumps({"t": 0, "type": "call_started", "room": "r", "started_at": "2026-10-15T09:00:00+09:00"}) + "\n"
        + json.dumps({"t": 5, "type": "user_transcript", "text": "Hello?"}) + "\n")
    fake = client("노트북 고르기")
    monkeypatch.setattr(titles, "anthropic_client", lambda: fake)
    titles.main([str(tmp_path)])
    assert last_event(tmp_path / "2026-10-15" / "0123")["title"] == "노트북 고르기"
    assert last_event(tmp_path / "2026-10-14" / "2145")["type"] == "call_ended"  # nobody spoke
    assert last_event(unended)["type"] == "user_transcript" and len(fake.messages.sent) == 1
    titles.main([str(tmp_path)])  # titled ones are not asked again
    assert len(fake.messages.sent) == 1
