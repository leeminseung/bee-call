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
    # Only what runs (the coordinator, 2026-10-15): Claude Sonnet 5.5 joins as the default with the Claude runner.
    assert [c.id for c in role("worker").choices] == ["glm"]
    assert {name for name, found in ROLES.items() if found.person} == {"call", "worker"}


def test_the_user_s_decisions_on_who_runs_what():
    """Decision 10 of the app contract and the user's choices of 2026-10-15; the worker runs GLM 5 on the AgentCore
    harness until the Claude runner exists."""
    worker = choice("worker")
    assert (worker.id, worker.offering.provider, worker.offering.model) == ("glm", "agentcore", "zai.glm-5")
    meta, prep = choice("meta"), choice("prep")
    assert (meta.offering.model, prep.offering.model) == ("claude-sonnet-5-5", "claude-haiku-5-5")
    for name in ("call", "meta", "prep"):
        assert (role(name).standby.offering.provider, role(name).standby.offering.model) == ("bedrock", "zai.glm-5")
    assert not role("meta").person and not role("prep").person


def test_a_stored_id_the_catalog_no_longer_has_reads_as_the_role_s_default():
    assert choice("call", "sonnet").id == "sonnet"
    assert choice("call", "opus-9").id == choice("call", None).id == "haiku"
    assert choice("worker", "haiku").id == "glm"  # haiku is a call model, not a worker
    assert choice("worker", "sonnet").id == "glm"  # not a worker until the Claude runner exists
    with pytest.raises(KeyError):
        role("nobody")


def test_the_call_ai_s_models_come_from_the_catalog():
    assert {name: (m.model, m.thinking, m.effort) for name, m in CALL_MODELS.items()} == {
        "haiku": ("claude-haiku-5-5", "adaptive", "low"), "sonnet": ("claude-sonnet-5-5", "between_tools", "low")}
    assert call_model_for(Settings(call_model="sonnet")).model == "claude-sonnet-5-5"
    assert call_model_for(Settings()).model == "claude-haiku-5-5"


def test_the_budget_prices_models_as_the_catalog_does():
    million = 1_000_000
    assert costs.llm_cost("claude-haiku-5-5", million, million) == pytest.approx(0.60)
    assert costs.llm_cost("claude-sonnet-5-5", million, 0, cached=million) == pytest.approx(0.20)
    assert costs.llm_cost("zai.glm-5", million, million) == pytest.approx(4.20)
    assert costs.llm_cost("some-new-model", million, 0) == pytest.approx(3.00)  # never cheaper than a known one
    assert model_catalog.price("claude-sonnet-5-5").usd_in == pytest.approx(2.00 / million)
