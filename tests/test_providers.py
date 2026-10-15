"""Model providers (voice/providers/): one small adapter each behind one interface, chosen by the model catalog
(voice/model_catalog.py). A new provider is one adapter plus catalog entries; the first test proves it with a
provider these tests make up."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from voice import model_catalog, providers
from voice.model_catalog import Choice, Offering, Role
from voice.providers import ToolCall, ToolResult, ToolSpec, Turn, Usage

TOOLS = [ToolSpec("search_bee", "Search Bee.", {"type": "object", "properties": {"query": {"type": "string"}},
                                                "required": ["query"]})]


class MadeUp:
    """A provider no code knows: it answers each request with the next scripted turn."""

    def __init__(self, *turns: Turn | Exception) -> None:
        self.turns, self.requests = list(turns), []

    async def reply(self, choice, system, turns, tools, *, on_text=None, usage=None):
        self.requests.append((choice.offering.model, system, list(turns), [t.name for t in tools]))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if on_text and turn.text:
            on_text(turn.text)
        used = Usage(input_tokens=100, output_tokens=10)
        if usage is not None:
            usage.add(used)
        return providers.Reply(turn, "tool_use" if turn.calls else "end", used, choice.offering.model,
                               choice.offering.provider)


@pytest.fixture
def made_up_provider(monkeypatch):
    """A provider named "madeup" for the meta role, with GLM 5 (Bedrock) standing by: one adapter registered and
    catalog entries, nothing else."""
    adapter = MadeUp()
    providers.register("madeup", adapter)
    offering = Offering("madeup", "made-up-large", 0.5 / 1e6, 1.5 / 1e6)
    meta = Role("meta", standby=model_catalog.GLM_STANDBY, choices=(
        Choice("large", "Made Up Large", "Made Up", {"ko": "판단", "en": "Decides"}, offering, default=True),))
    monkeypatch.setitem(model_catalog.ROLES, "meta", meta)
    monkeypatch.setattr(model_catalog, "OFFERINGS", model_catalog.OFFERINGS + (offering,))
    yield adapter
    providers.unregister("madeup")


def test_a_new_provider_is_one_adapter_and_catalog_entries(made_up_provider):
    from voice import costs

    made_up_provider.turns = [Turn("assistant", calls=[ToolCall("c1", "search_bee", {"query": "hike"})]),
                              Turn("assistant", text="Nothing to do.")]
    usage = Usage()

    async def main():
        first = await providers.answer("meta", "Be brief.", [Turn("user", text="Anything new?")], TOOLS, usage=usage)
        turns = [Turn("user", text="Anything new?"), first.turn,
                 Turn("user", results=[ToolResult("c1", "No matches.")])]
        return first, await providers.answer("meta", "Be brief.", turns, TOOLS, usage=usage)

    first, second = asyncio.run(main())
    assert (first.stop, first.turn.calls[0].input, second.turn.text) == ("tool_use", {"query": "hike"}, "Nothing to do.")
    assert [r[0] for r in made_up_provider.requests] == ["made-up-large", "made-up-large"]
    assert (usage.input_tokens, usage.output_tokens) == (200, 20)
    assert costs.llm_cost(second.model, 1_000_000, 1_000_000) == pytest.approx(2.0)  # its price from the catalog


def test_the_standby_answers_when_the_choice_s_request_fails(made_up_provider, monkeypatch):
    standby = MadeUp(Turn("assistant", text="From the standby."))
    providers.register("bedrock", standby)
    made_up_provider.turns = [RuntimeError("overloaded")]
    reply = asyncio.run(providers.answer("meta", "s", [Turn("user", text="hi")]))
    assert (reply.turn.text, reply.model, reply.instead_of) == ("From the standby.", "zai.glm-5", "large")
    assert "overloaded" in reply.reason
    providers.unregister("bedrock")


def test_a_choice_that_already_streamed_text_is_not_handed_to_the_standby(made_up_provider):
    class HalfWay(MadeUp):
        async def reply(self, choice, system, turns, tools, *, on_text=None, usage=None):
            on_text("Hello, ")
            raise RuntimeError("connection reset")

    providers.register("madeup", HalfWay())
    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(providers.answer("meta", "s", [Turn("user", text="hi")], on_text=lambda text: None))


# ---------- Anthropic's Messages API ----------

class Stream:
    def __init__(self, message: dict, deltas: list[str]) -> None:
        self.message, self.deltas = message, deltas

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def events():
            for delta in self.deltas:
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=delta))
        return events()

    async def get_final_message(self):
        message = json.loads(json.dumps(self.message))
        blocks = [SimpleNamespace(**b, model_dump=lambda b=b, **_: dict(b)) for b in message["content"]]
        usage = SimpleNamespace(**message["usage"])
        return SimpleNamespace(content=blocks, stop_reason=message["stop_reason"], usage=usage, model="m")


class Messages:
    def __init__(self, *messages) -> None:
        self.messages, self.sent = list(messages), []

    def stream(self, **params):
        self.sent.append(json.loads(json.dumps(params)))
        message, deltas = self.messages.pop(0)
        return Stream(message, deltas)


def test_the_anthropic_adapter_speaks_the_messages_api():
    from voice.providers.anthropic import Anthropic

    tool_turn = {"content": [{"type": "thinking", "thinking": "hm", "signature": "sig"},
                             {"type": "text", "text": "Looking."},
                             {"type": "tool_use", "id": "toolu_1", "name": "search_bee", "input": {"query": "hike"}}],
                 "stop_reason": "tool_use",
                 "usage": {"input_tokens": 900, "output_tokens": 40, "cache_read_input_tokens": 600,
                           "cache_creation_input_tokens": 100}}
    end_turn = {"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 5}}
    messages = Messages((tool_turn, ["Look", "ing."]), (end_turn, ["Done."]))
    adapter = Anthropic(client=SimpleNamespace(messages=messages))
    sonnet = model_catalog.choice("meta")
    seen, usage = [], Usage()

    async def main():
        first = await adapter.reply(sonnet, "Be brief.", [Turn("user", text="Anything?")], TOOLS,
                                    on_text=seen.append, usage=usage)
        turns = [Turn("user", text="Anything?"), first.turn, Turn("user", results=[ToolResult("toolu_1", "none",
                                                                                               error=True)])]
        return first, await adapter.reply(sonnet, "Be brief.", turns, TOOLS, usage=usage)

    first, second = asyncio.run(main())
    request = messages.sent[0]
    assert (request["model"], request["max_tokens"], request["thinking"], request["output_config"]) == (
        "claude-sonnet-5-5", 8000, {"type": "adaptive"}, {"effort": "medium"})
    assert request["system"] == "Be brief." and request["tools"][0]["input_schema"]["required"] == ["query"]
    assert seen == ["Look", "Looking."] and first.turn.text == "Looking."
    assert first.turn.calls == [ToolCall("toolu_1", "search_bee", {"query": "hike"})] and first.stop == "tool_use"
    replayed = messages.sent[1]["messages"]
    assert replayed[1]["content"][0] == {"type": "thinking", "thinking": "hm", "signature": "sig"}  # unchanged
    assert replayed[2]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "none",
                                       "is_error": True}]
    # Every prompt token counts, cache reads and writes among them, as the budget prices them (voice/costs.py).
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read, usage.cache_write) == (2600, 45, 600, 100)
    assert second.stop == "end" and second.model == "claude-sonnet-5-5"


# ---------- Bedrock's Converse API ----------

def test_the_bedrock_adapter_speaks_converse_and_streams_text():
    from tests.test_prep import Converse, text, tool
    from voice.providers.bedrock import Bedrock

    client = Converse([{"text": "Hi."}, tool(0, "search_bee", {"query": "hike"})], text("Done."), usage=(300, 20))
    adapter = Bedrock(client=client)
    glm = model_catalog.role("prep").standby
    seen, usage = [], Usage()

    async def main():
        first = await adapter.reply(glm, "Be brief.", [Turn("user", text="Go")], TOOLS, on_text=seen.append,
                                    usage=usage)
        turns = [Turn("user", text="Go"), first.turn, Turn("user", results=[ToolResult(first.turn.calls[0].id, "x")])]
        return first, await adapter.reply(glm, "Be brief.", turns, TOOLS, usage=usage)

    first, second = asyncio.run(main())
    request = client.requests[0]
    assert (request["modelId"], request["inferenceConfig"]) == ("zai.glm-5", {"maxTokens": 500, "temperature": 0.3})
    assert request["toolConfig"]["tools"][0]["toolSpec"]["name"] == "search_bee"
    assert first.turn.text == "Hi." and first.turn.calls[0].input == {"query": "hike"} and seen[-1] == "Hi."
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["toolResult"]["content"] == [{"text": "x"}] and second.turn.text == "Done."
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (600, 40, False)


def test_a_converse_request_cut_short_counts_an_estimate():
    from tests.test_prep import Converse, text
    from voice.providers.bedrock import Bedrock

    client = Converse(text("A long answer " * 40), gap=0.02)
    usage = Usage()

    async def main():
        task = asyncio.create_task(Bedrock(client=client).reply(model_catalog.role("prep").standby, "s",
                                                               [Turn("user", text="Go")], [], usage=usage,
                                                               on_text=lambda t: None))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert usage.estimated and usage.input_tokens > 0 and usage.output_tokens > 0
