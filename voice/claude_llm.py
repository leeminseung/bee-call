"""Claude as the coordinator's model, for LiveKit Agents: Claude Haiku 5.5 on Anthropic's own API (AsyncAnthropic,
COORDINATOR_LLM=anthropic) or Claude Sonnet 5.5 on Bedrock (AsyncAnthropicBedrock, COORDINATOR_LLM=claude); see
make_llm in voice/agent.py. The same history rules hold for both.

Why our own adapter instead of the LiveKit AWS plugin:
- Claude 5.5 needs its thinking blocks sent back unchanged, signature included.
  The plugin drops them.
- Editing an earlier turn invalidates its thinking blocks (accounts created
  after 2026-08-31 get a 400 on the replay). LiveKit trims an interrupted reply
  down to what was heard, which is an edit. So this adapter keeps its own
  history of what Claude actually produced, and tells Claude what the user
  heard with a note instead of changing the old turn.
- It calls InvokeModel through the Anthropic SDK (AsyncAnthropicBedrock): the
  Messages API body passes through, including output_config (effort) and
  mid-conversation system messages, which Converse does not carry.

History rules (one adapter instance per call):
- The agent instructions become the fixed system prompt, and the first tool
  list is sent with every request; neither changes during a call, because both
  are part of what each thinking block is checked against. Tool use is turned
  off with tool_choice "none", never by leaving the tools out. Until the first
  reply is committed nothing has been checked against the system prompt, so it
  follows the instructions: the call prep note can land after an early reply
  LiveKit threw away (voice/prep.py).
- `history` holds committed messages and only grows. A reply is committed once
  LiveKit schedules it to be spoken. A preemptive reply LiveKit throws away
  (the caller kept adding words) is dropped together with the messages that led
  to it; no later request saw it. This relies on LiveKit discarding a preemptive
  reply whenever the chat context changed first, which every other request here
  does (a note, a tool result).
- Everything after the last committed reply is rebuilt from LiveKit's chat
  context for each request: one user message (tool results first, then the
  caller's words), then one system message with the app's notes. The API takes
  a mid-conversation system message only right after a user message, and only
  as the last message or just before an assistant turn.
- LiveKit's assistant items are matched to Claude's replies by a tag that
  LiveKit copies from our chunks into the item's `extra`. If the caller cut a
  reply off, or never heard it, a note says so; the reply is never changed.
  A reply cut off while Claude was still streaming was never recorded, so the
  words the caller heard go in as a plain assistant turn, with a note. What the
  caller heard is checked against a reply's words without its audio tags
  (voice/audio_tags.py), which LiveKit's item never holds: a reply of only a
  tag, such as "[laughs]", has no item and is not one the caller missed.
- Every tool_use gets a tool_result in the next user message. A tool that was
  cancelled or is still running gets a stand-in result; its real result, if it
  comes, arrives later as a note.
- A turn Claude declines (stop_reason "refusal": a safety classifier or Claude
  itself) is never committed, and words of it that went out are discarded. The
  caller hears a short line instead (tagged as the app's), and what came in with
  the declined request (the caller's words, tool results, notes) is never sent
  again, with a note in its place and a stand-in tool result: sent again, it
  would most likely be declined turn after turn. Like a reply, a decline takes
  effect once LiveKit plays its line.
- The system prompt carries a cache breakpoint; with `auto_cache` (Anthropic's
  API) the request also carries one on its last block, so the history the call
  has built up is read from the cache by the next request.
- One request at a time, so each reply is filed under the history it was
  generated from.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
from anthropic import AsyncAnthropicBedrock
from livekit.agents import APIConnectionError, APIStatusError, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

from voice.audio_tags import strip_tags

try:  # private in LiveKit Agents (pinned to 1.8.6): the speech a reply is generated for
    from livekit.agents.voice.agent_activity import _SpeechHandleContextVar
except ImportError:  # pragma: no cover
    _SpeechHandleContextVar = None

log = logging.getLogger("bee-call.claude")

INSTRUCTIONS_ID = "lk.agent_task.instructions"
TURN_TAG = "bee_call_turn"
REFUSAL_TAG = "bee_call_refusal"  # on the app's line after Claude declined a turn
REFUSAL_LINE = "Sorry, I can't help with that one."
RUNNING_OUTPUT_SUFFIX = "_running_output"  # id of LiveKit's stand-in output for a tool still running
# Errors Bedrock sends inside an open stream arrive with status 200; these are worth a retry.
TRANSIENT_STREAM_ERRORS = {"throttlingException", "serviceUnavailableException", "modelStreamErrorException",
                           "internalServerException", "modelTimeoutException", "overloaded_error", "api_error"}

CONNECTED = "(The phone call has connected.)"
NOTHING_NEW = "(The caller has not said anything new.)"
STILL_RUNNING = "Still running when this turn began. If a result comes back, it will arrive as a note from the app."
NOT_RUN = "Did not run: the reply was cut off before the tool started."
NO_RESULT_YET = "No result yet. If one comes back, it will arrive as a note from the app."
LEFT_OUT = "The result came back but is left out: your reply to it was stopped by a safety check."


def default_model(region: str) -> str:
    from voice.model_catalog import bedrock_claude

    return bedrock_claude(region)


def current_speech() -> Any | None:
    return _SpeechHandleContextVar.get(None) if _SpeechHandleContextVar is not None else None


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_result(tool_id: str, output: str, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": is_error}


def _norm(text: str) -> str:
    return " ".join(text.split())


def _opening(text: str, limit: int = 60) -> str:
    text = _norm(text)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _spoken(speech: Any, tag: str) -> bool:
    return any(getattr(i, "type", None) == "message" and i.role == "assistant"
               and (getattr(i, "extra", None) or {}).get(TURN_TAG) == tag for i in speech.chat_items)


def check_request(messages: list[dict[str, Any]]) -> list[str]:
    """Message-order problems the Messages API rejects, found before sending. Empty when fine."""
    if not messages:
        return ["no messages"]
    problems = []
    if messages[0]["role"] != "user":
        problems.append("the first message is not a user message")
    if messages[-1]["role"] == "assistant":
        problems.append("the last message is an assistant message")
    for i, msg in enumerate(messages):
        prev = messages[i - 1] if i else None
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        blocks = msg["content"] if isinstance(msg["content"], list) else []
        if msg["role"] == "system":
            if prev is None or prev["role"] != "user":
                problems.append(f"message {i}: a system note must follow a user message")
            if nxt is not None and nxt["role"] != "assistant":
                problems.append(f"message {i}: a system note must be last or followed by an assistant turn")
        tool_ids = [b["id"] for b in blocks if b.get("type") == "tool_use"]
        if msg["role"] == "assistant" and tool_ids and nxt is not None:
            lead = []
            for b in nxt["content"] if nxt["role"] == "user" and isinstance(nxt["content"], list) else []:
                if b.get("type") != "tool_result":
                    break
                lead.append(b["tool_use_id"])
            if sorted(lead) != sorted(tool_ids):
                problems.append(f"message {i}: the next message must open with a tool_result for each tool_use")
        if msg["role"] == "user":
            results = [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"]
            asked = {b["id"] for b in prev["content"] if b.get("type") == "tool_use"} \
                if prev is not None and prev["role"] == "assistant" else set()
            if any(r not in asked for r in results):
                problems.append(f"message {i}: tool_result without a matching tool_use just before it")
    return problems


@dataclass
class Draft:
    """What one request adds after the committed history, and what that accounts for."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    item_ids: set[str] = field(default_factory=set)  # LiveKit items the messages came from
    settled: set[str] = field(default_factory=set)  # replies whose hearing these messages settle
    words: set[str] = field(default_factory=set)  # the items of the caller's words among them
    results: set[str] = field(default_factory=set)  # the tool calls whose results they carry


