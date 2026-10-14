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


def test_notes_stay_in_turn_order_for_converse_models(tmp_path):
    """LiveKit keeps a mid-call system note in place as an <instructions> user turn for Bedrock models."""
    when = datetime(2026, 10, 13, 16, 40, tzinfo=ZoneInfo("Asia/Seoul"))

    async def main():
        agent = Coordinator(CallRecorder(tmp_path, room="r", now=when), SimpleNamespace(), tmp_path, clock=lambda: when)
        chat = agent.chat_ctx.copy()
        chat.add_message(role="assistant", content="Hi, it's Bee Call. What's on your mind?")
        chat.add_message(role="user", content="Can you check the weather?")
        await agent.update_chat_ctx(chat)
        await agent.add_note("Note from the app: background job 01 finished. Result: dry.")
        return agent

    agent = asyncio.run(main())
    assert "It is now Tuesday, October 13, 2026, 4:40 PM (Asia/Seoul)." in agent.instructions
    messages, extra = agent.chat_ctx.to_provider_format(format="aws")
    assert not any("job 01" in s for s in extra.system_messages)  # not moved to the top
    last = messages[-1]
    assert last["role"] == "user" and last["content"][0]["text"] == "Can you check the weather?"
    assert "<instructions>" in last["content"][1]["text"] and "job 01 finished" in last["content"][1]["text"]
