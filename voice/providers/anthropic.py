"""Anthropic's Messages API (ANTHROPIC_API_KEY), for the model catalog's Claude choices.

reply() streams one request: the choice's model, max_tokens, thinking and effort; the conversation with each Claude
turn sent back exactly as it came (thinking blocks and their signatures, server tool results and their encrypted
content must not change); client tools as Anthropic's tool definitions. stream() is that one request for callers
that add more of the API's own (the Claude worker, worker/claude.py: server tools and its container).
"""

import os
from typing import Any, Callable

from voice.model_catalog import Choice
from voice.providers import Reply, ToolCall, ToolSpec, Turn, Usage, estimated_tokens

PROVIDER = "anthropic"
STOPS = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal",
         "pause_turn": "pause", "stop_sequence": "end"}
TIMEOUT_S = 120.0  # one request, its answer included
# The result blocks of the server tools usage.server_tool_use counts, by the tool's name there.
RESULTS_CHARGED = {"web_search_tool_result": "web_search", "web_fetch_tool_result": "web_fetch"}
# The call AI's requests: the SDK does not retry (a failed first request goes to the standby at once, and LiveKit
# retries later ones); no route shows within 3 s.
CALL_TIMEOUT_S, CALL_CONNECT_S = 20.0, 3.0


def async_client(**options: Any) -> Any:
    """Anthropic's async client (tests put a stand-in here)."""
    import anthropic

    return anthropic.AsyncAnthropic(**options)


def api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


class Anthropic:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def setup_problem(self) -> str | None:
        """Why this provider cannot be used here, or None."""
        return None if self._client is not None or api_key() else "ANTHROPIC_API_KEY is not set"

    def ready(self) -> bool:
        return self.setup_problem() is None

    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = async_client(api_key=api_key() or None, max_retries=1,
                                        timeout=anthropic.Timeout(TIMEOUT_S, connect=5.0))
        return self._client

    def worker(self, choice: Choice) -> Any:
        """Background jobs on Claude, with Anthropic's server tools (worker/claude.py)."""
        from worker.claude import ClaudeWorker

        return ClaudeWorker(choice)

    def call_llm(self, choice: Choice, *, on_event: Callable[..., None], refusal_line: str) -> Any:
        """The call AI on Claude: voice/claude_llm.py's adapter for LiveKit, its conversation cached as it grows,
        and `refusal_line` said when Claude declines a turn."""
        import anthropic

        from voice.claude_llm import ClaudeBedrockLLM

        client = async_client(api_key=api_key(), max_retries=0,
                              timeout=anthropic.Timeout(CALL_TIMEOUT_S, connect=CALL_CONNECT_S))
        return ClaudeBedrockLLM(client=client, model=choice.offering.model, thinking=choice.thinking,
                                effort=choice.effort, auto_cache=True, refusal_line=refusal_line, on_event=on_event)

    def params(self, choice: Choice, system: str, turns: list[Turn], tools: list[ToolSpec]) -> dict[str, Any]:
        """A Messages API request for a choice: its model, max_tokens, thinking, effort and other fields."""
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
        return params

    async def stream(self, params: dict[str, Any], *, on_text: Callable[[str], None] | None = None,
                     usage: Usage | None = None, client: Any = None) -> Any:
        """One streamed request: the final message. `usage` is added what the API counted, or, for a request cut
        short once its answer began, an estimate: what was sent, all the model wrote (text, thinking, the input of
        the tools it called) and the searches that already ran."""
        text, answering, counted = "", False, False
        written: list[str] = []
        ran: dict[str, int] = {}
        try:
            async with (client or self.client()).messages.stream(**params) as stream:
                async for event in stream:
                    answering = True
                    kind, delta = getattr(event, "type", None), getattr(event, "delta", None)
                    if kind == "content_block_delta":
                        part = getattr(delta, "type", None)
                        if part == "text_delta":
                            text += delta.text
                            written.append(delta.text)
                            if on_text:
                                on_text(text)
                        elif part == "thinking_delta":
                            written.append(getattr(delta, "thinking", "") or "")
                        elif part == "input_json_delta":
                            written.append(getattr(delta, "partial_json", "") or "")
                    elif kind == "content_block_start":
                        tool = RESULTS_CHARGED.get(getattr(getattr(event, "content_block", None), "type", None))
                        if tool:  # a server tool that ran (searches are charged)
                            ran[tool] = ran.get(tool, 0) + 1
                final = await stream.get_final_message()
            used = counted_usage(final.usage)
            counted = True
        finally:
            if answering and not counted and usage is not None:  # cut short: what was sent and written, estimated
                usage.add(Usage(input_tokens=estimated_tokens(params.get("system"), params["messages"]),
                                output_tokens=estimated_tokens("".join(written)), estimated=True, server_tools=ran))
        if usage is not None:
            usage.add(used)
        return final

    async def reply(self, choice: Choice, system: str, turns: list[Turn], tools: list[ToolSpec], *,
                    on_text: Callable[[str], None] | None = None, usage: Usage | None = None) -> Reply:
        used = Usage()
        try:
            final = await self.stream(self.params(choice, system, turns, tools), on_text=on_text, usage=used)
        finally:
            if usage is not None:
                usage.add(used)
        content = content_of(final)
        said = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
        return Reply(Turn("assistant", said, calls, native=content, native_provider=PROVIDER),
                     STOPS.get(final.stop_reason, final.stop_reason or "end"), used, choice.offering.model, PROVIDER)


def content_of(final: Any) -> list[dict[str, Any]]:
    """A message's content blocks as the SDK itself sends response blocks back: every field the API sent, null ones
    too, and none it did not."""
    return [block.model_dump(mode="json", exclude_unset=True, by_alias=True) if hasattr(block, "model_dump")
            else dict(block) for block in final.content]


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
