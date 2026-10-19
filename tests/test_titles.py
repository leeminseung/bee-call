"""A call's title, written after the call (voice/titles.py; contract section 1 and decision 7): one request to the
model catalog's title role (Claude Haiku 5.5) in the call's language, only when the person spoke, within 5 s,
recorded as call_titled; on any failure no title, and the call view falls back."""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from voice import call_view, providers, titles
from voice.costs import SpendLog
from voice.language import call_language
from voice.providers import Reply, Turn, Usage

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
SEOUL = ZoneInfo("Asia/Seoul")


class TitleModel:
    """The title role's provider, answering from a script: a title, an error, a long wait or a refusal."""

    def __init__(self, answer: str | BaseException, wait: float = 0.0, stop: str = "end") -> None:
        self.answer, self.wait, self.stop, self.sent = answer, wait, stop, []

    def ready(self) -> bool:
        return True

    async def reply(self, choice, system, turns, tools, *, on_text=None, usage=None):
        self.sent.append({"model": choice.offering.model, "system": system, "said": turns[0].text,
                          "thinking": choice.thinking, "max_tokens": choice.max_tokens})
        await asyncio.sleep(self.wait)
        if isinstance(self.answer, BaseException):
            raise self.answer
        used = Usage(input_tokens=812, output_tokens=17)
        if usage is not None:
            usage.add(used)
        return Reply(Turn("assistant", self.answer), self.stop, used, choice.offering.model, "anthropic")


@pytest.fixture
def title_model(monkeypatch):
    """Installs a TitleModel as the title role's provider (Anthropic's)."""
    def install(answer, **kwargs) -> TitleModel:
        model = TitleModel(answer, **kwargs)
        monkeypatch.setitem(providers._ADAPTERS, "anthropic", model)
        return model
    return install


def untitled_copy(tmp_path: Path, folder: str = "2026-10-15/0123") -> Path:
    """A fixture call without its title line."""
    copy = tmp_path / folder
    shutil.copytree(FIXTURES / folder, copy)
    lines = (copy / "events.jsonl").read_text().splitlines(keepends=True)
    (copy / "events.jsonl").write_text("".join(l for l in lines if json.loads(l)["type"] != "call_titled"))
    return copy


def last_event(folder: Path) -> dict:
    return json.loads((folder / "events.jsonl").read_text().splitlines()[-1])


def test_a_call_gets_one_title_in_its_language_from_what_was_said(tmp_path, title_model):
    folder = untitled_copy(tmp_path)
    model = title_model("  “13인치와 15인치 노트북, 뭐가 나을까.”\n")
    title = asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path))
    assert title == "13인치와 15인치 노트북, 뭐가 나을까"
    (sent,) = model.sent
    assert (sent["model"], sent["thinking"], sent["max_tokens"]) == ("claude-haiku-5-5", "disabled", 80)
    assert "Korean" in sent["system"]
    assert "Person: 13인치랑 15인치 중에 업무용으로 고민 중이에요." in sent["said"]
    assert "AI: 요즘 어떤 게 제일 불편하세요?" in sent["said"]
    event = last_event(folder)
    assert (event["type"], event["title"], event["model"]) == ("call_titled", title, "claude-haiku-5-5")
    (spent,) = [json.loads(l) for l in (tmp_path / "state" / "spend.jsonl").read_text().splitlines()]
    assert (spent["what"], spent["model"], spent["input_tokens"], spent["output_tokens"]) == (
        "call_title", "claude-haiku-5-5", 812, 17)
    assert SpendLog(tmp_path).path.exists()
    view = call_view.call_view(tmp_path, "call-minseung-20261015-012301", zone=SEOUL, language="ko",
                               now=datetime(2026, 10, 15, 15, 0, tzinfo=SEOUL))
    assert (view["call"]["title"], view["call"]["title_source"]) == (title, "summary")


def test_no_title_is_asked_for_when_the_person_never_spoke(tmp_path, title_model):
    folder = untitled_copy(tmp_path, "2026-10-14/2145")
    model = title_model("A title")
    assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path)) is None
    assert model.sent == [] and last_event(folder)["type"] != "call_titled"


def test_any_failure_leaves_the_call_without_a_title_within_the_limit(tmp_path, title_model, monkeypatch):
    """A slow model (the hard 5 s limit), an error, a refusal or an empty answer: nothing is written, and the view's
    title falls back to the person's first sentence; a provider that is not set up is not asked at all."""
    monkeypatch.setattr(titles, "TITLE_LIMIT_S", 0.1)
    folder = untitled_copy(tmp_path)
    for answer, more in (("늦은 제목", {"wait": 1.0}), (RuntimeError("overloaded"), {}), ("", {}),
                         ("제목", {"stop": "refusal"})):
        title_model(answer, **more)
        assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path)) is None
    assert last_event(folder)["type"] != "call_titled"
    unready = title_model("A title")
    unready.ready = lambda: False  # no ANTHROPIC_API_KEY
    assert asyncio.run(titles.title_call(folder, call_language("ko"), workspace=tmp_path)) is None
    assert unready.sent == []


def test_older_calls_get_their_title_from_the_one_off_command(tmp_path, title_model):
    """`python -m voice.titles <person folder>` titles each ended call that has none; a call without an end (a
    stopped voice server) is left alone, so the command never makes it look alive."""
    for folder in ("2026-10-15/0123", "2026-10-14/2145"):
        untitled_copy(tmp_path, folder)
    unended = tmp_path / "2026-10-15" / "0900"
    unended.mkdir(parents=True)
    (unended / "events.jsonl").write_text(
        json.dumps({"t": 0, "type": "call_started", "room": "r", "started_at": "2026-10-15T09:00:00+09:00"}) + "\n"
        + json.dumps({"t": 5, "type": "user_transcript", "text": "Hello?"}) + "\n")
    model = title_model("노트북 고르기")
    titles.main([str(tmp_path)])
    assert last_event(tmp_path / "2026-10-15" / "0123")["title"] == "노트북 고르기"
    assert last_event(tmp_path / "2026-10-14" / "2145")["type"] == "call_ended"  # nobody spoke
    assert last_event(unended)["type"] == "user_transcript" and len(model.sent) == 1
    titles.main([str(tmp_path)])  # titled ones are not asked again
    assert len(model.sent) == 1