@dataclass
class Reply:
    id: str
    content: list[dict[str, Any]]  # Claude's blocks, exactly as returned
    text: str
    tools: dict[str, str]  # tool_use id -> tool name
    speech: Any | None  # LiveKit's SpeechHandle, when known
    draft: Draft
    tools_sent: bool = True  # False when the reply was cut short and its tool calls never went to LiveKit

    @property
    def said(self) -> str:
        """The reply's words as the caller hears them and LiveKit's item for it holds them: without audio tags."""
        return strip_tags(self.text)


class ClaudeBedrockLLM(llm.LLM):
    def __init__(self, *, profile: str | None = None, region: str = "ap-southeast-2", model: str | None = None,
                 thinking: str | None = None, effort: str | None = None, max_tokens: int = 4096,
                 client: Any | None = None, on_event: Callable[..., None] | None = None, auto_cache: bool = False,
                 refusal_line: str = REFUSAL_LINE) -> None:
        super().__init__()
        self._model = model or os.environ.get("COORDINATOR_MODEL") or default_model(region)
        # "adaptive" (thinking on, the default), "between_tools" (no up-front thinking; Claude Sonnet 5.5 only) or
        # "disabled" (Claude Haiku 5.5, for speed; accepted at effort high or below).
        self._thinking = thinking or os.environ.get("COORDINATOR_THINKING", "adaptive")
        if self._thinking == "between_tools" and "sonnet-5-5" not in self._model:
            self._thinking = "disabled"  # every other model answers "between_tools" with a 400
        # One level per call: on Bedrock, Sonnet 5.5 cannot change effort mid-conversation.
        self._effort = effort or os.environ.get("COORDINATOR_EFFORT", "low")
        self._max_tokens = max_tokens
        self._client = client or AsyncAnthropicBedrock(aws_profile=profile, aws_region=region)
        # Anthropic's automatic caching: a cache breakpoint on the last block, so the history the call has built
        # up is read from the cache by the next request (on top of the system prompt's own breakpoint).
        self._auto_cache = auto_cache
        # Said when Claude declines a turn (stop_reason "refusal"), in the call's language (voice/language.py).
        self._refusal_line = refusal_line
        self.on_event = on_event
        self.system_prompt: str | None = None
        self.history: list[dict[str, Any]] = []
        self._committed: set[str] = set()
        self._replies: dict[str, Reply] = {}
        self._unsettled: list[str] = []  # committed replies with words, not yet known heard or not
        self._last: Reply | None = None  # the last committed reply; its tool_use ids need results
        self._pending: Reply | None = None  # generated, waiting for LiveKit to schedule it
        self._cut_short: dict[str, bool] = {}  # replies stopped mid-stream after words went out -> a tool had started
        self._declined: set[str] = set()  # what came in with a turn Claude declined: never sent again
        self._declined_results: set[str] = set()  # tool calls whose result was declined: LEFT_OUT stands in
        self._pending_decline: tuple[str, Draft, Any] | None = None  # a decline waiting for LiveKit to play its line
        self._tools_schema: list[dict[str, Any]] | None = None  # the call's tool list, sent unchanged every time
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "anthropic" if isinstance(self._client, anthropic.AsyncAnthropic) else "aws-bedrock"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> "ClaudeStream":
        return ClaudeStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                            tool_choice=tool_choice)

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, **data)
            except Exception:
                log.exception("on_event failed")

    # ---------- history ----------

    def draft(self, chat_ctx: llm.ChatContext) -> Draft:
        """The messages a request built from this chat context adds after the committed history."""
        open_tools = self._last.tools if self._last else {}
        draft = Draft()
        results: dict[str, dict[str, Any]] = {}
        stand_ins: set[str] = set()
        words: list[dict[str, Any]] = []
        notes: list[str] = []

        def close() -> None:
            # A user message, then one system message with the app's notes. The first user message
            # opens with a result for each tool the last committed reply called.
            content: list[dict[str, Any]] = []
            if not draft.messages:
                never_ran = bool(self._last and (not self._last.tools_sent or (
                    self._last.speech is not None and self._last.speech.interrupted)))
                for tool_id in open_tools:
                    if tool_id in self._declined_results and tool_id not in results:
                        content.append(_tool_result(tool_id, LEFT_OUT))
                        continue
                    content.append(results.get(tool_id) or _tool_result(
                        tool_id, NOT_RUN if never_ran else NO_RESULT_YET, is_error=never_ran))
            content += words
            words.clear()
            if not content:
                content = [_text(CONNECTED if not self.history and not draft.messages else NOTHING_NEW)]
            draft.messages.append({"role": "user", "content": content})
            if notes:
                draft.messages.append({"role": "system", "content": "\n\n".join(notes)})
                notes.clear()

        for item in chat_ctx.items:
            if item.id in self._committed or item.id in self._declined:
                continue
            kind = getattr(item, "type", None)
            if kind == "message" and item.id == INSTRUCTIONS_ID:
                if self.system_prompt is None or not self.history:
                    self.system_prompt = item.text_content or ""
                continue
            draft.item_ids.add(item.id)
            if kind == "message":
                text = item.text_content or ""
                if item.role in ("system", "developer"):
                    if text:
                        notes.append(text)
                elif item.role == "user":
                    if text:
                        words.append(_text(text))
                        draft.words.add(item.id)
                elif item.role == "assistant":
                    if (getattr(item, "extra", None) or {}).get(REFUSAL_TAG):
                        # The app's line after Claude declined a turn. Words of the declined reply that went out
                        # before it are discarded, as the API asks, and so are the caller's words it declined.
                        notes.append("Note from the app: your reply here was stopped by a safety check, so the app "
                                     f'said this to the caller instead: "{self._refusal_line}". What came in before '
                                     "it (the caller's words, tool results, notes) is left out of this conversation.")
                        continue
                    tag = (getattr(item, "extra", None) or {}).get(TURN_TAG)
                    reply = self._replies.get(tag) if tag else None
                    if reply is not None:
                        if tag in self._unsettled and tag not in draft.settled:
                            draft.settled.add(tag)
                            if item.interrupted and _norm(text) != _norm(reply.said):
                                notes.append("Note from the app: the caller cut you off. "
                                             f'They heard only this part of your reply: "{text}"')
                    elif tag or (not self.history and not draft.messages):
                        # Words the caller heard that are not a recorded reply: Claude's reply cut off while it
                        # was still streaming, or the app's greeting before Claude's first reply.
                        close()
                        draft.messages.append({"role": "assistant", "content": [_text(text)]})
                        if tag and (item.interrupted or tag in self._cut_short):
                            tool = " The tool you were about to call did not run." if self._cut_short.get(tag) else ""
                            notes.append("Note from the app: your reply was cut off before you finished it. "
                                         f"The caller heard only your words above.{tool}")
                    elif text:
                        notes.append(f'Note from the app: the app said this to the caller: "{text}"')
            elif kind == "function_call_output":
                answerable = not draft.messages and item.call_id in open_tools
                if item.id.endswith(RUNNING_OUTPUT_SUFFIX):
                    if answerable and item.call_id not in results:
                        results[item.call_id] = _tool_result(item.call_id, STILL_RUNNING)
                        stand_ins.add(item.call_id)
                elif answerable and (item.call_id not in results or item.call_id in stand_ins):
                    results[item.call_id] = _tool_result(item.call_id, item.output or "", bool(item.is_error))
                    stand_ins.discard(item.call_id)
                    draft.results.add(item.call_id)
                else:
                    notes.append(f"Note from the app: the {item.name} tool finished after the conversation "
                                 f"moved on. Result: {item.output or ''}")
            # function_call items mirror tool_use blocks already in history; other kinds carry nothing.

        for tag in self._unsettled:
            reply = self._replies[tag]
            if tag not in draft.settled and reply.speech is not None and reply.speech.done() \
                    and not _spoken(reply.speech, tag):
                draft.settled.add(tag)
                notes.append(f'Note from the app: the caller did not hear your reply that began '
                             f'"{_opening(reply.said)}". They started talking before it played.')

        close()
        return draft

    def build(self, chat_ctx: llm.ChatContext, tools_schema: list[dict[str, Any]],
              tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN) -> tuple[dict[str, Any], Draft]:
        self._settle_pending()
        draft = self.draft(chat_ctx)
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": self.history + draft.messages,
            "thinking": {"type": self._thinking},
            "output_config": {"effort": self._effort},
        }
        if self.system_prompt:
            params["system"] = [{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}]
        if self._auto_cache:
            params["cache_control"] = {"type": "ephemeral"}
        if self._tools_schema is None and tools_schema:
            self._tools_schema = tools_schema
        elif tools_schema and tools_schema != self._tools_schema:
            log.warning("the tool list changed mid-call; sending the first one so earlier thinking stays valid")
        if self._tools_schema:
            params["tools"] = self._tools_schema
            if tool_choice == "none" or not tools_schema:
                params["tool_choice"] = {"type": "none"}
        problems = check_request(params["messages"])
        if problems:
            log.error("request breaks the Messages API rules: %s", problems)
            self._emit("claude_request_problem", problems=problems)
        return params, draft

    def record(self, turn_id: str, content: list[dict[str, Any]], draft: Draft, speech: Any | None = None,
               stop_reason: str | None = "end_turn") -> Reply | None:
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        tools = {b["id"]: b["name"] for b in content if b.get("type") == "tool_use"}
        if not text.strip() and not tools:
            # Nothing to say or do: leave history as it was, so the next request starts from the same place.
            self._emit("claude_reply_empty", turn=turn_id)
            return None
        reply = Reply(turn_id, content, text, tools, speech, draft, tools_sent=stop_reason == "tool_use")
        if speech is None or speech.scheduled:
            self._commit(reply)
        else:
            self._pending = reply
        return reply

    def decline(self, turn_id: str, draft: Draft, speech: Any | None = None) -> None:
        """Claude declined this turn (stop_reason "refusal"). History stays as it was, and what came in with the
        declined request (the caller's words, tool results, the app's notes) is never sent again: sent again, it
        would most likely be declined again, turn after turn. Like a reply, this takes effect once LiveKit plays
        the decline's line; a preemptive request LiveKit throws away leaves nothing out."""
        if speech is None or speech.scheduled:
            self._apply_decline(turn_id, draft)
        else:
            self._pending_decline = (turn_id, draft, speech)

    def _apply_decline(self, turn_id: str, draft: Draft) -> None:
        self._declined |= draft.item_ids
        self._declined_results |= draft.results
        self._emit("claude_reply_declined", turn=turn_id, words_left_out=len(draft.words),
                   items_left_out=len(draft.item_ids))

    def settle(self) -> None:
        """Commit or drop the reply still waiting on LiveKit, for example when the call ends."""
        self._settle_pending()

    def _settle_pending(self) -> None:
        declined, self._pending_decline = self._pending_decline, None
        if declined is not None:
            turn_id, draft, speech = declined
            if speech.scheduled:
                self._apply_decline(turn_id, draft)
            else:
                self._emit("claude_decline_dropped", turn=turn_id)  # thrown away: nobody heard the line
        reply, self._pending = self._pending, None
        if reply is None:
            return
        if reply.speech is None or reply.speech.scheduled:
            self._commit(reply)
        else:
            # LiveKit threw it away (a preemptive reply to words the caller kept adding to).
            # Nobody heard it and no other request used it.
            self._emit("claude_reply_dropped", turn=reply.id)

    def _commit(self, reply: Reply) -> None:
        self.history.extend(reply.draft.messages)
        self.history.append({"role": "assistant", "content": reply.content})
        self._committed |= reply.draft.item_ids
        self._unsettled = [t for t in self._unsettled if t not in reply.draft.settled]
        if reply.said.strip():  # words the caller could miss; a reply of only a tag has none
            self._unsettled.append(reply.id)
        self._replies[reply.id] = reply
        self._last = reply


