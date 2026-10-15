"""Anthropic's Messages API (ANTHROPIC_API_KEY), for the model catalog's Claude choices.

reply() streams one request: the choice's model, max_tokens, thinking and effort; the conversation with each Claude
turn sent back exactly as it came (thinking blocks and their signatures must not change); client tools as
Anthropic's tool definitions.
"""

import os
from typing import Any, Callable

from voice.model_catalog import Choice
from voice.providers import Reply, ToolCall, ToolSpec, Turn, Usage, estimated_tokens

PROVIDER = "anthropic"
STOPS = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal",
         "pause_turn": "pause", "stop_sequence": "end"}
TIMEOUT_S = 120.0  # one request, its answer included (the call AI has its own, shorter, in voice/agent.py)


class Anthropic:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def ready(self) -> bool:
        """Set up here: a stand-in client, or ANTHROPIC_API_KEY."""
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
                                                    max_retries=1, timeout=anthropic.Timeout(TIMEOUT_S, connect=5.0))
        return self._client

    async def reply(self, choice: Choice, system: str, turns: list[Turn], tools: list[ToolSpec], *,
                    on_text: Callable[[str], None] | None = None, usage: Usage | None = None) -> Reply:
        params: dict[str, Any] = {"model": choice.offering.model, "max_tokens": choice.max_tokens, "system": system,
                                  "messages": [message(turn) for turn in turns]}
        if tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in tools]
        if choice.thinking:
            params["thinking"] = {"type": choice.thinking}
        if choice.effort:
            params["output_config"] = {"effort": choice.effort}
        params.update(choice.extra.get("anthropic", {}))
        text, answering, counted = "", False, False
        try:
            async with self.client().messages.stream(**params) as stream:
                async for event in stream:
                    answering = True
                    delta = getattr(event, "delta", None)
                    if getattr(event, "type", None) == "content_block_delta" and getattr(delta, "type", None) == "text_delta":
                        text += delta.text
                        if on_text:
                            on_text(text)
                final = await stream.get_final_message()
            used = counted_usage(final.usage)
            counted = True
        finally:
            if answering and not counted and usage is not None:  # cut short: what was sent and said, estimated
                usage.add(Usage(input_tokens=estimated_tokens(system, params["messages"]),
                                output_tokens=estimated_tokens(text), estimated=True))
        if usage is not None:
            usage.add(used)
        content = [block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
                   for block in final.content]
        said = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
        return Reply(Turn("assistant", said, calls, native=content, native_provider=PROVIDER),
                     STOPS.get(final.stop_reason, final.stop_reason or "end"), used, choice.offering.model, PROVIDER)


def counted_usage(usage: Any) -> Usage:
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    tools = getattr(usage, "server_tool_use", None)
    server = {name: count for name, count in (("web_search", getattr(tools, "web_search_requests", 0)),
                                              ("web_fetch", getattr(tools, "web_fetch_requests", 0))) if count}
    return Usage(input_tokens=(getattr(usage, "input_tokens", 0) or 0) + read + write,
                 output_tokens=getattr(usage, "output_tokens", 0) or 0, cache_read=read, cache_write=write,
                 server_tools=server)


def message(turn: Turn) -> dict[str, Any]:
    """A Turn as a Messages API message: a Claude turn as it came, any other from its text and calls."""
    if turn.role == "assistant":
        if turn.native is not None and turn.native_provider == PROVIDER:
            return {"role": "assistant", "content": turn.native}
        content: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text.strip() else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in turn.calls]
        return {"role": "assistant", "content": content}
    content = [{"type": "tool_result", "tool_use_id": r.id, "content": r.text, **({"is_error": True} if r.error else {})}
               for r in turn.results]
    if turn.text:
        content.append({"type": "text", "text": turn.text})
    return {"role": "user", "content": content}


ADAPTER = Anthropic()
