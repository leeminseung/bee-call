"""The model catalog (voice/model_catalog.py): every model choice and role in one place, provider-independent ids,
providers, their model ids, prices and run settings (app contract section 5b, decision 10)."""

import re

import pytest

from voice import costs, model_catalog
from voice.call_models import CALL_MODELS, call_model_for
from voice.model_catalog import ROLES, choice, role
from voice.people import Settings

MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,40}")


def test_each_role_lists_its_choices_default_first_with_ids_of_the_catalog_s_own():
    for name, found in ROLES.items():
        assert 1 <= len(found.choices) <= 12 and found.choices[0].default, name
        assert sum(c.default for c in found.choices) == 1, name
        for c in found.choices:
            assert MODEL_ID.fullmatch(c.id) and c.name and c.maker and c.description["ko"] and c.description["en"]
            assert c.offering.provider in ("anthropic", "bedrock", "agentcore")
    assert [c.id for c in role("call").choices] == ["haiku", "sonnet"]
    assert [c.id for c in role("worker").choices] == ["sonnet", "glm"]
    assert {name for name, found in ROLES.items() if found.person} == {"call", "worker"}


def test_the_user_s_decisions_on_who_runs_what():
    """Decision 10 of the app contract and the user's choices of 2026-10-15: the worker is Claude Sonnet 5.5 on
    Anthropic's API with thinking on at effort high, its conversation cached as it grows (worker/claude.py), and GLM 5
    on the AgentCore harness stays selectable."""
    worker = choice("worker")
    assert (worker.id, worker.offering.provider, worker.offering.model, worker.thinking, worker.effort) == (
        "sonnet", "anthropic", "claude-sonnet-5-5", "adaptive", "high")
    assert worker.extra["anthropic"] == {"cache_control": {"type": "ephemeral"}}
    glm = choice("worker", "glm")
    assert (glm.offering.provider, glm.offering.model, glm.default) == ("agentcore", "zai.glm-5", False)
    # Z.ai makes GLM 5 and AWS runs it: the app's row reads "Z.ai · AWS에서 돌아가요", never "AWS · AWS에서 ...".
    assert {c.maker for c in (glm, role("call").standby, role("meta").standby, role("prep").standby)} == {"Z.ai"}
    assert glm.description == {"ko": "AWS에서 돌아가요", "en": "Runs on AWS"}
    meta, prep = choice("meta"), choice("prep")
    assert (meta.offering.model, prep.offering.model) == ("claude-sonnet-5-5", "claude-haiku-5-5")
    for name in ("call", "meta", "prep"):
        assert (role(name).standby.offering.provider, role(name).standby.offering.model) == ("bedrock", "zai.glm-5")
    assert not role("meta").person and not role("prep").person


def test_a_stored_id_the_catalog_no_longer_has_reads_as_the_role_s_default():
    assert choice("call", "sonnet").id == "sonnet"
    assert choice("call", "opus-9").id == choice("call", None).id == "haiku"
    assert choice("worker", "haiku").id == "sonnet"  # haiku is a call model, not a worker
    with pytest.raises(KeyError):
        role("nobody")


def test_the_call_ai_s_models_come_from_the_catalog():
    assert {name: (m.model, m.thinking, m.effort) for name, m in CALL_MODELS.items()} == {
        "haiku": ("claude-haiku-5-5", "adaptive", "low"), "sonnet": ("claude-sonnet-5-5", "between_tools", "low")}
    assert call_model_for(Settings(call_model="sonnet")).model == "claude-sonnet-5-5"
    assert call_model_for(Settings()).model == "claude-haiku-5-5"


def test_a_claude_worker_job_is_priced_with_its_cache_and_its_web_searches():
    """A Claude worker job's usage.json (worker/claude.py): tokens at Claude Sonnet 5.5's prices, a cache read at a
    tenth of an input token and a cache write at 1.25, and each web search at Anthropic's 10 USD per 1,000; web fetch
    and code execution cost nothing beyond their tokens. The harness's own web_search (Nova) still counts by call."""
    usage = {"provider": "anthropic", "model": "claude-sonnet-5-5", "input_tokens": 1_000_000, "cache_read": 800_000,
             "cache_write": 100_000, "output_tokens": 100_000, "server_tools": {"web_search": 30, "web_fetch": 12},
             "steps": [{"calls": [{"name": "save_file", "s": 0.0}]}]}
    tokens = (2.00 * (100_000 + 0.1 * 800_000 + 1.25 * 100_000) + 10.00 * 100_000) / 1_000_000
    assert costs.job_cost(usage) == pytest.approx(tokens + 30 * 0.01)
    nova = {"input_tokens": 0, "output_tokens": 0, "steps": [{"calls": [{"name": "web_search", "s": 3.0}]}]}
    assert costs.job_cost(nova) == pytest.approx(costs.WEB_SEARCH_USD)


def test_the_budget_prices_models_as_the_catalog_does():
    million = 1_000_000
    assert costs.llm_cost("claude-haiku-5-5", million, million) == pytest.approx(0.60)
    assert costs.llm_cost("claude-sonnet-5-5", million, 0, cached=million) == pytest.approx(0.20)
    assert costs.llm_cost("zai.glm-5", million, million) == pytest.approx(4.20)
    assert costs.llm_cost("some-new-model", million, 0) == pytest.approx(3.00)  # never cheaper than a known one
    assert model_catalog.price("claude-sonnet-5-5").usd_in == pytest.approx(2.00 / million)
