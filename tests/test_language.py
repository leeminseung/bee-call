"""The call language: English by default, Korean on request, set once for Transcribe, Polly, the coordinator and
the prep's opening."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from beelink.context import CallInputs
from voice.coordinator import INSTRUCTIONS, Coordinator
from voice.language import call_language
from voice.prep import Preparation
from voice.recorder import CallRecorder

NOW = datetime(2026, 10, 14, 0, 30, tzinfo=ZoneInfo("Asia/Seoul"))


def test_english_is_the_default_and_korean_is_one_setting(monkeypatch):
    monkeypatch.delenv("CALL_LANGUAGE", raising=False)
    english = call_language()
    assert (english.code, english.transcribe, english.voice, english.engine, english.rule) == (
        "en", "en-US", "Ruth", "generative", "")
    korean = call_language("KO ")
    assert (korean.transcribe, korean.voice, korean.engine) == ("ko-KR", "Seoyeon", "neural") and "Korean" in korean.rule
    with pytest.raises(SystemExit):
        call_language("fr")


def test_the_coordinator_speaks_the_call_language(tmp_path):
    korean = Coordinator(CallRecorder(tmp_path, room="r"), SimpleNamespace(), tmp_path, now=NOW,
                         language=call_language("ko"))
    english = Coordinator(CallRecorder(tmp_path, room="r2"), SimpleNamespace(), tmp_path, now=NOW,
                          language=call_language("en"))
    assert call_language("ko").rule in korean.instructions and korean.instructions.startswith(INSTRUCTIONS)
    assert "Korean" not in english.instructions


def test_the_prep_opening_is_in_the_call_language(tmp_path):
    from voice.costs import SpendLog

    async def main():
        return (Preparation(CallInputs(), {}, None, spend=SpendLog(tmp_path), bee=True, language=call_language("ko"))._system,
                Preparation(CallInputs(), {}, None, spend=SpendLog(tmp_path), bee=True, language=call_language("en"))._system)

    import asyncio
    korean, english = asyncio.run(main())
    assert "spoken Korean, in the polite style, as the end of the next message asks" in korean
    assert "under 20 words of spoken English" in english and "{opening}" not in english


def test_fallback_greetings_follow_the_language(monkeypatch):
    from voice import agent

    monkeypatch.setattr(agent, "COORDINATOR_LLM", "bedrock")
    korean = call_language("ko")
    assert agent.greeting({"direction": "inbound"}, korean).startswith("네, Bee Call이에요")
    assert "음성 메모" in agent.greeting({"direction": "outbound", "kind": "voice note"}, korean)
