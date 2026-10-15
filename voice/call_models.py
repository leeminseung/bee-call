"""The call AI's model, the person's choice (Settings.call_model, set in the app), as the model catalog's call role
gives it (voice/model_catalog.py, the one place for models and their settings) for COORDINATOR_LLM=anthropic
(voice/agent.py). GLM 5 on Bedrock stands by for both (voice/fallback_llm.py); the daily budget prices whichever
model answered (voice/costs.py). The environment settings the other modes read (COORDINATOR_MODEL,
COORDINATOR_THINKING, COORDINATOR_EFFORT) change nothing here; a call that finds one set says so in its log and its
record (voice/agent.py ignored_settings).
"""

import os
from dataclasses import dataclass
from typing import Any

from voice import model_catalog

# Which models the call AI, the prep and the meta agent use (voice/agent.py lists the values). Unset: the echo
# stand-in, which costs nothing; a server gets its own from scripts/deploy_server.py.
COORDINATOR_LLM_DEFAULT = "echo"


def coordinator_llm() -> str:
    """COORDINATOR_LLM, read the same way by every process (server review 19): voice/outbound.py's prep never
    prepares with another model than the one the voice server answers with."""
    return os.environ.get("COORDINATOR_LLM", "").strip() or COORDINATOR_LLM_DEFAULT


@dataclass(frozen=True)
class CallModel:
    name: str  # the setting's value
    model: str  # Anthropic's model id (no prefix on Anthropic's own API)
    thinking: str  # Claude's thinking type
    effort: str  # one effort level for the whole call


CALL_MODELS = {c.id: CallModel(c.id, c.offering.model, thinking=c.thinking, effort=c.effort)
               for c in model_catalog.role("call").choices}
DEFAULT_CALL_MODEL = model_catalog.choice("call").id


def call_model_for(settings: Any) -> CallModel:
    """The person's call model: their setting, or the default (a stored value that is no longer known keeps it)."""
    return CALL_MODELS[model_catalog.choice("call", getattr(settings, "call_model", None)).id]
