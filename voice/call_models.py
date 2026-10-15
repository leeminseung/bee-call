"""The call AI's model, the person's choice (Settings.call_model, set in the app): the one place that maps the choice
to Claude's model and settings for COORDINATOR_LLM=anthropic (voice/agent.py). GLM 5 on Bedrock stands by for both
(voice/fallback_llm.py); the daily budget prices whichever model answered (voice/costs.py). The environment settings
the other modes read (COORDINATOR_MODEL, COORDINATOR_THINKING, COORDINATOR_EFFORT) change nothing here; a call that
finds one set says so in its log and its record (voice/agent.py ignored_settings).
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallModel:
    name: str  # the setting's value
    model: str  # Anthropic's model id (no prefix on Anthropic's own API)
    thinking: str  # Claude's thinking type
    effort: str  # one effort level for the whole call


CALL_MODELS = {
    # Adaptive, the user's choice (2026-10-15): with thinking disabled for speed, Haiku said it booked a call back and
    # never called book_call in 3 of 6 probe calls, and once spoke its plan in English in a Korean call; adaptive
    # at effort low booked 6 of 6 and spoke no notes (first words median 1.45 s against 1.23 s; probes/haiku_probe.py).
    "haiku": CallModel("haiku", "claude-haiku-5-5", thinking="adaptive", effort="low"),
    # Claude Sonnet 5.5 refuses "disabled"; "between_tools" skips thinking before the first words.
    "sonnet": CallModel("sonnet", "claude-sonnet-5-5", thinking="between_tools", effort="low"),
}
DEFAULT_CALL_MODEL = "haiku"


def call_model_for(settings: Any) -> CallModel:
    """The person's call model: their setting, or the default (a stored value that is no longer known keeps it)."""
    return CALL_MODELS.get(getattr(settings, "call_model", None), CALL_MODELS[DEFAULT_CALL_MODEL])