class ClaudeStream(llm.LLMStream):
    def __init__(self, owner: ClaudeBedrockLLM, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                 conn_options: APIConnectOptions, tool_choice: NotGivenOr[llm.ToolChoice]) -> None:
        super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._tool_choice = tool_choice
        # A retry after words were already sent would speak the start of the reply twice.
        self._retry_on_chunk_sent = False

    async def _run(self) -> None:
        owner: ClaudeBedrockLLM = self._llm  # type: ignore[assignment]
        speech = current_speech()
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        tools_schema = llm.ToolContext(self._tools).parse_function_tools("anthropic") if self._tools else []
        async with owner._lock:
            params, draft = owner.build(self._chat_ctx, tools_schema, self._tool_choice)
            started = time.perf_counter()
            first_text: float | None = None
            tool_started = False
            try:
                async with owner._client.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "text" and event.text:
                            if first_text is None:
                                first_text = time.perf_counter() - started
                            # The tag rides into the assistant item LiveKit adds once the reply is spoken.
                            self._event_ch.send_nowait(llm.ChatChunk(id=turn_id, delta=llm.ChoiceDelta(
                                role="assistant", content=event.text, extra={TURN_TAG: turn_id})))
                        elif event.type == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                            tool_started = True
                    final = await stream.get_final_message()
            except BaseException as exc:
                if first_text is not None:
                    # Words went out but the reply was never finished (cut off, or an error): see draft().
                    owner._cut_short[turn_id] = tool_started
                if isinstance(exc, anthropic.APIStatusError):
                    body = exc.body if isinstance(exc.body, dict) else {}
                    error_type = str((body.get("error") or {}).get("type", ""))
                    retryable = exc.status_code == 429 or exc.status_code >= 500 or error_type in TRANSIENT_STREAM_ERRORS
                    raise APIStatusError(str(exc), status_code=exc.status_code, retryable=retryable) from exc
                if isinstance(exc, anthropic.APIConnectionError):
                    raise APIConnectionError(str(exc)) from exc
                raise
            content = [b.model_dump(exclude_none=True) for b in final.content]
            refused = final.stop_reason == "refusal"
            if refused:
                reply = None
                owner.decline(turn_id, draft, speech)
            else:
                reply = owner.record(turn_id, content, draft, speech, final.stop_reason)

        usage = getattr(final, "usage", None)
        cache_read = (getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
        owner._emit("claude_turn", turn=turn_id, stop=final.stop_reason, first_text_s=first_text,
                    input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
                    cache_read_tokens=cache_read, status="refused" if refused else "empty" if reply is None
                    else "pending" if owner._pending is reply else "committed")
        if reply is not None and reply.tools:
            if reply.tools_sent:
                calls = [llm.FunctionToolCall(name=b["name"], arguments=json.dumps(b.get("input", {})), call_id=b["id"])
                         for b in content if b.get("type") == "tool_use"]
                self._event_ch.send_nowait(llm.ChatChunk(id=turn_id, delta=llm.ChoiceDelta(
                    role="assistant", tool_calls=calls, extra={TURN_TAG: turn_id})))
            else:
                # Cut short (for example max_tokens): the tool input may be incomplete, so it is not run.
                # The next request gives it a stand-in result.
                log.warning("tool use not run: stop_reason=%s", final.stop_reason)
        if usage is not None:
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            prompt = (usage.input_tokens or 0) + cache_read + cache_write
            self._event_ch.send_nowait(llm.ChatChunk(id=turn_id, usage=llm.CompletionUsage(
                completion_tokens=usage.output_tokens or 0, prompt_tokens=prompt, prompt_cached_tokens=cache_read,
                cache_creation_tokens=cache_write, total_tokens=prompt + (usage.output_tokens or 0))))
        if refused:
            # Claude declined the turn (a safety classifier, or Claude itself; Claude Haiku 5.5 has no server-side
            # fallback). The caller hears a short line, as the app's words: the next request tells Claude so.
            details = getattr(final, "stop_details", None)
            owner._emit("claude_refusal", turn=turn_id, category=getattr(details, "category", None),
                        words_out=first_text is not None)
            log.warning("Claude declined this turn (stop_reason=refusal)")
            self._event_ch.send_nowait(llm.ChatChunk(id=turn_id, delta=llm.ChoiceDelta(
                role="assistant", content=(" " if first_text is not None else "") + owner._refusal_line,
                extra={REFUSAL_TAG: turn_id})))
