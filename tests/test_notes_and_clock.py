"""App notes reach non-Claude models in turn order, and both agents get the user's local time."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from beelink.clock import now_text
from voice.coordinator import Coordinator
from voice.recorder import CallRecorder


def test_now_text_reads_like_a_spoken_date():
    when = datetime(2026, 10, 17, 9, 5, tzinfo=ZoneInfo("Asia/Seoul"))
    assert now_text(when) == "Saturday, October 17, 2026, 9:05 AM (Asia/Seoul)"
    assert now_text(when.replace(hour=0)).endswith("12:05 AM (Asia/Seoul)")
    assert now_text(when.replace(hour=13)).endswith("1:05 PM (Asia/Seoul)")


def test_notes_are_user_turns_for_converse_models_and_system_messages_for_claude(tmp_path):
    when = datetime(2026, 10, 13, 16, 40, tzinfo=ZoneInfo("Asia/Seoul"))

    async def notes(as_user: bool):
        agent = Coordinator(CallRecorder(tmp_path, room=f"r{as_user}"), SimpleNamespace(), tmp_path,
                            notes_as_user=as_user, now=when)
        await agent.add_note("Note from the app: background job 01 finished. Result: dry.")
        return agent

    converse = asyncio.run(notes(True))
    last = converse.chat_ctx.items[-1]
    assert last.role == "user" and last.text_content == "(Note from the app: background job 01 finished. Result: dry.)"
    claude = asyncio.run(notes(False))
    assert claude.chat_ctx.items[-1].role == "system"
    assert "It is now Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul)." in claude.instructions
