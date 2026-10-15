"""The Claude adapter inside a real LiveKit AgentSession (text mode), with the fake Anthropic client.

This checks what the unit tests can only assume: that LiveKit carries our tag into the
assistant items it adds, runs the delegate tool and asks for a reply to its result,
and that a job note kept in the agent's history reaches Claude in a valid request.
"""

import asyncio

from livekit.agents import Agent, AgentSession, RunContext, function_tool

from voice.claude_llm import NOTHING_NEW, TURN_TAG, ClaudeBedrockLLM
from voice.fake_claude import FakeAnthropic


class Helper(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a test.")

    @function_tool
    async def delegate(self, context: RunContext, task: str) -> str:
        """Hand slow work to the background worker.

        Args:
            task: What to do.
        """
        return "Job 01 started."


def test_adapter_in_a_livekit_session():
    async def main():
        client = FakeAnthropic()
        model = ClaudeBedrockLLM(client=client)
        agent = Helper()
        async with AgentSession(llm=model) as session:
            await session.start(agent)
            await session.run(user_input="Hello there")
            await session.run(user_input="Please start a test job")

            # A finished job: the note is kept in the agent's history, then a reply is asked for.
            chat_ctx = agent.chat_ctx.copy()
            chat_ctx.add_message(role="system", content="Note from the app: background job 01 finished. Result: 42")
            await agent.update_chat_ctx(chat_ctx)
            await session.generate_reply()

            items = session.history.items
        assert client.problems == []
        said = [i for i in items if getattr(i, "role", None) == "assistant"]
        assert said and all(i.extra.get(TURN_TAG) for i in said)
        assert [c.get("tool_choice") for c in client.calls] == [None] * len(client.calls)
        # hello, test job (tool_use), the tool's result, the job note
        assert len(client.calls) == 4
        assert client.calls[2]["messages"][-1]["content"][0]["type"] == "tool_result"
        last = client.calls[3]["messages"]
        assert [m["role"] for m in last[-2:]] == ["user", "system"]
        assert last[-2]["content"] == [{"type": "text", "text": NOTHING_NEW}]
        # Each reply went into history exactly once.
        assistant_turns = [m for m in model.history if m["role"] == "assistant"]
        assert len(assistant_turns) == 4

    asyncio.run(main())


def test_the_call_ai_in_a_livekit_session_logs_each_request_and_never_speaks_an_aside(tmp_path):
    """The coordinator itself (not a stand-in agent) in a real LiveKit session: its llm_node keeps the model log,
    the speech filter drops a reply that is only a stage direction, and the record gets a turn_timing per turn."""
    import json
    from types import SimpleNamespace
    from tests.helpers import seoul_now
    from voice.agent import wire_events
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        client = FakeAnthropic([[{"type": "text", "text": "(사용자가 잠시 멈춤)"}],
                                [{"type": "text", "text": "Sure. [pause] What should we look at?"}]])
        async with AgentSession(llm=ClaudeBedrockLLM(client=client)) as session:
            wire_events(session, rec)
            await session.start(coordinator)
            await session.run(user_input="어")
            await session.run(user_input="Let's plan tomorrow")
            said = [i.text_content.strip() for i in session.history.items if getattr(i, "role", None) == "assistant"]
        rec.close()
        return rec, said

    rec, said = asyncio.run(main())
    assert [text for text in said if text] == ["Sure. What should we look at?"]  # the aside alone said nothing
    log = [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]
    assert [entry["reply"]["text"].strip() for entry in log] == ["(사용자가 잠시 멈춤)",
                                                                "Sure. [pause] What should we look at?"]
    assert "Let's plan tomorrow" in json.dumps(log[1]["chat"], ensure_ascii=False)
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events].count("turn_timing") == 2

