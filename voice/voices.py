"""The voice the AI speaks with: the person's pick from the voice catalog, voice/voice_catalog.json (contract section
5, decision 9), or Amazon Polly on a machine without ElevenLabs.

The catalog lists each call language's voices in the order the app shows them, the default first. Each entry has a
provider-independent id (ko-sian), the voice's own name, one word for how it sounds in Korean and English, its
provider and that provider's voice, a sample of about five seconds in voice/voice_samples/ (named by its content's
hash, so a new sample gets a new URL), and server-only speech tuning that the app never sees, tuned by ear:
  stability  0 to 1, lower is livelier and less steady (ElevenLabs voice settings)
  (empty for every voice so far; how a voice laughs, a tag or written laughter, will go here too)
A person's settings hold a catalog id or null (the language's default); voice/people.py keeps it one of the call
language's.

The machine's own settings, for every call on it:
  TTS                   polly: every call speaks with Polly (the call language's voice, or POLLY_VOICE). Unset or
                        elevenlabs: the catalog's provider. Without ELEVENLABS_API_KEY (Text to Speech permission)
                        a call speaks with Polly too, and its record says so (the coordinator event's voice)
  ELEVENLABS_MODEL      eleven_v4_turbo when unset (the most expressive fast model; it goes through ElevenLabs'
                        text-to-dialogue stream); eleven_flash_v2_5 is the older fast one
  ELEVENLABS_STABILITY  0 to 1, for voices whose tuning does not set it
"""

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from livekit.agents import tts
from livekit.plugins import aws, elevenlabs

from voice.catalog import SAMPLES, CatalogVoice, catalog, catalog_id, catalog_voice  # noqa: F401 (also for callers)
from voice.language import CallLanguage

PROVIDERS = ("polly", "elevenlabs")
# Measured from Seoul on 2026-10-14: first sound 0.15-0.24 s, as quick as Flash v2.5, and the voice Matt preferred.
ELEVENLABS_MODEL = "eleven_v4_turbo"
SIMILARITY_BOOST = 0.75  # ElevenLabs' default; VoiceSettings needs it whenever stability is set


@dataclass(frozen=True)
class VoiceChoice:
    provider: str
    voice: str  # a Polly voice name, or an ElevenLabs voice id
    model: str  # Polly's engine, or the ElevenLabs model
    stability: float | None = None
    catalog: str | None = None  # the person's catalog voice this stands for

    def describe(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def elevenlabs_key(env: Mapping[str, str] = os.environ) -> str:
    # ELEVEN_API_KEY is the LiveKit plugin's own name for it.
    return env.get("ELEVENLABS_API_KEY") or env.get("ELEVEN_API_KEY") or ""


def voice_choice(language: CallLanguage, voice_id: str | None = None,
                 env: Mapping[str, str] = os.environ) -> VoiceChoice:
    """The voice a call speaks with: the person's catalog voice (`voice_id`) when it is one of the call language's,
    else the language's default."""
    return choose(catalog_voice(language.code, voice_id), language, env)


def choose(voice: CatalogVoice, language: CallLanguage, env: Mapping[str, str] = os.environ) -> VoiceChoice:
    """How `voice` is spoken on this machine: by its provider, or by Polly (TTS=polly, or no ElevenLabs key)."""
    provider = (env.get("TTS") or "").strip().lower()
    if provider and provider not in PROVIDERS:
        raise SystemExit(f"TTS must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    stability = env.get("ELEVENLABS_STABILITY")
    try:
        stability_value = float(stability) if stability else None
    except ValueError:
        raise SystemExit(f"ELEVENLABS_STABILITY must be a number from 0 to 1, not {stability!r}") from None
    if stability_value is not None and not 0 <= stability_value <= 1:
        raise SystemExit(f"ELEVENLABS_STABILITY must be from 0 to 1, not {stability_value}")
    if provider == "polly" or voice.provider != "elevenlabs" or not elevenlabs_key(env):
        return VoiceChoice("polly", env.get("POLLY_VOICE") or language.voice, language.engine, catalog=voice.id)
    tuned = voice.tuning.get("stability")
    return VoiceChoice("elevenlabs", voice.provider_voice, env.get("ELEVENLABS_MODEL") or ELEVENLABS_MODEL,
                       float(tuned) if isinstance(tuned, (int, float)) else stability_value, catalog=voice.id)


def make_tts(choice: VoiceChoice, language: CallLanguage, region: str, *, env: Mapping[str, str] = os.environ,
             http_session: Any = None) -> tts.TTS:
    """The LiveKit TTS for a choice. `http_session` is for use outside a voice server job (the samples probe)."""
    if choice.provider == "polly":
        return aws.TTS(voice=choice.voice, speech_engine=choice.model, region=region)
    options: dict[str, Any] = {}
    if choice.stability is not None:
        options["voice_settings"] = elevenlabs.VoiceSettings(stability=choice.stability,
                                                             similarity_boost=SIMILARITY_BOOST)
    if http_session is not None:
        options["http_session"] = http_session
    return elevenlabs.TTS(voice_id=choice.voice, model=choice.model, language=language.code,
                          api_key=elevenlabs_key(env), **options)
