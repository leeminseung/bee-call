"""The voice the AI speaks with: Amazon Polly (the default) or ElevenLabs, picked with TTS.

Polly needs nothing beyond the AWS profile the voice server already uses. ElevenLabs sounds closer to a person;
it needs ELEVENLABS_API_KEY with the Text to Speech permission, and a voice from the account's My Voices list
(Voice Library, "Add to my voices"). `uv run python -m probes.voice_samples --list` shows those voices, with
the Voices read permission.

  TTS                   polly (default) or elevenlabs
  POLLY_VOICE           a Polly voice in place of the call language's (Ruth for English, Seoyeon for Korean)
  ELEVENLABS_VOICE      a voice id in place of the call language's
  ELEVENLABS_MODEL      eleven_flash_v2_5 when unset; eleven_v3_conversational and eleven_v4_turbo are more
                        expressive and go through ElevenLabs' text-to-dialogue stream
  ELEVENLABS_STABILITY  0 to 1, lower is livelier and less steady; unset keeps the voice's own setting
"""

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from livekit.agents import tts
from livekit.plugins import aws, elevenlabs

from voice.language import CallLanguage

PROVIDERS = ("polly", "elevenlabs")
ELEVENLABS_MODEL = "eleven_flash_v2_5"  # ElevenLabs' fastest model, made for conversations
SIMILARITY_BOOST = 0.75  # ElevenLabs' default; VoiceSettings needs it whenever stability is set


@dataclass(frozen=True)
class VoiceChoice:
    provider: str
    voice: str  # a Polly voice name, or an ElevenLabs voice id
    model: str  # Polly's engine, or the ElevenLabs model
    stability: float | None = None

    def describe(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def elevenlabs_key(env: Mapping[str, str] = os.environ) -> str:
    # ELEVEN_API_KEY is the LiveKit plugin's own name for it.
    return env.get("ELEVENLABS_API_KEY") or env.get("ELEVEN_API_KEY") or ""


def voice_choice(language: CallLanguage, env: Mapping[str, str] = os.environ) -> VoiceChoice:
    provider = (env.get("TTS") or "polly").strip().lower()
    if provider not in PROVIDERS:
        raise SystemExit(f"TTS must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    if provider == "polly":
        return VoiceChoice("polly", env.get("POLLY_VOICE") or language.voice, language.engine)
    voice = env.get("ELEVENLABS_VOICE") or language.elevenlabs_voice
    if not voice:
        raise SystemExit("TTS=elevenlabs needs a voice: set ELEVENLABS_VOICE to a voice id from My Voices "
                         "(uv run python -m probes.voice_samples --list)")
    if not elevenlabs_key(env):
        raise SystemExit("TTS=elevenlabs needs ELEVENLABS_API_KEY, with the Text to Speech permission")
    stability = env.get("ELEVENLABS_STABILITY")
    try:
        stability_value = float(stability) if stability else None
    except ValueError:
        raise SystemExit(f"ELEVENLABS_STABILITY must be a number from 0 to 1, not {stability!r}") from None
    if stability_value is not None and not 0 <= stability_value <= 1:
        raise SystemExit(f"ELEVENLABS_STABILITY must be from 0 to 1, not {stability_value}")
    return VoiceChoice("elevenlabs", voice, env.get("ELEVENLABS_MODEL") or ELEVENLABS_MODEL, stability_value)


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
