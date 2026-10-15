"""The model catalog: the one place that knows every model choice and role, the providers that run them, the
providers' own model ids, their prices and how each role runs a model (app contract section 5b, decision 10).

Everything else names a model by its catalog id ("haiku", "sonnet", "glm") and a role:
  call     the call AI: the person's call_model, with GLM 5 standing by (voice/call_models.py, voice/agent.py)
  worker   background jobs: the person's worker_model (worker/)
  meta     the meta agent between calls, with GLM 5 standing by (meta/agent.py)
  prep     the call prep before the AI picks up, with GLM 5 standing by (voice/prep.py)
  title    the one-line title after a call (voice/titles.py)
The person picks the call and worker models in the app (GET /models lists them); the server decides the others.

A provider is reached through one small adapter, voice/providers/<provider>.py, which turns a role's request into
that provider's API (voice/providers/__init__.py says what an adapter offers). Adding a provider is that one file
and its entries here: an Offering per model, and a Choice in each role it should serve.
"""

from dataclasses import dataclass, field
from typing import Any

MILLION = 1_000_000


@dataclass(frozen=True)
class Offering:
    """A model as one provider sells it."""
    provider: str  # an adapter in voice/providers/
    model: str  # the provider's own model id
    usd_in: float  # per input token
    usd_out: float  # per output token
    cache_read_share: float = 0.1  # a cache read costs this share of an input token (Anthropic)
    cache_write_share: float = 1.25  # a 5-minute cache write costs this much of one


@dataclass(frozen=True)
class Choice:
    """One model for one role, and how that role runs it."""
    id: str  # the catalog id: what a person's setting holds, never a provider's id
    name: str  # as shown: "Claude Haiku 5.5"
    maker: str  # who makes or runs it, as shown
    description: dict[str, str]  # one short line for this role, {"ko", "en"}
    offering: Offering
    default: bool = False
    thinking: str | None = None  # the provider's thinking setting (Claude: adaptive, between_tools, disabled)
    effort: str | None = None
    max_tokens: int = 1024
    # Provider-specific run settings: temperature, read_timeout and retries (Bedrock), fields (Bedrock's
    # additionalModelRequestFields), reasoning_effort (the AgentCore worker), anthropic (more Messages API fields).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Role:
    name: str
    choices: tuple[Choice, ...]  # the default first
    standby: Choice | None = None  # answers when the choice's first request fails
    person: bool = False  # the person picks it in the app (GET /models?role=)


# ---------- what each provider sells (prices checked 2026-10-15) ----------

# Anthropic's list prices for prompts of 100K tokens or fewer (every request of ours is far below that).
CLAUDE_HAIKU = Offering("anthropic", "claude-haiku-5-5", 0.10 / MILLION, 0.50 / MILLION)
CLAUDE_SONNET = Offering("anthropic", "claude-sonnet-5-5", 2.00 / MILLION, 10.00 / MILLION)
# GLM 5 on Bedrock: third-party price lists (AWS's model card points to the pricing page without the figures).
GLM_5 = Offering("bedrock", "zai.glm-5", 1.00 / MILLION, 3.20 / MILLION)
# The same GLM 5, run by the AgentCore harness worker (worker/harness.py), priced the same.
GLM_5_WORKER = Offering("agentcore", "zai.glm-5", 1.00 / MILLION, 3.20 / MILLION)
OFFERINGS = (CLAUDE_HAIKU, CLAUDE_SONNET, GLM_5, GLM_5_WORKER)
# A model not listed is priced like this: above every model we use.
UNKNOWN = Offering("unknown", "unknown", 3.00 / MILLION, 15.00 / MILLION)
# Server tools a provider charges for apart from tokens, USD a use: Anthropic's web search ($10 per 1,000; web
# fetch has no charge, code execution none with web search or web fetch in the request).
SERVER_TOOL_USD = {("anthropic", "web_search"): 10.00 / 1000}

GLM_STANDBY = Choice("glm", "GLM 5", "AWS", {"ko": "대기 모델", "en": "Standing by"}, GLM_5, max_tokens=2000)

