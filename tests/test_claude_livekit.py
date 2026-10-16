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



def test_one_reply_is_one_caption_line_and_carries_no_markup(tmp_path):
    """App contract section 3 numbers caption lines and counts them in the call view; that holds while each spoken
    reply is one caption line. The coordinator never hands LiveKit a FlushSentinel (which would split a reply into
    several lines), and its speech filter keeps asides and markup out of what is spoken, captioned and recorded."""
    from types import SimpleNamespace

    from livekit.agents import FlushSentinel

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    yielded = []

    class Watched(Coordinator):
        async def llm_node(self, chat_ctx, tools, model_settings):
            async for chunk in super().llm_node(chat_ctx, tools, model_settings):
                yielded.append(chunk)
                yield chunk

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Watched(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        model = ScriptedLLM([["Sure. ", "(laughs) ", "<tool_call>x</tool_call>", "What should we look at?"],
                             ["One more ", "thing, ", "and then ", "we are done."]])
        async with AgentSession(llm=model) as session:
            await session.start(coordinator)
            turns = []
            for words in ("Let's plan tomorrow", "Anything else?"):
                before = len(session.history.items)
                await session.run(user_input=words)
                turns.append([i.text_content for i in session.history.items[before:]
                              if getattr(i, "role", None) == "assistant"])
        rec.close()
        return turns

    turns = asyncio.run(main())
    assert not any(isinstance(chunk, FlushSentinel) for chunk in yielded)
    assert [len(said) for said in turns] == [1, 1]  # each reply is one item, one caption line
    assert "(laughs)" not in turns[0][0] and "tool_call" not in turns[0][0]
    assert turns[0][0].startswith("Sure.") and turns[1][0] == "One more thing, and then we are done."


def test_a_reply_that_is_only_a_stage_direction_leaves_no_blank_turn_for_bedrock(tmp_path):
    """GLM 5 on Bedrock (no Claude adapter, no tags): a reply of "(사용자가 잠시 멈춤)" and a line break leaves no
    assistant turn behind. A turn of "\n" would go into every later Bedrock request, which refuses a blank text
    block, so every later reply of the call would fail."""
    from types import SimpleNamespace

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        glm = ScriptedLLM([["(사용자가 잠시 멈춤)", ".\n"], ["\n", "Sure. ", "What should we look at?"]])
        async with AgentSession(llm=glm) as session:
            await session.start(coordinator)
            await session.run(user_input="어")
            await session.run(user_input="Let's plan tomorrow")
            history = session.history.copy()
        rec.close()
        return history

    history = asyncio.run(main())
    said = [i.text_content for i in history.items if getattr(i, "role", None) == "assistant"]
    assert said == ["Sure. What should we look at?"]
    messages, _ = history.to_provider_format("aws")
    blocks = [b["text"] for m in messages for b in m["content"] if "text" in b]
    assert blocks and all(text.strip() for text in blocks)


def test_when_claude_fails_the_first_request_glm_answers_and_the_model_log_says_so(tmp_path):
    """COORDINATOR_LLM=anthropic in a real LiveKit session: Anthropic refuses the key on the first request, so GLM 5
    answers it and the next one. The model log names the model that answered each request, and the record says why
    GLM 5 took the call."""
    import json
    from types import SimpleNamespace

    import anthropic
    import httpx2

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.agent import wire_events
    from voice.coordinator import Coordinator
    from voice.fallback_llm import FirstRequestFallback
    from voice.recorder import CallRecorder

    response = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    refused = anthropic.AuthenticationError(
        "invalid x-api-key", response=response,
        body={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now)
        claude = ClaudeBedrockLLM(client=FakeAnthropic([refused]), model="claude-haiku-5-5", thinking="disabled")
        glm = ScriptedLLM([["네, 말씀하세요."], ["내일 일정을 같이 볼까요?"]])
        model = FirstRequestFallback(claude, lambda: glm, on_choice=lambda chosen, reason: rec.event(
            "coordinator_model", model=chosen.model, reason=reason))
        async with AgentSession(llm=model) as session:
            wire_events(session, rec)
            await session.start(coordinator)
            await session.run(user_input="여보세요?")
            await session.run(user_input="내일 계획 세우자")
            said = [i.text_content for i in session.history.items if getattr(i, "role", None) == "assistant"]
        rec.close()
        return rec, said

    rec, said = asyncio.run(main())
    assert said == ["네, 말씀하세요.", "내일 일정을 같이 볼까요?"]
    log = [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]
    assert [entry["model"] for entry in log] == ["zai.glm-5", "zai.glm-5"]
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    (choice,) = [e for e in events if e["type"] == "coordinator_model"]
    assert choice["model"] == "zai.glm-5" and "401" in choice["reason"]
    assert not [e for e in events if e["type"] == "error"]  # the failed first try is no failed turn


def test_the_call_ai_s_replies_are_said_without_the_user_s_name(tmp_path):
    """voice/name_guard.py in the coordinator's llm_node: the name in a reply (split across pieces, as it streams) is
    neither said nor captioned nor kept in the conversation; the model log keeps the reply as written, and the record
    counts what was left out."""
    import json
    from types import SimpleNamespace

    from tests.helpers import ScriptedLLM, seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    async def main():
        rec = CallRecorder(tmp_path / "workspace", room="r", now=seoul_now())
        coordinator = Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now,
                                  user_names=["Minseung", "민승"])
        model = ScriptedLLM([["알겠어요, 민", "승 님. 민승 님은 ", "주말에 뭐 하세요?"]])
        async with AgentSession(llm=model) as session:
            await session.start(coordinator)
            await session.run(user_input="오늘 좀 피곤하네")
            said = [i.text_content for i in session.history.items if getattr(i, "role", None) == "assistant"]
        rec.close()
        return rec, said

    rec, said = asyncio.run(main())
    assert said == ["알겠어요. 주말에 뭐 하세요?"]
    log = [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]
    assert log[0]["reply"]["text"] == "알겠어요, 민승 님. 민승 님은 주말에 뭐 하세요?"
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert [e["times"] for e in events if e["type"] == "name_left_out"] == [2]
