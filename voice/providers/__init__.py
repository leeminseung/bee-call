"""Model providers: one small adapter per provider, voice/providers/<provider>.py, the only code that speaks that
provider's API. The model catalog (voice/model_catalog.py) says which provider runs each role's model; everything
else asks for a role and gets an answer, in one shape whatever the provider.

An adapter offers what its provider can do, among:
  reply(choice, system, turns, tools, *, on_text=None, usage=None) -> Reply
      one model request: the conversation so far (Turn), the tools it may call (ToolSpec), the answer as text and
      tool calls; on_text gets the text so far as it streams, and `usage` (a Usage) is added to as the provider
      counts, also for a request cut short (then an estimate, marked so).
  call_llm(choice, *, on_event, refusal_line) -> a LiveKit LLM for the call AI
  worker(choice) -> a background worker (worker/runner.py Backend)
An adapter module's ADAPTER is the adapter; a module is found by its provider's name, so adding a provider is its
module and its catalog entries. Tests register stand-ins (register()).
"""

import importlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from voice import model_catalog
from voice.model_catalog import Choice

log = logging.getLogger("bee-call.providers")


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # a JSON Schema object


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    id: str  # the ToolCall's
    text: str
    error: bool = False


@dataclass
class Turn:
    """One message of a conversation: the user's text and tool results, or the assistant's text and tool calls."""
    role: str  # user or assistant
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    # The provider's own form of an assistant turn (Claude's thinking blocks, which must go back unchanged), with
    # the provider's name; another provider uses text and calls instead.
    native: Any = None
    native_provider: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0  # every prompt token, cache reads and writes among them
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    estimated: bool = False  # some of it was counted from what was sent: the provider's own count never came
    server_tools: dict[str, int] = field(default_factory=dict)  # server tool uses the provider charges for

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.estimated = self.estimated or other.estimated
        for name, count in other.server_tools.items():
            self.server_tools[name] = self.server_tools.get(name, 0) + count


@dataclass
class Reply:
    turn: Turn
    stop: str  # end, tool_use, max_tokens, refusal, pause, or the provider's own word
    usage: Usage
    model: str  # the provider's model id that answered, as prices name it
    provider: str
    instead_of: str | None = None  # the choice that failed, when the standby answered
    reason: str | None = None  # why it failed


def estimated_tokens(*parts: Any) -> int:
    """About how many tokens these are, for a request cut short before the provider counted it: four bytes of UTF-8 a
    token (English runs about four characters a token, Korean about one character)."""
    return sum(len(json.dumps(part, ensure_ascii=False, default=str).encode()) for part in parts) // 4


_ADAPTERS: dict[str, Any] = {}


def register(name: str, adapter: Any) -> None:
    """Puts an adapter in place of the module's (tests)."""
    _ADAPTERS[name] = adapter


def unregister(name: str) -> None:
    _ADAPTERS.pop(name, None)


def adapter(name: str) -> Any:
    """The provider's adapter: one registered, or voice/providers/<name>.py's ADAPTER."""
    if name not in _ADAPTERS:
        _ADAPTERS[name] = importlib.import_module(f"voice.providers.{name}").ADAPTER
    return _ADAPTERS[name]


async def reply(choice: Choice, system: str, turns: list[Turn], tools: list[ToolSpec] = (), *,
                on_text: Callable[[str], None] | None = None, usage: Usage | None = None) -> Reply:
    """One request to a choice's model, through its provider's adapter."""
    return await adapter(choice.offering.provider).reply(choice, system, list(turns), list(tools), on_text=on_text,
                                                         usage=usage)


async def answer(role_name: str, system: str, turns: list[Turn], tools: list[ToolSpec] = (), *,
                 model_id: str | None = None, on_text: Callable[[str], None] | None = None,
                 usage: Usage | None = None) -> Reply:
    """The role's model answers (its choice `model_id`, or its default). When its request fails before it streamed
    any text, the role's standby answers instead, and the reply says so (instead_of, reason)."""
    first = model_catalog.choice(role_name, model_id)
    streamed = False

    def seen(text: str) -> None:
        nonlocal streamed
        streamed = True
        on_text(text)

    try:
        return await reply(first, system, turns, tools, on_text=seen if on_text else None, usage=usage)
    except Exception as exc:
        standby = model_catalog.role(role_name).standby
        if standby is None or streamed:
            raise
        why = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("%s on %s failed (%s); %s answers", role_name, first.offering.model, why, standby.offering.model)
        found = await reply(standby, system, turns, tools, on_text=on_text, usage=usage)
        found.instead_of, found.reason = first.id, why
        return found
