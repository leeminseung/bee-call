"""The call AI on Claude Haiku 5.5 through Anthropic's own API, with GLM 5 on Bedrock standing by for the first
request (COORDINATOR_LLM=anthropic). No network: a fake Anthropic client and a scripted GLM 5."""

import asyncio
import json

import anthropic
import httpx2
import pytest
from livekit.agents import APIError, llm
from livekit.agents.types import APIConnectOptions

from tests.helpers import ScriptedLLM, seoul_now
from voice.call_models import CALL_MODELS
from voice.claude_llm import INSTRUCTIONS_ID, ClaudeBedrockLLM
from voice.fake_claude import FakeAnthropic
from voice.fallback_llm import FirstRequestFallback

GLM_LINE = "GLM 5 here."
QUICK = APIConnectOptions(max_retry=0, retry_interval=0.0, timeout=5)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def say(text):
    return [{"type": "text", "text": text}]


def api_error(cls, status, kind, message):
    body = {"type": "error", "error": {"type": kind, "message": message}}
    return cls(message, response=httpx2.Response(status, request=REQUEST), body=body)


FIRST_REQUEST_FAILURES = {
    "no network": lambda: anthropic.APIConnectionError(request=REQUEST),
    "a refused key": lambda: api_error(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key"),
    "a rate limit": lambda: api_error(anthropic.RateLimitError, 429, "rate_limit_error", "Rate limited"),
    "no credit": lambda: api_error(anthropic.BadRequestError, 400, "invalid_request_error",
                                   "Your credit balance is too low to access the Anthropic API."),
    "an outage": lambda: api_error(anthropic.InternalServerError, 500, "api_error", "Internal server error"),
    "overloaded": lambda: api_error(anthropic.OverloadedError, 529, "overloaded_error", "Overloaded"),
}


def standing_by(client):
    """Claude Haiku 5.5 (a fake client) first, GLM 5 standing by: the model, the choices it reported, and whether
    GLM 5 was built."""
    choices, built = [], []
    claude = ClaudeBedrockLLM(client=client, model="claude-haiku-5-5", thinking="disabled", effort="low",
                              auto_cache=True)
    glm = ScriptedLLM([[GLM_LINE]] * 3)

    def make_glm():
        built.append(glm)
        return glm

    model = FirstRequestFallback(claude, make_glm, on_choice=lambda chosen, reason: choices.append((chosen.model, reason)))
    return model, choices, built


def new_ctx():
    ctx = llm.ChatContext()
    ctx.add_message(role="system", content="You are Bee Call.", id=INSTRUCTIONS_ID)
    return ctx


async def turn(model, words, ctx):
    ctx.add_message(role="user", content=words)
    chunks = []
    async with model.chat(chat_ctx=ctx, conn_options=QUICK) as stream:
        async for chunk in stream:
            chunks.append(chunk)
    text = "".join(c.delta.content or "" for c in chunks if c.delta).strip()
    ctx.add_message(role="assistant", content=text)
    return text


def test_the_first_answer_decides_the_call_s_model():
    async def main():
        client = FakeAnthropic([say("Hi, Claude here."), say("Still Claude.")])
        model, choices, built = standing_by(client)
        metrics = []
        model.on("metrics_collected", metrics.append)
        ctx = new_ctx()
        said = [await turn(model, "Hello?", ctx), await turn(model, "And now?", ctx)]
        return said, choices, built, client, metrics

    said, choices, built, client, metrics = asyncio.run(main())
    assert said == ["Hi, Claude here.", "Still Claude."] and built == []
    assert choices == [("claude-haiku-5-5", None)]  # recorded once, when Claude first answered
    assert [m.metadata.model_name for m in metrics] == ["claude-haiku-5-5"] * 2  # the daily budget prices Haiku
    first = client.calls[0]
    assert first["model"] == "claude-haiku-5-5" and first["thinking"] == {"type": "disabled"}
    assert first["output_config"] == {"effort": "low"} and first["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("failure", list(FIRST_REQUEST_FAILURES))
def test_a_first_request_that_fails_is_answered_by_glm_and_so_is_the_rest_of_the_call(failure):
    async def main():
        client = FakeAnthropic([FIRST_REQUEST_FAILURES[failure](), say("Claude again.")])
        model, choices, built = standing_by(client)
        errors, metrics = [], []
        model.on("error", errors.append)
        model.on("metrics_collected", metrics.append)
        ctx = new_ctx()
        said = [await turn(model, "안녕하세요", ctx), await turn(model, "내일 계획 세워요", ctx)]
        return said, choices, built, client, errors, metrics, model

    said, choices, built, client, errors, metrics, model = asyncio.run(main())
    assert said == [GLM_LINE, GLM_LINE] and len(client.calls) == 1  # Claude is not asked again in this call
    (chosen, reason), = choices
    assert chosen == "zai.glm-5" and reason  # the record says which model answered and why
    assert model.model == "zai.glm-5" and errors == []  # handled: no failed turn reaches the session
    assert [m.metadata.model_name for m in metrics] == ["zai.glm-5"] * 2


def test_a_first_request_claude_declines_is_said_as_the_line_and_the_call_stays_with_claude():
    """A decline is a safety decision, not an outage: it is never handed to another model to answer (Claude Haiku
    5.5 has no fallback model for its declines). The caller hears the one short line, as with any decline."""
    async def main():
        client = FakeAnthropic([[{"type": "refusal"}], say("Sure, what else?")])
        model, choices, built = standing_by(client)
        ctx = new_ctx()
        return [await turn(model, "something it declines", ctx), await turn(model, "Never mind.", ctx)], choices, built

    said, choices, built = asyncio.run(main())
    assert said == ["Sorry, I can't help with that one.", "Sure, what else?"]
    assert choices == [("claude-haiku-5-5", None)] and built == []  # GLM 5 was never asked


def test_after_the_first_answer_a_failure_is_a_failed_turn_never_a_switch():
    """Once Claude has answered, the call stays with it: a later error fails that turn, as with any model."""
    async def main():
        client = FakeAnthropic([say("Hi."), api_error(anthropic.InternalServerError, 500, "api_error", "Internal")])
        model, choices, built = standing_by(client)
        errors = []
        model.on("error", errors.append)
        ctx = new_ctx()
        await turn(model, "Hello?", ctx)
        with pytest.raises(APIError):
            await turn(model, "And now?", ctx)
        return choices, built, errors, model

    choices, built, errors, model = asyncio.run(main())
    assert choices == [("claude-haiku-5-5", None)] and built == [] and model.model == "claude-haiku-5-5"
    assert len(errors) == 1 and not errors[0].recoverable  # the session counts it as it counts any failed turn


# ---------- make_llm: the settings of COORDINATOR_LLM=anthropic ----------

class FakeConverse(ScriptedLLM):
    """Stands in for LiveKit's aws.LLM (GLM 5 on Bedrock): no AWS."""

    def __init__(self, *, model: str, region: str, **_) -> None:
        super().__init__([[GLM_LINE]] * 3, model=model)
        self.region = region


def anthropic_mode(monkeypatch, tmp_path, key: str | None, client=None):
    from voice import agent
    from voice.recorder import CallRecorder

    monkeypatch.setattr(agent, "COORDINATOR_LLM", "anthropic")
    monkeypatch.setattr(agent.aws, "LLM", FakeConverse)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    clients = []

    def fake_client(**kwargs):
        clients.append(kwargs)
        return client

    monkeypatch.setattr(agent, "anthropic_client", fake_client)
    return CallRecorder(tmp_path / "workspace", room="r", now=seoul_now()), clients


def recorded(rec, kind):
    rec.close()
    return [e for e in (json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines())
            if e["type"] == kind]


def test_without_an_anthropic_key_the_call_runs_on_glm_5_and_the_record_says_why(tmp_path, monkeypatch):
    from voice import agent
    from voice.language import call_language

    rec, clients = anthropic_mode(monkeypatch, tmp_path, key=None)
    model = agent.make_llm(rec, call_language("ko"), CALL_MODELS["haiku"])
    assert isinstance(model, FakeConverse) and model.model == "zai.glm-5" and clients == []
    (choice,) = recorded(rec, "coordinator_model")
    assert choice["model"] == "zai.glm-5" and choice["reason"] == "ANTHROPIC_API_KEY is not set"


def test_with_a_key_the_call_ai_is_claude_haiku_5_5_on_anthropic_s_api(tmp_path, monkeypatch):
    from voice import agent
    from voice.language import call_language

    client = FakeAnthropic([[{"type": "refusal"}], say("네, 좋아요.")])
    rec, clients = anthropic_mode(monkeypatch, tmp_path, key="test-key", client=client)
    korean = call_language("ko")

    async def main():
        model = agent.make_llm(rec, korean, CALL_MODELS["haiku"])
        ctx = new_ctx()
        await turn(model, "안녕하세요", ctx)  # Claude answers first: it stays the call's model
        return model, await turn(model, "그건 좀 이상한 질문인데요", ctx)

    client.replies.appendleft(say("안녕하세요!"))
    model, second = asyncio.run(main())
    (kwargs,) = clients
    assert kwargs["api_key"] == "test-key" and kwargs["max_retries"] == 0  # a failed first request falls back at once
    assert model.model == "claude-haiku-5-5" and second == korean.refusal  # a later decline: one line, in Korean
    assert client.calls[0]["thinking"] == {"type": "adaptive"} and client.calls[0]["output_config"] == {"effort": "low"}
    assert recorded(rec, "coordinator_model")[0]["model"] == "claude-haiku-5-5"


def test_a_person_who_picked_sonnet_talks_to_claude_sonnet_5_5_with_glm_5_standing_by(tmp_path, monkeypatch):
    """call_model "sonnet": Claude Sonnet 5.5 on Anthropic's API, thinking between_tools (it refuses "disabled"),
    effort low. Its first request failing sends the call to GLM 5 as with Haiku, and the record says so."""
    from voice import agent
    from voice.language import call_language

    client = FakeAnthropic([say("안녕하세요!")])
    rec, _ = anthropic_mode(monkeypatch, tmp_path, key="test-key", client=client)

    async def main():
        return await turn(agent.make_llm(rec, call_language("ko"), CALL_MODELS["sonnet"]), "안녕하세요", new_ctx())

    assert asyncio.run(main()) == "안녕하세요!"
    sent = client.calls[0]
    assert sent["model"] == "claude-sonnet-5-5" and sent["thinking"] == {"type": "between_tools"}
    assert sent["output_config"] == {"effort": "low"} and sent["cache_control"] == {"type": "ephemeral"}
    assert recorded(rec, "coordinator_model")[0]["model"] == "claude-sonnet-5-5"

    failing = FakeAnthropic([FIRST_REQUEST_FAILURES["no credit"]()])
    rec, _ = anthropic_mode(monkeypatch, tmp_path / "again", key="test-key", client=failing)

    async def again():
        return await turn(agent.make_llm(rec, call_language("ko"), CALL_MODELS["sonnet"]), "안녕하세요", new_ctx())

    assert asyncio.run(again()) == GLM_LINE
    (choice,) = recorded(rec, "coordinator_model")
    assert choice["model"] == "zai.glm-5" and choice["instead_of"] == "claude-sonnet-5-5" and "credit" in choice["reason"]


def test_coordinator_settings_in_the_environment_never_change_the_call_model(tmp_path, monkeypatch):
    """voice/call_models.py is the one place for the call model's settings with COORDINATOR_LLM=anthropic. The
    variables the other modes read are not used here: a call says so in its log and its record, never silently."""
    from voice import agent
    from voice.language import call_language

    client = FakeAnthropic([say("안녕하세요!")])
    rec, _ = anthropic_mode(monkeypatch, tmp_path, key="test-key", client=client)
    for name, value in (("COORDINATOR_THINKING", "disabled"), ("COORDINATOR_EFFORT", "high"),
                        ("COORDINATOR_MODEL", "claude-opus-5-5")):
        monkeypatch.setenv(name, value)

    async def main():
        return await turn(agent.make_llm(rec, call_language("ko"), CALL_MODELS["haiku"]), "안녕하세요", new_ctx())

    asyncio.run(main())
    sent = client.calls[0]
    assert (sent["model"], sent["thinking"], sent["output_config"]) == (
        "claude-haiku-5-5", {"type": "adaptive"}, {"effort": "low"})
    assert agent.ignored_settings() == ["COORDINATOR_EFFORT", "COORDINATOR_MODEL", "COORDINATOR_THINKING"]


def test_with_anthropic_the_prep_runs_on_haiku_and_the_meta_agent_on_sonnet_with_glm_5_standing_by(tmp_path,
                                                                                                   monkeypatch):
    """Decision 10 of the app contract, as the server's mode (anthropic) runs it; COORDINATOR_MODEL names the
    development modes' model and changes nothing here."""
    from meta.agent import Thinker
    from voice.prep import prep_role

    monkeypatch.setenv("COORDINATOR_LLM", "anthropic")
    monkeypatch.setenv("COORDINATOR_MODEL", "claude-opus-5-5")
    prep, meta = prep_role("anthropic"), Thinker(tmp_path, bee=True).role
    assert [(c.offering.provider, c.offering.model) for c in (prep.choices[0], meta.choices[0])] == [
        ("anthropic", "claude-haiku-5-5"), ("anthropic", "claude-sonnet-5-5")]
    assert {(r.standby.offering.provider, r.standby.offering.model) for r in (prep, meta)} == {("bedrock", "zai.glm-5")}


def test_every_call_language_has_a_line_for_a_declined_turn():
    from voice.language import LANGUAGES

    assert all(language.refusal for language in LANGUAGES.values())


def test_every_process_reads_the_same_coordinator_mode_with_the_same_default(tmp_path, monkeypatch):
    """Server review 19: unset, COORDINATOR_LLM meant "echo" to the voice server but "bedrock" to the prep in
    voice/outbound.py and to the meta agent, so a Mac's AI calls were prepared by another model than the one that
    answered them. One function reads it now, with one default."""
    from datetime import datetime

    from meta.agent import Thinker
    from tests.helpers import SEOUL
    from voice import prep as prep_module
    from voice.call_models import coordinator_llm
    from voice.costs import SpendLog
    from beelink.planner import Plan

    monkeypatch.delenv("COORDINATOR_LLM", raising=False)
    monkeypatch.setenv("BEDROCK_REGION", "ap-southeast-2")
    assert coordinator_llm() == "echo"
    from meta import agent as meta_agent

    modes = []
    monkeypatch.setattr(prep_module, "prep_role", lambda mode: modes.append(mode))
    monkeypatch.setattr(meta_agent.model_catalog, "mode_role", lambda name, mode: modes.append(mode))
    asyncio.run(prep_module.prepare_outbound(Plan(call=True, reason="r", inbox_dir=None), spend=SpendLog(tmp_path),
                                             bee=False, now=datetime.now(SEOUL)))
    Thinker(tmp_path, bee=False)
    assert modes == ["echo", "echo"]
    monkeypatch.setenv("COORDINATOR_LLM", "anthropic")
    assert coordinator_llm() == "anthropic"
