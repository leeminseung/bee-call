"""Tests for the Claude adapter's history rules, with a fake Anthropic client (no network).

FakeAnthropic checks every request against the Messages API rules and checks each
earlier thinking block against the history before it, so these tests fail where
Bedrock would reject the request.
"""

import asyncio

import anthropic
import httpx2
from livekit.agents import llm
from livekit.agents.types import APIConnectOptions
from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

from voice.claude_llm import (CONNECTED, INSTRUCTIONS_ID, NO_RESULT_YET, NOT_RUN, NOTHING_NEW, STILL_RUNNING, TURN_TAG,
                              ClaudeBedrockLLM, check_request)
from voice.fake_claude import FakeAnthropic


class FakeSpeech:
    """The parts of LiveKit's SpeechHandle the adapter reads."""

    def __init__(self, scheduled=True):
        self.scheduled = scheduled
        self.interrupted = False
        self.chat_items = []
        self._done = False

    def done(self):
        return self._done


@llm.function_tool
async def delegate(task: str) -> str:
    """Hand slow work to the background worker.

    Args:
        task: What to do.
    """
    return "ok"


def say(text):
    return [{"type": "text", "text": text}]


async def run_turn(model, ctx, tools=(), speech=None):
    token = _SpeechHandleContextVar.set(speech) if speech is not None else None
    try:
        stream = model.chat(chat_ctx=ctx, tools=list(tools))  # the stream's task copies this context
    finally:
        if token is not None:
            _SpeechHandleContextVar.reset(token)
    chunks = []
    async with stream:
        async for chunk in stream:
            chunks.append(chunk)
    return chunks


def text_of(chunks):
    return "".join(c.delta.content or "" for c in chunks if c.delta).strip()


def spoken(ctx, chunks, heard=None, interrupted=False):
    """The assistant item LiveKit adds once a reply has been (partly) spoken."""
    tag = next(c.delta.extra[TURN_TAG] for c in chunks if c.delta and c.delta.extra)
    return ctx.add_message(role="assistant", content=text_of(chunks) if heard is None else heard,
                           interrupted=interrupted, extra={TURN_TAG: tag})


def new_ctx():
    ctx = llm.ChatContext()
    ctx.add_message(role="system", content="You are Bee Call.", id=INSTRUCTIONS_ID)
    return ctx


def roles(params):
    return [m["role"] for m in params["messages"]]


def test_first_turn_sends_fixed_system_thinking_and_effort():
    async def main():
        client = FakeAnthropic([say("Hi! How was your day?")])
        model = ClaudeBedrockLLM(client=client, thinking="adaptive", effort="low")
        ctx = new_ctx()
        ctx.add_message(role="user", content="Hello?")
        chunks = await run_turn(model, ctx)
        params = client.calls[0]
        assert params["system"][0]["text"] == "You are Bee Call."
        assert params["thinking"] == {"type": "adaptive"} and params["output_config"] == {"effort": "low"}
        assert params["messages"] == [{"role": "user", "content": say("Hello?")}]
        assert text_of(chunks) == "Hi! How was your day?"
        assert [b["type"] for b in model.history[-1]["content"]] == ["thinking", "text"]
        assert client.problems == []

    asyncio.run(main())


