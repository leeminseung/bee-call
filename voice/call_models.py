"""The call AI's model, the person's choice (Settings.call_model, set in the app): the one place that maps the choice
to Claude's model and settings for COORDINATOR_LLM=anthropic (voice/agent.py). GLM 5 on Bedrock stands by for both
(voice/fallback_llm.py); the daily budget prices whichever model answered (voice/costs.py).
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
    # Thinking off for speed; Claude Haiku 5.5 takes "disabled" at effort high or below.
    "haiku": CallModel("haiku", "claude-haiku-5-5", thinking="disabled", effort="low"),
    # Claude Sonnet 5.5 refuses "disabled"; "between_tools" skips thinking before the first words.
    "sonnet": CallModel("sonnet", "claude-sonnet-5-5", thinking="between_tools", effort="low"),
}
DEFAULT_CALL_MODEL = "haiku"


def call_model_for(settings: Any) -> CallModel:
    """The person's call model: their setting, or the default (a stored value that is no longer known keeps it)."""
    return CALL_MODELS.get(getattr(settings, "call_model", None), CALL_MODELS[DEFAULT_CALL_MODEL])
