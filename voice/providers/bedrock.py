"""Amazon Bedrock's Converse API, for the model catalog's GLM 5 choices (the AWS session of voice/aws_session.py;
BEDROCK_REGION, else AWS_REGION, else Sydney).

reply() streams one Converse request, reading boto3's blocking stream in a thread: the choice's model, maxTokens
and temperature (extra), the conversation as Converse messages, client tools as Converse tool specs. A request cut
short after the model began its answer is counted as an estimate.
"""

import asyncio
import functools
import json
import os
import threading
from typing import Any, Callable

from botocore.config import Config

from voice.aws_session import aws_session
from voice.model_catalog import Choice
from voice.providers import Reply, ToolCall, ToolSpec, Turn, Usage, estimated_tokens

PROVIDER = "bedrock"
STOPS = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "max_tokens", "stop_sequence": "end"}


@functools.lru_cache(maxsize=8)
def bedrock_client(region: str, read_timeout: int, retries: int):
    return aws_session().client("bedrock-runtime", region_name=region,
                                config=Config(connect_timeout=3, read_timeout=read_timeout,
                                              retries={"max_attempts": retries}))


def region_of(choice: Choice) -> str:
    return choice.extra.get("region") or os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "ap-southeast-2"))


class Bedrock:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def setup_problem(self) -> str | None:
        return None  # the AWS session finds its own credentials, as every AWS service here does

    def ready(self) -> bool:
        return True

    def client(self, choice: Choice) -> Any:
        if self._client is not None:
            return self._client
        return bedrock_client(region_of(choice), int(choice.extra.get("read_timeout", 60)),
                              int(choice.extra.get("retries", 1)))

    def call_llm(self, choice: Choice, *, on_event: Callable[..., None] | None = None,
                 refusal_line: str | None = None) -> Any:
        """The call AI on a Bedrock model: LiveKit's own Converse plugin."""
        from livekit.plugins import aws

        return aws.LLM(model=choice.offering.model, region=region_of(choice))

    async def reply(self, choice: Choice, system: str, turns: list[Turn], tools: list[ToolSpec], *,
                    on_text: Callable[[str], None] | None = None, usage: Usage | None = None) -> Reply:
        messages = [message(turn) for turn in turns]
        inference: dict[str, Any] = {"maxTokens": choice.max_tokens}
        if "temperature" in choice.extra:
            inference["temperature"] = choice.extra["temperature"]
        request: dict[str, Any] = {"modelId": choice.offering.model, "system": [{"text": system}],
                                   "messages": messages, "inferenceConfig": inference}
        if tools:
            request["toolConfig"] = {"tools": [{"toolSpec": {"name": t.name, "description": t.description,
                                                             "inputSchema": {"json": t.parameters}}} for t in tools]}
        if choice.extra.get("fields"):
            request["additionalModelRequestFields"] = choice.extra["fields"]
        client = self.client(choice)
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def pump() -> None:  # boto3 streams are blocking, so they are read in a thread
            try:
                for event in client.converse_stream(**request)["stream"]:
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(events.put_nowait, event)
                loop.call_soon_threadsafe(events.put_nowait, None)
            except Exception as exc:
                if not stop.is_set():
                    loop.call_soon_threadsafe(events.put_nowait, exc)

        threading.Thread(target=pump, daemon=True, name="converse-stream").start()
        text, calls, tool, used, stopped = "", [], None, Usage(), "end"
        answering = counted = False  # the model began its answer (the request was read); Bedrock sent its count
        try:
            while (event := await events.get()) is not None:
                if isinstance(event, Exception):
                    raise event
                answering = True
                if "contentBlockStart" in event and "toolUse" in event["contentBlockStart"].get("start", {}):
                    tool = {**event["contentBlockStart"]["start"]["toolUse"], "input": ""}
                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    if "text" in delta:
                        text += delta["text"]
                        if on_text:
                            on_text(text)
                    elif "toolUse" in delta and tool is not None:
                        tool["input"] += delta["toolUse"].get("input", "")
                elif "contentBlockStop" in event and tool is not None:
                    try:
                        found = json.loads(tool["input"] or "{}")
                    except ValueError:
                        found = {}
                    calls.append(ToolCall(tool["toolUseId"], tool["name"], found if isinstance(found, dict) else {}))
                    tool = None
                elif "messageStop" in event:
                    stopped = STOPS.get(event["messageStop"].get("stopReason"), event["messageStop"].get("stopReason"))
                elif "metadata" in event:
                    counted_now = event["metadata"].get("usage") or {}
                    used = Usage(input_tokens=int(counted_now.get("inputTokens") or 0),
                                 output_tokens=int(counted_now.get("outputTokens") or 0))
                    counted = True
        finally:
            stop.set()  # a request given up on (a deadline) stops being read
            if answering and not counted:
                used = Usage(input_tokens=estimated_tokens(system, messages), output_tokens=estimated_tokens(text),
                             estimated=True)
            if usage is not None and (counted or answering):
                usage.add(used)
        native = ([{"text": text}] if text.strip() else []) + [
            {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.input}} for c in calls]
        return Reply(Turn("assistant", text, calls, native=native, native_provider=PROVIDER), stopped, used,
                     choice.offering.model, PROVIDER)


def message(turn: Turn) -> dict[str, Any]:
    """A Turn as a Converse message (Bedrock refuses a blank text block)."""
    if turn.role == "assistant":
        if turn.native is not None and turn.native_provider == PROVIDER:
            return {"role": "assistant", "content": turn.native}
        content: list[dict[str, Any]] = [{"text": turn.text}] if turn.text.strip() else []
        content += [{"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.input}} for c in turn.calls]
        return {"role": "assistant", "content": content}
    content = [{"toolResult": {"toolUseId": r.id, "content": [{"text": r.text}], **({"status": "error"} if r.error
                                                                                      else {})}}
               for r in turn.results]
    if turn.text.strip():
        content.append({"text": turn.text})
    return {"role": "user", "content": content}


ADAPTER = Bedrock()
