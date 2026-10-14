"""The AI's voice: Amazon Polly by default, ElevenLabs with TTS=elevenlabs. No network: building a TTS opens
no connection."""

from dataclasses import replace

import pytest

from voice.language import call_language
from voice.voices import ELEVENLABS_MODEL, VoiceChoice, make_tts, voice_choice

ENGLISH, KOREAN = call_language("en"), call_language("ko")
ELEVEN = {"TTS": "elevenlabs", "ELEVENLABS_VOICE": "voice123", "ELEVENLABS_API_KEY": "key"}


def test_polly_is_the_default_and_follows_the_call_language():
    assert voice_choice(ENGLISH, {}) == VoiceChoice("polly", "Ruth", "generative")
    assert voice_choice(KOREAN, {"TTS": "Polly "}) == VoiceChoice("polly", "Seoyeon", "neural")
    assert voice_choice(ENGLISH, {"POLLY_VOICE": "Danielle"}).voice == "Danielle"
    with pytest.raises(SystemExit):
        voice_choice(ENGLISH, {"TTS": "azure"})


def test_elevenlabs_needs_a_voice_and_a_key():
    # Each call language has a default voice: Sarah for English, Sian for Korean.
    assert voice_choice(KOREAN, {"TTS": "elevenlabs", "ELEVENLABS_API_KEY": "key"}).voice == "5n5gqmaQi9Ewevrz7bOS"
    assert voice_choice(ENGLISH, {"TTS": "elevenlabs", "ELEVENLABS_API_KEY": "key"}).voice == "EXAVITQu4vr4xnSDxMaL"
    with pytest.raises(SystemExit, match="ELEVENLABS_VOICE"):
        voice_choice(replace(ENGLISH, elevenlabs_voice=""), {"TTS": "elevenlabs", "ELEVENLABS_API_KEY": "key"})
    with pytest.raises(SystemExit, match="ELEVENLABS_API_KEY"):
        voice_choice(ENGLISH, {"TTS": "elevenlabs", "ELEVENLABS_VOICE": "voice123"})
    assert voice_choice(ENGLISH, ELEVEN) == VoiceChoice("elevenlabs", "voice123", ELEVENLABS_MODEL)
    assert voice_choice(ENGLISH, {"TTS": "elevenlabs", "ELEVENLABS_VOICE": "voice123",
                                  "ELEVEN_API_KEY": "key"}).provider == "elevenlabs"  # the plugin's own name


def test_the_model_and_the_stability_can_be_set():
    choice = voice_choice(KOREAN, {**ELEVEN, "ELEVENLABS_MODEL": "eleven_v3_conversational",
                                   "ELEVENLABS_STABILITY": "0.35"})
    assert (choice.model, choice.stability) == ("eleven_v3_conversational", 0.35)
    assert choice.describe() == {"provider": "elevenlabs", "voice": "voice123",
                                 "model": "eleven_v3_conversational", "stability": 0.35}
    for bad in ("lively", "1.5"):
        with pytest.raises(SystemExit, match="ELEVENLABS_STABILITY"):
            voice_choice(ENGLISH, {**ELEVEN, "ELEVENLABS_STABILITY": bad})


def test_each_choice_builds_its_livekit_tts():
    polly = make_tts(voice_choice(KOREAN, {}), KOREAN, "ap-southeast-2")
    assert (polly._opts.voice, polly._opts.speech_engine) == ("Seoyeon", "neural")

    choice = voice_choice(KOREAN, {**ELEVEN, "ELEVENLABS_STABILITY": "0.3"})
    eleven = make_tts(choice, KOREAN, "ap-southeast-2", env=ELEVEN)
    assert (eleven._opts.voice_id, eleven._opts.model, eleven._opts.language) == ("voice123", ELEVENLABS_MODEL, "ko")
    assert eleven._opts.voice_settings.stability == 0.3