def test_cut_off_reply_is_never_edited():
    async def main():
        full = "I can help with that. First, tell me the two offers."
        client = FakeAnthropic([say(full), say("Sure.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Help me decide on a job.")
        chunks = await run_turn(model, ctx)
        # LiveKit trims the item to what was actually heard.
        spoken(ctx, chunks, heard="I can help with that.", interrupted=True)
        ctx.add_message(role="user", content="Wait, one thing first.")
        await run_turn(model, ctx)
        second = client.calls[1]["messages"]
        assert second[0] == client.calls[0]["messages"][0]
        assert second[1]["content"][1] == {"type": "text", "text": full}
        assert second[2] == {"role": "user", "content": say("Wait, one thing first.")}
        assert second[3]["role"] == "system" and 'heard only this part of your reply: "I can help with that."' in second[3]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_tool_call_then_result_keeps_thinking_and_leads_with_tool_result():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_1", "name": "delegate", "input": {"task": "compare offers"}}
        client = FakeAnthropic([[tool_use], say("Started it.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Compare the two offers.")
        chunks = await run_turn(model, ctx, [delegate])
        calls = [tc for c in chunks if c.delta for tc in c.delta.tool_calls]
        assert [(tc.name, tc.call_id) for tc in calls] == [("delegate", "toolu_1")]
        assert client.calls[0]["tools"][0]["name"] == "delegate"
        ctx.items.append(llm.FunctionCall(call_id="toolu_1", name="delegate", arguments='{"task": "compare offers"}'))
        ctx.items.append(llm.FunctionCallOutput(call_id="toolu_1", name="delegate", output="Job 01 started.", is_error=False))
        await run_turn(model, ctx, [delegate])
        second = client.calls[1]["messages"]
        assert [b["type"] for b in second[1]["content"]] == ["thinking", "tool_use"]
        assert second[2] == {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Job 01 started.", "is_error": False}]}
        assert client.calls[1]["tools"] == client.calls[0]["tools"]
        assert client.problems == []

    asyncio.run(main())


def test_greeting_and_bee_note_sit_where_the_api_allows():
    async def main():
        # The note lands after the greeting: it waits for the caller's first words.
        client = FakeAnthropic([say("Great, let's start.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="assistant", content="Hi, it's Bee Call.")
        ctx.add_message(role="system", content="Note from the app, Bee context: ...")
        ctx.add_message(role="user", content="Hey.")
        await run_turn(model, ctx)
        assert roles(client.calls[0]) == ["user", "assistant", "user", "system"]
        assert client.calls[0]["messages"][0]["content"] == say(CONNECTED)
        assert client.calls[0]["messages"][1]["content"] == say("Hi, it's Bee Call.")

        # The note lands before the greeting: it goes between the connect line and the greeting.
        client = FakeAnthropic([say("Great.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="system", content="Note from the app, Bee context: ...")
        ctx.add_message(role="assistant", content="Hi, it's Bee Call.")
        ctx.add_message(role="user", content="Hey.")
        await run_turn(model, ctx)
        assert roles(client.calls[0]) == ["user", "system", "assistant", "user"]
        assert client.problems == []

    asyncio.run(main())


def test_job_note_with_no_new_words_follows_a_user_message():
    async def main():
        client = FakeAnthropic([say("Hello."), say("Your job is done.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Hi.")
        spoken(ctx, await run_turn(model, ctx))
        ctx.add_message(role="system", content="Note from the app: background job 01 finished. Result: 42")
        await run_turn(model, ctx)
        second = client.calls[1]["messages"]
        assert roles(client.calls[1]) == ["user", "assistant", "user", "system"]
        assert second[2]["content"] == say(NOTHING_NEW)
        assert client.problems == []

    asyncio.run(main())


def test_preemptive_reply_that_livekit_drops_leaves_no_trace():
    async def main():
        events = []
        client = FakeAnthropic([say("Congrats!"), say("Congrats on the offer!")])
        model = ClaudeBedrockLLM(client=client, on_event=lambda kind, **data: events.append(kind))
        base = new_ctx()
        # A preemptive reply to words the caller then kept adding to: never scheduled.
        early = base.copy()
        early.add_message(role="user", content="I got a")
        dropped = FakeSpeech(scheduled=False)
        await run_turn(model, early, speech=dropped)
        dropped._done = True
        base.add_message(role="user", content="I got a job offer today.")
        await run_turn(model, base, speech=FakeSpeech())
        assert client.calls[1]["messages"] == [{"role": "user", "content": say("I got a job offer today.")}]
        assert [m["role"] for m in model.history] == ["user", "assistant"]
        assert "claude_reply_dropped" in events
        assert client.problems == []

    asyncio.run(main())


def test_system_prompt_follows_the_instructions_until_the_first_reply_is_committed():
    """The call prep note can land in the instructions after an early reply LiveKit threw away (voice/prep.py)."""
    async def main():
        client = FakeAnthropic([say("Hi!"), say("Hello again."), say("Sure.")])
        model = ClaudeBedrockLLM(client=client)
        early = new_ctx()
        early.add_message(role="user", content="Hey")
        dropped = FakeSpeech(scheduled=False)
        await run_turn(model, early, speech=dropped)
        dropped._done = True
        ctx = new_ctx()
        ctx.items[0] = llm.ChatMessage(id=INSTRUCTIONS_ID, role="system", content=["You are Bee Call. Prep note."])
        ctx.add_message(role="user", content="Hey there.")
        chunks = await run_turn(model, ctx, speech=FakeSpeech())
        spoken(ctx, chunks)
        ctx.items[0] = llm.ChatMessage(id=INSTRUCTIONS_ID, role="system", content=["Changed after the first reply."])
        ctx.add_message(role="user", content="Can you help?")
        await run_turn(model, ctx, speech=FakeSpeech())
        systems = [call["system"][0]["text"] for call in client.calls]
        assert systems == ["You are Bee Call.", "You are Bee Call. Prep note.", "You are Bee Call. Prep note."]
        assert client.problems == []

    asyncio.run(main())


def test_reply_the_caller_never_heard_gets_a_note():
    async def main():
        client = FakeAnthropic([say("Let me think about your options here."), say("Sorry, go ahead.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="So about the offer.")
        speech = FakeSpeech()
        await run_turn(model, ctx, speech=speech)
        # The caller kept talking before any audio played, so LiveKit added no assistant item.
        speech.interrupted, speech._done = True, True
        ctx.add_message(role="user", content="And the salary is lower.")
        await run_turn(model, ctx)
        second = client.calls[1]["messages"]
        assert second[-1]["role"] == "system"
        assert 'did not hear your reply that began "Let me think about your options here."' in second[-1]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_tool_cut_off_before_it_ran_gets_a_stand_in_result():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_9", "name": "delegate", "input": {"task": "x"}}
        client = FakeAnthropic([say("Sure, I'll start that now.") + [tool_use], say("Okay.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Start a job.")
        speech = FakeSpeech()
        chunks = await run_turn(model, ctx, [delegate], speech=speech)
        speech.interrupted, speech._done = True, True
        speech.chat_items.append(spoken(ctx, chunks, heard="Sure,", interrupted=True))
        ctx.add_message(role="user", content="Actually, wait.")
        await run_turn(model, ctx, [delegate])
        user = client.calls[1]["messages"][2]
        assert user["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_9", "content": NOT_RUN, "is_error": True}
        assert user["content"][1] == {"type": "text", "text": "Actually, wait."}
        assert 'heard only this part of your reply: "Sure,"' in client.calls[1]["messages"][3]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_tool_still_running_gets_a_stand_in_then_a_late_result_note():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_5", "name": "delegate", "input": {"task": "x"}}
        client = FakeAnthropic([[tool_use], say("Still looking."), say("Found it.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Look it up.")
        await run_turn(model, ctx, [delegate])
        # LiveKit's stand-in pair for a tool that is still running, then the caller speaks.
        turn = ctx.copy()
        turn.items.append(llm.FunctionCall(id="fc_running", call_id="toolu_5", name="delegate", arguments="{}"))
        turn.items.append(llm.FunctionCallOutput(id="fc_running_output", call_id="toolu_5", name="delegate",
                                                 output="The tool call is still in progress.", is_error=False))
        turn.add_message(role="user", content="Are you there?")
        ctx.items = list(turn.items)
        await run_turn(model, turn, [delegate])
        assert client.calls[1]["messages"][2]["content"][0]["content"] == STILL_RUNNING
        # The real result arrives later.
        ctx.items = [i for i in ctx.items if not i.id.startswith("fc_running")]
        ctx.items.append(llm.FunctionCallOutput(call_id="toolu_5", name="delegate", output="Result: 42", is_error=False))
        ctx.add_message(role="user", content="So?")
        await run_turn(model, ctx, [delegate])
        third = client.calls[2]["messages"]
        assert third[-2] == {"role": "user", "content": say("So?")}
        assert "the delegate tool finished after the conversation moved on. Result: Result: 42" in third[-1]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_requests_run_one_at_a_time():
    async def main():
        client = FakeAnthropic([say("First answer."), say("Your job is done.")], first_delay=0.05)
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="First question.")
        relay = ctx.copy()
        relay.add_message(role="system", content="Note from the app: background job 01 finished.")
        await asyncio.gather(run_turn(model, ctx), run_turn(model, relay))
        # The second request waited for the first reply and was built on top of it.
        assert roles(client.calls[1]) == ["user", "assistant", "user", "system"]
        assert client.problems == []

    asyncio.run(main())


async def cut_off_mid_stream(model, ctx, tools=(), after=0.1):
    """Start a reply, then cancel it the way LiveKit does when the caller cuts in."""
    token = _SpeechHandleContextVar.set(FakeSpeech())
    try:
        stream = model.chat(chat_ctx=ctx, tools=list(tools))
    finally:
        _SpeechHandleContextVar.reset(token)
    chunks = []

    async def consume():
        async for chunk in stream:
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(after)
    await stream.aclose()
    await asyncio.gather(task, return_exceptions=True)
    return chunks


def test_reply_cut_off_mid_stream_goes_in_as_the_words_heard():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_3", "name": "delegate", "input": {"task": "x"}}
        # The words stream out at once, then Claude spends a while writing the tool input.
        client = FakeAnthropic([say("Hi there."), say("Sure, I'll start that now.") + [tool_use], say("Starting it.")],
                               tool_delay=5)
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Hi.")
        spoken(ctx, await run_turn(model, ctx, [delegate]))  # the same tools on every request, as in a call
        ctx.add_message(role="user", content="Start a job.")
        chunks = await cut_off_mid_stream(model, ctx, [delegate])
        assert len(model.history) == 2 and not any(c.delta and c.delta.tool_calls for c in chunks)
        spoken(ctx, chunks, heard="Sure, I'll", interrupted=True)
        ctx.add_message(role="user", content="Okay.")
        await run_turn(model, ctx, [delegate])
        third = client.calls[2]["messages"]
        assert [m["role"] for m in third] == ["user", "assistant", "user", "assistant", "user", "system"]
        assert third[2]["content"] == say("Start a job.") and third[3]["content"] == say("Sure, I'll")
        assert "The tool you were about to call did not run." in third[5]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_tool_reply_cut_off_mid_stream_keeps_one_tool_result():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_4", "name": "delegate", "input": {"task": "x"}}
        client = FakeAnthropic([[tool_use], say("Okay, it is running now and I will tell you when it is done."),
                                say("Sure.")], word_delay=0.05)
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Look it up.")
        await run_turn(model, ctx, [delegate])
        ctx.items.append(llm.FunctionCall(call_id="toolu_4", name="delegate", arguments="{}"))
        ctx.items.append(llm.FunctionCallOutput(call_id="toolu_4", name="delegate", output="Job 01 started.", is_error=False))
        chunks = await cut_off_mid_stream(model, ctx, [delegate], after=0.12)
        spoken(ctx, chunks, heard=text_of(chunks), interrupted=True)
        ctx.add_message(role="user", content="Wait.")
        await run_turn(model, ctx, [delegate])
        third = client.calls[2]["messages"]
        assert [m["role"] for m in third] == ["user", "assistant", "user", "assistant", "user", "system"]
        assert third[2]["content"][0]["tool_use_id"] == "toolu_4" and third[4]["content"] == say("Wait.")
        assert client.problems == []

    asyncio.run(main())


def test_bedrock_error_inside_the_stream_is_retried_when_nothing_was_said():
    async def main():
        response = httpx2.Response(200, request=httpx2.Request("POST", "https://bedrock-runtime.example"))
        body = {"type": "error", "error": {"type": "throttlingException", "message": "Too many requests"}}
        throttled = anthropic.APIStatusError(str(body), response=response, body=body)
        client = FakeAnthropic([throttled, say("Hello again.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Hello?")
        stream = model.chat(chat_ctx=ctx, conn_options=APIConnectOptions(max_retry=1, retry_interval=0.0, timeout=5))
        chunks = [c async for c in stream]
        await stream.aclose()
        assert text_of(chunks) == "Hello again." and len(client.calls) == 2
        assert client.problems == []

    asyncio.run(main())


def test_settle_commits_a_last_reply_at_the_end_of_the_call():
    async def main():
        client = FakeAnthropic([say("Bye!")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Bye.")
        speech = FakeSpeech(scheduled=False)  # a preemptive reply, used once the caller's turn ended
        await run_turn(model, ctx, speech=speech)
        assert model.history == []
        speech.scheduled = True
        model.settle()
        assert [m["role"] for m in model.history] == ["user", "assistant"]

    asyncio.run(main())


def test_tool_result_after_a_cut_off_reply_arrives_as_a_note():
    async def main():
        tool_use = {"type": "tool_use", "id": "toolu_6", "name": "delegate", "input": {"task": "x"}}
        client = FakeAnthropic([[tool_use], say("While that runs, tell me more about your day."), say("Got it.")],
                               word_delay=0.05)
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Look it up.")
        await run_turn(model, ctx, [delegate])
        # The tool is still running when the caller speaks; that reply is cut off mid-stream.
        ctx.add_message(role="user", content="Hello?")
        chunks = await cut_off_mid_stream(model, ctx, [delegate], after=0.12)
        spoken(ctx, chunks, heard=text_of(chunks), interrupted=True)
        # Only now does the tool finish, then the caller speaks again.
        ctx.items.append(llm.FunctionCallOutput(call_id="toolu_6", name="delegate", output="Job 01 started.", is_error=False))
        ctx.add_message(role="user", content="So?")
        await run_turn(model, ctx, [delegate])
        third = client.calls[2]["messages"]
        assert third[2]["content"][0]["content"] == NO_RESULT_YET
        assert "the delegate tool finished after the conversation moved on. Result: Job 01 started." in third[-1]["content"]
        assert client.problems == []

    asyncio.run(main())


def test_tool_list_stays_the_same_for_the_whole_call():
    async def main():
        client = FakeAnthropic([say("One."), say("Two.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="First.")
        spoken(ctx, await run_turn(model, ctx, [delegate]))
        ctx.add_message(role="user", content="Second.")
        await run_turn(model, ctx, [])  # a request that carries no tools, for example while draining
        assert client.calls[1]["tools"] == client.calls[0]["tools"]
        assert client.calls[1]["tool_choice"] == {"type": "none"}
        assert client.problems == []

    asyncio.run(main())


def test_check_request_flags_shapes_the_api_rejects():
    user = {"role": "user", "content": say("Hi")}
    reply = {"role": "assistant", "content": say("Hello")}
    note = {"role": "system", "content": "Note from the app: ..."}
    tool_reply = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}
    assert check_request([user, note]) == []
    assert check_request([user, note, reply, user]) == []
    assert check_request([user, reply, note, user])  # a note after an assistant turn, then a user turn
    assert check_request([user, tool_reply, user])  # no tool_result
    assert check_request([user, tool_reply, note])  # a note between tool_use and tool_result
    assert check_request([user, reply])  # ends with an assistant message
