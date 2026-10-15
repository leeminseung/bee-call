"""Shared test helpers."""

import dataclasses
import functools
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

from voice.people import People

SEOUL = ZoneInfo("Asia/Seoul")  # the default time zone of a person (voice/people.py)


def seoul_now() -> datetime:
    """A person's clock when their time zone is the default (Person.now())."""
    return datetime.now(SEOUL)


def signed_in(root: Path, person: str = "matt") -> SimpleNamespace:
    """An approved person with a sign-in token, in a data root under `root`."""
    people = People(root / "data")
    people.add(person)
    token = people.new_session(person)
    return SimpleNamespace(people=people, id=person, person=people.person(person), token=token,
                           workspace=people.workspace(person), headers={"Authorization": f"Bearer {token}"})


def fake_livekit(requests: list) -> SimpleNamespace:
    """LiveKit's room API, keeping each CreateRoomRequest it gets."""
    async def create_room(request):
        requests.append(request)

    return SimpleNamespace(room=SimpleNamespace(create_room=create_room))


def placing(requests: list):
    """The real place_call (voice/dispatch.py), with LiveKit faked: for call_due's `place`."""
    from voice.dispatch import place_call
    return functools.partial(place_call, lk=fake_livekit(requests))


def another_conversation(plan, n: int = 1):
    """The same plan about another Bee conversation: one conversation leads to at most one offer (BC-014), so a
    test that asks again asks about another one."""
    return dataclasses.replace(plan, conversation_ids=[1000 + n])


def next_question(store, plan, n: int, now):
    """Another question later the same day, about another conversation. One question at a time: the user has
    said no to the one still waiting."""
    for waiting in [o for o in store.open(now) if o.state == "asked"]:
        store.answer(waiting.id, "decline", now)
    return store.ask(another_conversation(plan, n), now)


class ScriptedLLM(llm.LLM):
    """A model that streams scripted replies as plain text, the way LiveKit's Converse plugin does for GLM 5 on
    Bedrock (no tags): each reply is a list of text pieces. It keeps the chat contexts it was given."""

    def __init__(self, replies: list[list[str]], model: str = "zai.glm-5", provider: str = "aws-bedrock") -> None:
        super().__init__()
        self.replies = list(replies)
        self.requests: list[llm.ChatContext] = []
        self._model, self._provider = model, provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool] | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN, tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
             extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN) -> llm.LLMStream:
        self.requests.append(chat_ctx.copy())
        return _ScriptedStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _ScriptedStream(llm.LLMStream):
    async def _run(self) -> None:
        chunk_id = uuid.uuid4().hex
        model: ScriptedLLM = self._llm  # type: ignore[assignment]
        for piece in model.replies.pop(0) if model.replies else ["Okay."]:
            self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant",
                                                                                         content=piece)))
        self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, usage=llm.CompletionUsage(
            completion_tokens=5, prompt_tokens=50, total_tokens=55)))


def converse(monkeypatch, client) -> None:
    """Bedrock's Converse API answers from `client` (a stand-in such as tests/test_prep.py Converse), for whatever
    runs on Bedrock: the bedrock development mode's prep, and GLM 5 standing by."""
    from voice import providers
    from voice.providers.bedrock import Bedrock

    monkeypatch.setitem(providers._ADAPTERS, "bedrock", Bedrock(client=client))
