"""The voice catalog, voice/voice_catalog.json: each call language's voices for the app's lists (GET /voices) and
the person's voice setting (contract section 5, decision 9). voice/voices.py turns an entry into the call's voice.

Each entry: a provider-independent id (ko-sian), the voice's own name, one word for how it sounds in Korean and
English, whether it is the language's default (exactly one, listed first), its provider and that provider's voice,
a sample of about five seconds in voice/voice_samples/ named by its content's hash (a new sample gets a new URL),
and server-only speech tuning the app never sees (voice/voices.py says what it may hold).
"""

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).with_name("voice_catalog.json")
SAMPLES = Path(__file__).with_name("voice_samples")


@dataclass(frozen=True)
class CatalogVoice:
    id: str  # ko-sian: the value of a person's voice setting
    name: str
    character: dict[str, str]  # {"ko": "차분함", "en": "Calm"}
    default: bool
    provider: str
    provider_voice: str
    sample: str  # a file in SAMPLES
    tuning: dict[str, Any] = field(default_factory=dict)  # server-only, tuned by ear


@cache
def _catalog() -> dict[str, tuple[CatalogVoice, ...]]:
    data = json.loads(CATALOG.read_text())
    return {code: tuple(CatalogVoice(**entry) for entry in entries) for code, entries in data.items()}


def catalog(language: str) -> tuple[CatalogVoice, ...]:
    """A call language's voices, in the order the app lists them, the default first; () for an unknown language."""
    return _catalog().get(language, ())


def catalog_voice(language: str, voice_id: str | None) -> CatalogVoice:
    """The voice `voice_id` when it is one of the language's, else the language's default."""
    voices = catalog(language)
    return next((v for v in voices if v.id == voice_id), None) or next(v for v in voices if v.default)


def catalog_id(value: str | None) -> str | None:
    """A stored voice as a catalog id: a catalog id stays, a provider's voice id becomes its catalog voice's (the
    hand-set values from before the catalog), anything else None."""
    for voices in _catalog().values():
        for voice in voices:
            if value in (voice.id, voice.provider_voice):
                return voice.id
    return None


def sample_files() -> set[str]:
    return {voice.sample for voices in _catalog().values() for voice in voices}
