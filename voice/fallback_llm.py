"""The call AI's model with GLM 5 on Bedrock standing by for the first request (COORDINATOR_LLM=anthropic).

Claude Haiku 5.5 on Anthropic's own API is a second provider next to AWS, with ways to fail that GLM 5 on Bedrock
does not share: a missing or refused key (401), no credit left (400 "credit balance is too low"), a rate limit (429),
an outage (5xx, 529), no route to api.anthropic.com. If the first request of a call fails in any way, or Claude
declines it before saying a word, GLM 5 answers that request and every later one, and the record says which model
answered and why (on_choice; voice/agent.py writes the "coordinator_model" event). The failed try is no failed turn:
its error never reaches the session.

Once a model has answered, it is the call's only model and requests go straight to it: a later failure is a failed
turn, as with any model (LiveKit retries what is worth retrying, then gives the turn up). The call never switches
models mid-conversation, which the Claude adapter's own history could not follow.
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterable
from typing import Any, Callable, ClassVar

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

from voice.claude_llm import REFUSAL_TAG

log = logging.getLogger("bee-call.model")


class FirstRequestFallback(llm.LLM):
    def __init__(self, primary: llm.LLM, make_fallback: Callable[[], llm.LLM],
                 on_choice: Callable[[llm.LLM, str | None], None]) -> None:
        super().__init__()
        self.primary = primary
        self._make_fallback = make_fallback  # built only when needed
        self._on_choice = on_choice  # (the model that answered, why the primary did not, or None)
        self.chosen: llm.LLM | None = None  # the call's model, once the first answer has shown which
        self._deciding = asyncio.Lock()
        self._listen(primary)

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def provider(self) -> str:
        return self.active.provider

    @property
    def active(self) -> llm.LLM:
        """The model answering: the chosen one, or the primary until the first answer."""
        return self.chosen or self.primary

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool] | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN, tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
             extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN) -> llm.LLMStream:
        if self.chosen is not None:  # straight to the call's model, with its own retries and errors
            return self.chosen.chat(chat_ctx=chat_ctx, tools=tools, conn_options=conn_options,
                                    parallel_tool_calls=parallel_tool_calls, tool_choice=tool_choice,
                                    extra_kwargs=extra_kwargs)
        return FirstRequest(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                            parallel_tool_calls=parallel_tool_calls, tool_choice=tool_choice,
                            extra_kwargs=extra_kwargs)

    async def aclose(self) -> None:
        for model in {self.primary, self.chosen} - {None}:
            await model.aclose()

    def _listen(self, model: llm.LLM) -> None:
        # The session listens to this model: what the models inside it measure (usage, for the daily budget) is
        # passed on, and so are errors of the call's model. The primary's failure on the first request is handled
        # here, so it is no failed turn.
        model.on("metrics_collected", lambda *args, **kwargs: self.emit("metrics_collected", *args, **kwargs))
        model.on("error", lambda *args, **kwargs: self.emit("error", *args, **kwargs)
                 if self.chosen is model else None)

    def _choose(self, model: llm.LLM, reason: str | None = None) -> None:
        self.chosen = model
        try:
            self._on_choice(model, reason)
        except Exception:  # the record is never worth a call
            log.exception("could not record the call's model")

    def _fall_back(self, reason: str) -> llm.LLM:
        log.warning("%s failed on the first request, %s takes the call: %s", self.primary.model, "the stand-by",
                    reason)
        fallback = self._make_fallback()
        self._listen(fallback)
        self._choose(fallback, reason)
        return fallback


class FirstRequest(llm.LLMStream):
    """A request made before the call's model is known: the primary is tried once; the first word or tool call it
    sends makes it the call's model. If it fails first, the stand-by answers."""

    # The models inside measure and report themselves; this stream only relays their chunks.
    _genai_operation_name: ClassVar[str | None] = None

    def __init__(self, owner: FirstRequestFallback, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                 conn_options: APIConnectOptions, parallel_tool_calls: NotGivenOr[bool],
                 tool_choice: NotGivenOr[llm.ToolChoice], extra_kwargs: NotGivenOr[dict[str, Any]]) -> None:
        # This stream itself never retries: the stand-by retries on its own, with the session's options.
        super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=dataclasses.replace(conn_options,
                                                                                                    max_retry=0))
        self._options = conn_options
        self._request = {"parallel_tool_calls": parallel_tool_calls, "tool_choice": tool_choice,
                         "extra_kwargs": extra_kwargs}

    async def _run(self) -> None:
        owner: FirstRequestFallback = self._llm  # type: ignore[assignment]
        async with owner._deciding:
            if owner.chosen is None:
                reason = await self._try(owner.primary)
                if reason is None:
                    return
                owner._fall_back(reason)
        await self._relay(owner.chosen, self._options)  # type: ignore[arg-type]

    async def _try(self, primary: llm.LLM) -> str | None:
        """Relays the primary's answer: None once it answered, else why it did not."""
        owner: FirstRequestFallback = self._llm  # type: ignore[assignment]
        try:
            async with primary.chat(chat_ctx=self._chat_ctx, tools=self._tools,
                                    conn_options=dataclasses.replace(self._options, max_retry=0),
                                    **self._request) as stream:
                async for chunk in stream:
                    if owner.chosen is None:
                        if chunk.delta and (chunk.delta.extra or {}).get(REFUSAL_TAG):
                            return "Claude declined the first request before saying anything (see claude_refusal)"
                        if chunk.has_response():
                            owner._choose(primary)
                    self._event_ch.send_nowait(chunk)
        except Exception as exc:
            if owner.chosen is primary:
                raise  # it had answered: a failed turn, like any other
            return f"{type(exc).__name__}: {exc}"[:500]
        if owner.chosen is None:
            owner._choose(primary)  # it answered with nothing to say, which is still an answer
        return None

    async def _relay(self, model: llm.LLM, options: APIConnectOptions) -> None:
        async with model.chat(chat_ctx=self._chat_ctx, tools=self._tools, conn_options=options,
                              **self._request) as stream:
            async for chunk in stream:
                self._event_ch.send_nowait(chunk)

    def _emit_error(self, api_error: Exception, recoverable: bool) -> None:
        # Every error raised here was already reported by the model it came from (and passed on by the owner when
        # it is the call's model); reporting it again would count one failed turn twice.
        self._current_attempt_has_error = True

    async def _metrics_monitor_task(self, event_aiter: AsyncIterable[llm.ChatChunk]) -> None:
        async for _ in event_aiter:  # the models inside report their own metrics
            pass