ROLES: dict[str, Role] = {role.name: role for role in (
    Role("call", person=True, standby=GLM_STANDBY, choices=(
        # Adaptive, the user's choice (2026-10-15): with thinking disabled for speed, Haiku said it booked a call
        # back and never called book_call in 3 of 6 probe calls, and once spoke its plan in English in a Korean
        # call; adaptive at effort low booked 6 of 6 and spoke no notes (first words median 1.45 s against 1.23 s;
        # probes/haiku_probe.py).
        Choice("haiku", "Claude Haiku 5.5", "Anthropic", {"ko": "가장 빠르게 대답해요", "en": "Answers fastest"},
               CLAUDE_HAIKU, default=True, thinking="adaptive", effort="low"),
        # Claude Sonnet 5.5 refuses "disabled"; "between_tools" skips thinking before the first words.
        Choice("sonnet", "Claude Sonnet 5.5", "Anthropic",
               {"ko": "더 깊이 생각하고, 조금 느려요", "en": "Thinks more, a little slower"},
               CLAUDE_SONNET, thinking="between_tools", effort="low"),
    )),
    Role("worker", person=True, choices=(
        # The user's choice (2026-10-15): thinking on and effort high; background work need not be quick.
        Choice("sonnet", "Claude Sonnet 5.5", "Anthropic",
               {"ko": "조사와 보고서가 가장 꼼꼼해요", "en": "Most thorough research and reports"},
               CLAUDE_SONNET, default=True, thinking="adaptive", effort="high", max_tokens=32000),
        # GLM 5 reasons only when asked to (probed 2026-10-15); 32K holds any report in one call and leaves about
        # 170K of its 202,752-token context for the job's conversation (worker/harness.py).
        Choice("glm", "GLM 5", "AWS", {"ko": "AWS에서 돌아가요", "en": "Runs on AWS"}, GLM_5_WORKER,
               max_tokens=32768, extra={"reasoning_effort": "high"}),
    )),
    Role("meta", standby=Choice("glm", "GLM 5", "AWS", {"ko": "대기 모델", "en": "Standing by"}, GLM_5,
                                max_tokens=2000, extra={"read_timeout": 90, "retries": 3}), choices=(
        Choice("sonnet", "Claude Sonnet 5.5", "Anthropic", {"ko": "통화 사이의 판단", "en": "Decides between calls"},
               CLAUDE_SONNET, default=True, thinking="adaptive", effort="medium", max_tokens=8000),
    )),
    Role("prep", standby=Choice("glm", "GLM 5", "AWS", {"ko": "대기 모델", "en": "Standing by"}, GLM_5,
                                max_tokens=500, extra={"temperature": 0.3, "read_timeout": 20}), choices=(
        # The opening has to be written within seconds of the phone coming into the room: no thinking.
        Choice("haiku", "Claude Haiku 5.5", "Anthropic", {"ko": "통화 준비", "en": "Prepares the call"},
               CLAUDE_HAIKU, default=True, thinking="disabled", effort="low", max_tokens=700),
    )),
    Role("title", choices=(
        Choice("haiku", "Claude Haiku 5.5", "Anthropic", {"ko": "통화 제목", "en": "Titles a call"}, CLAUDE_HAIKU,
               default=True, thinking="disabled", effort="low", max_tokens=80),
    )),
)}


def role(name: str) -> Role:
    """A role by name; KeyError for an unknown one."""
    return ROLES[name]


def choice(role_name: str, model_id: str | None = None) -> Choice:
    """The role's choice `model_id`, or its default when it has none by that id (a stored id the catalog no longer
    has reads as the default)."""
    found = role(role_name)
    return next((c for c in found.choices if c.id == model_id), None) or next(c for c in found.choices if c.default)


def ids(role_name: str) -> tuple[str, ...]:
    return tuple(c.id for c in role(role_name).choices)


def price(provider_model: str | None) -> Offering:
    """How a provider's model is priced, by the provider's model id (as usage records name it)."""
    return next((o for o in OFFERINGS if o.model == provider_model), UNKNOWN)


def models_body(role_name: str) -> dict[str, Any]:
    """GET /models?role=: a person-facing role's choices, the default first (contract section 5b)."""
    return {"role": role_name, "models": [
        {"id": c.id, "name": c.name, "maker": c.maker, "description": dict(c.description), "default": c.default}
        for c in role(role_name).choices]}
