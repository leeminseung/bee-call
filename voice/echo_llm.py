"""Test stand-in for the coordinator while Claude on Bedrock is not available.

It answers by repeating the last thing the user said, so the voice path
(LiveKit, Transcribe, Polly, turn-taking, recording) can be tested on its own.
If the caller says "test job", it calls the `delegate` tool with what it heard,
so the delegate -> worker -> relay flow can be tested without a real model.
It is a test scaffold, not part of the product; the Claude adapter replaces it.
"""

import json
import uuid
from typing import Any

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

TRIGGER = "test job"


class EchoLLM(llm.LLM):
    @property
    def model(self) -> str:
        return "echo"

    @property
    def provider(self) -> str:
        return "test"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "EchoStream":
        return EchoStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


def _tool_names(tools: list[llm.Tool]) -> set[str]:
    return {getattr(getattr(t, "info", None), "name", None) for t in tools} - {None}


class EchoStream(llm.LLMStream):
    async def _run(self) -> None:
        items = self._chat_ctx.items
        chunk_id = uuid.uuid4().hex
        if items and getattr(items[-1], "type", None) == "function_call_output":
            reply = "Okay, I started the test job. I'll tell you when it's done."
            self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant", content=reply)))
            return

        last_user = next((i for i in reversed(items) if getattr(i, "role", None) == "user"), None)
        heard = (last_user.text_content or "").strip() if last_user else ""
        if TRIGGER in heard.lower() and "delegate" in _tool_names(self._tools):
            call = llm.FunctionToolCall(name="delegate", arguments=json.dumps({"task": heard}),
                                        call_id=f"call_{uuid.uuid4().hex[:12]}")
            self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant", tool_calls=[call])))
            return

        reply = f"You said: {heard}" if heard else "Sorry, I didn't catch that."
        self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant", content=reply)))
