"""The AI's voice: the person's pick from the voice catalog (voice/voice_catalog.json, contract section 5), spoken by
the catalog's provider (ElevenLabs), or by Amazon Polly on a machine without ElevenLabs. No network: building a TTS
opens no connection."""

import hashlib

import pytest

from voice import voices
from voice.language import call_language
from voice.voices import ELEVENLABS_MODEL, SAMPLES, VoiceChoice, catalog, make_tts, voice_choice

ENGLISH, KOREAN = call_language("en"), call_language("ko")
KEY = {"ELEVENLABS_API_KEY": "key"}


def test_each_language_lists_its_voices_default_first_each_with_a_sample_named_by_its_content():
    """Decision 9 of the contract: the user's picks, provider-independent ids, one default per language, listed
    first; each sample's file name carries its content's hash, so a new sample gets a new URL."""
    assert [v.id for v in catalog("ko")] == ["ko-sian", "ko-han", "ko-anna", "ko-yooni", "ko-seulki", "ko-kyle",
                                             "ko-mrk", "ko-jason"]
    assert [v.id for v in catalog("en")] == ["en-zara", "en-amelia", "en-hope", "en-will", "en-chris"]
    for code in ("ko", "en"):
        listed = catalog(code)
        assert 1 <= len(listed) <= 12 and listed[0].default and sum(v.default for v in listed) == 1
        for voice in listed:
            assert voice.id.startswith(f"{code}-") and voice.name and voice.character["ko"] and voice.character["en"]
            data = (SAMPLES / voice.sample).read_bytes()
            assert voice.sample == f"{voice.id}-{hashlib.sha256(data).hexdigest()[:8]}.mp3"
            assert data[:3] == b"ID3" or data[:2] == b"\xff\xfb"  # an MP3
    assert catalog("ko")[0].provider_voice == "5n5gqmaQi9Ewevrz7bOS"  # Sian


def test_a_call_speaks_with_the_person_s_voice_of_its_language_or_the_language_s_default():
    assert voice_choice(KOREAN, "ko-han", KEY) == VoiceChoice("elevenlabs", "8jHHF8rMqMlg8if2mOUe", ELEVENLABS_MODEL,
                                                              catalog="ko-han")
    assert voice_choice(KOREAN, None, KEY).catalog == "ko-sian"
    assert voice_choice(KOREAN, "en-zara", KEY).catalog == "ko-sian"  # another language's voice: the default
    assert voice_choice(ENGLISH, "nonsense", KEY).catalog == "en-zara"


def test_without_elevenlabs_or_with_tts_polly_the_call_speaks_with_polly():
    """A machine without an ElevenLabs key (a Mac for tests), or TTS=polly (the operator's choice for every call),
    speaks with the call language's Polly voice; the record names it."""
    assert voice_choice(ENGLISH, "en-hope", {}) == VoiceChoice("polly", "Ruth", "generative", catalog="en-hope")
    assert voice_choice(KOREAN, None, {**KEY, "TTS": "Polly "}) == VoiceChoice("polly", "Seoyeon", "neural",
                                                                              catalog="ko-sian")
    assert voice_choice(ENGLISH, None, {"POLLY_VOICE": "Danielle"}).voice == "Danielle"
    with pytest.raises(SystemExit):
        voice_choice(ENGLISH, None, {**KEY, "TTS": "azure"})


def test_a_voice_s_own_tuning_comes_first_and_the_machine_s_model_and_stability_after():
    choice = voice_choice(KOREAN, "ko-han", {**KEY, "ELEVENLABS_MODEL": "eleven_v3_conversational",
                                             "ELEVENLABS_STABILITY": "0.35"})
    assert (choice.model, choice.stability) == ("eleven_v3_conversational", 0.35)
    assert choice.describe() == {"provider": "elevenlabs", "voice": "8jHHF8rMqMlg8if2mOUe",
                                 "model": "eleven_v3_conversational", "stability": 0.35, "catalog": "ko-han"}
    tuned = voices.CatalogVoice(**{**catalog("ko")[1].__dict__, "tuning": {"stability": 0.6}})
    assert voices.choose(tuned, KOREAN, {**KEY, "ELEVENLABS_STABILITY": "0.35"}).stability == 0.6
    for bad in ("lively", "1.5"):
        with pytest.raises(SystemExit, match="ELEVENLABS_STABILITY"):
            voice_choice(ENGLISH, None, {**KEY, "ELEVENLABS_STABILITY": bad})


def test_each_choice_builds_its_livekit_tts(monkeypatch):
    """What each LiveKit TTS is given, read from the arguments the plugins get rather than their private state
    (server review 16)."""
    built = []

    def plugin(name):
        return lambda **kwargs: built.append((name, kwargs)) or name

    monkeypatch.setattr(voices.aws, "TTS", plugin("polly"))
    monkeypatch.setattr(voices.elevenlabs, "TTS", plugin("elevenlabs"))
    assert make_tts(voice_choice(KOREAN, None, {}), KOREAN, "ap-southeast-2") == "polly"
    choice = voice_choice(KOREAN, "ko-han", {**KEY, "ELEVENLABS_STABILITY": "0.3"})
    assert make_tts(choice, KOREAN, "ap-southeast-2", env=KEY) == "elevenlabs"
    (_, polly), (_, eleven) = built
    assert polly == {"voice": "Seoyeon", "speech_engine": "neural", "region": "ap-southeast-2"}
    assert (eleven["voice_id"], eleven["model"], eleven["language"]) == ("8jHHF8rMqMlg8if2mOUe", ELEVENLABS_MODEL, "ko")
    assert eleven["voice_settings"].stability == 0.3 and eleven["api_key"]


def test_a_hand_set_voice_moves_to_its_catalog_id():
    """Decision 9: today's hand-set provider voice ids move to the matching catalog id, anything else to null."""
    assert voices.catalog_id("5n5gqmaQi9Ewevrz7bOS") == "ko-sian"
    assert voices.catalog_id("ko-han") == "ko-han"
    assert voices.catalog_id("Seoyeon") is None and voices.catalog_id(None) is None


def test_a_voice_set_by_hand_on_the_machine_is_named_as_not_used(monkeypatch):
    """ELEVENLABS_VOICE once picked every call's voice; the catalog decides now, so a call that finds it set says so in
    its log and its record (the coordinator event's settings_not_used), as for the call model's settings."""
    from voice import agent

    monkeypatch.setattr(agent, "COORDINATOR_LLM", "echo")
    monkeypatch.setenv("ELEVENLABS_VOICE", "EXAVITQu4vr4xnSDxMaL")
    assert agent.ignored_settings() == ["ELEVENLABS_VOICE"]
    monkeypatch.delenv("ELEVENLABS_VOICE")
    assert agent.ignored_settings() == []
