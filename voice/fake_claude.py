"""Test stand-in for the Anthropic client: scripted Claude replies, no network.

It checks each request the way the Messages API would: message order, where
system notes sit, and tool results. It also signs each reply's thinking block
with a digest of the system prompt, the tools and the messages before it, and
checks that digest whenever the reply comes back in a later request. A request
that edited earlier history fails that check, the way a replayed thinking block
fails on Bedrock. A scripted reply can also be an exception, raised from inside
the stream the way Bedrock reports errors mid-stream.

Used by the tests, and by the voice server with COORDINATOR_LLM=fake to run the
real Claude adapter through LiveKit (barge-in, preemptive replies, tools)
while Claude on Bedrock is not available. It is a test scaffold, not part of
the product.
"""

import asyncio
import copy
import hashlib
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from voice.claude_llm import check_request

Blocks = list[dict[str, Any]]


def digest(params: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    bound = {"system": params.get("system"), "tools": params.get("tools"), "messages": messages}
    return "sig-" + hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()[:16]


def check_thinking(params: dict[str, Any]) -> list[str]:
    problems = []
    messages = params["messages"]
    for i, msg in enumerate(messages):
        if msg["role"] != "assistant" or not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") == "thinking" and block.get("signature") != digest(params, messages[:i]):
                problems.append(f"message {i}: thinking block no longer matches the history before it")
    return problems


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg["role"] == "user":
            texts = [b["text"] for b in msg["content"] if b.get("type") == "text"]
            return texts[-1] if texts else ""
    return ""


def voice_test_policy(params: dict[str, Any]) -> Blocks:
    """Replies for live voice tests: long enough to cut off, and a delegate call on "test job"."""
    messages = params["messages"]
    last = messages[-1]
    if last["role"] == "system" and "background job" in last["content"] and " failed (" in last["content"]:
        return [{"type": "text", "text": "Sorry, the test job failed. Want me to try again?"}]
    if last["role"] == "system" and "background job" in last["content"]:
        result = last["content"].split("Result: ", 1)[1].strip() if "Result: " in last["content"] else "It is done."
        return [{"type": "text", "text": f"Your test job is done. {result} The details are in the app."}]
    if last["role"] == "user" and any(b.get("type") == "tool_result" for b in last["content"]):
        return [{"type": "text", "text": "Okay, it's running. I'll tell you when it's done."}]
    heard = last_user_text(messages)
    tool_names = {t["name"] for t in params.get("tools", [])}
    if "test job" in heard.lower() and "delegate" in tool_names:
        return [{"type": "text", "text": "Sure, I'll start that now."},
                {"type": "tool_use", "id": f"toolu_{len(messages):03d}", "name": "delegate", "input": {"task": heard}}]
    return [{"type": "text", "text": f'You said: "{heard}". I will keep talking for a while, so that you can '
                                     "try cutting me off in the middle of this sentence."}]


class FakeBlock:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.type = data["type"]

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        return copy.deepcopy(self._data)


class FakeStream:
    def __init__(self, blocks: Blocks, first_delay: float, word_delay: float, tool_delay: float = 0.0,
                 error: BaseException | None = None) -> None:
        self.blocks = blocks
        self.first_delay = first_delay
        self.word_delay = word_delay
        self.tool_delay = tool_delay  # time spent writing a tool's input, after the tool block starts
        self.error = error

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self):
        async def events():
            await asyncio.sleep(self.first_delay)
            if self.error is not None:
                raise self.error
            for block in self.blocks:
                if block["type"] == "text":
                    for word in block["text"].split(" "):
                        await asyncio.sleep(self.word_delay)
                        yield SimpleNamespace(type="text", text=word + " ")
                elif block["type"] == "tool_use":
                    yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(**block))
                    await asyncio.sleep(self.tool_delay)
        return events()

    async def get_final_message(self) -> SimpleNamespace:
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in self.blocks) else "end_turn"
        return SimpleNamespace(content=[FakeBlock(b) for b in self.blocks], stop_reason=stop, usage=usage)


class FakeAnthropic:
    """Duck-types `client.messages.stream(**params)` of the Anthropic SDK."""

    def __init__(self, replies: list[Blocks | BaseException] | None = None, *,
                 policy: Callable[[dict[str, Any]], Blocks] = voice_test_policy, first_delay: float = 0.0,
                 word_delay: float = 0.0, tool_delay: float = 0.0, log_path: Path | None = None) -> None:
        self.messages = self
        self.replies = deque(replies or [])
        self.policy = policy
        self.first_delay = first_delay
        self.word_delay = word_delay
        self.tool_delay = tool_delay
        self.log_path = log_path
        self.calls: list[dict[str, Any]] = []
        self.problems: list[tuple[int, str]] = []

    def stream(self, **params: Any) -> FakeStream:
        params = copy.deepcopy(params)
        index = len(self.calls)
        self.calls.append(params)
        problems = check_request(params["messages"]) + check_thinking(params)
        self.problems += [(index, p) for p in problems]
        scripted = self.replies.popleft() if self.replies else self.policy(params)
        if isinstance(scripted, BaseException):
            return FakeStream([], self.first_delay, 0.0, error=scripted)
        thinking = {"type": "thinking", "thinking": "", "signature": digest(params, params["messages"])}
        blocks = [thinking] + [b for b in copy.deepcopy(scripted) if b["type"] != "thinking"]
        if self.log_path is not None:
            with self.log_path.open("a") as f:
                f.write(json.dumps({"request": index, "roles": [m["role"] for m in params["messages"]],
                                    "tail": params["messages"][-3:], "problems": problems,
                                    "reply": [b for b in blocks if b["type"] != "thinking"]}, ensure_ascii=False) + "\n")
        return FakeStream(blocks, self.first_delay, self.word_delay, self.tool_delay)
