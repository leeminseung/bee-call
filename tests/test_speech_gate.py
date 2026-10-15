"""A transcript whose audio was too quiet to be the user is not passed on (voice/speech_gate.py). The levels are the
ones measured in the 2026-10-15 01:23 call: the AI's voice coming back at RMS about 60, real speech above 1,400."""

import asyncio

import numpy as np
from livekit import rtc
from livekit.agents import stt

from voice.speech_gate import SpeechGate

RATE = 24000
FRAME_S = 0.02


def frames(seconds: float, rms: float) -> list[rtc.AudioFrame]:
    """20 ms frames of a 200 Hz tone whose RMS is `rms`."""
    n = int(RATE * FRAME_S)
    out = []
    for i in range(round(seconds / FRAME_S)):
        t = (np.arange(n) + i * n) / RATE
        tone = np.sin(2 * np.pi * 200 * t) * rms * np.sqrt(2)
        out.append(rtc.AudioFrame(tone.astype(np.int16).tobytes(), RATE, 1, n))
    return out


def transcript(text: str, final: bool = True, start: float = 0.0, end: float = 0.0) -> stt.SpeechEvent:
    kind = stt.SpeechEventType.FINAL_TRANSCRIPT if final else stt.SpeechEventType.INTERIM_TRANSCRIPT
    return stt.SpeechEvent(type=kind, alternatives=[stt.SpeechData(language="ko", text=text, start_time=start,
                                                                   end_time=end)])


def heard(gate: SpeechGate, *parts: tuple[float, float]) -> None:
    for seconds, rms in parts:
        for frame in frames(seconds, rms):
            gate.hear(frame)


def test_the_ai_s_voice_coming_back_quietly_is_not_taken_as_the_user():
    dropped = []
    gate = SpeechGate(on_drop=lambda text, level: dropped.append((text, round(level))))
    heard(gate, (2.0, 2.0), (2.5, 60.0), (0.5, 2.0))  # silence, the faint echo, silence
    assert not gate.allows(transcript("확인해보세요."))
    assert dropped and dropped[0][0] == "확인해보세요." and 50 <= dropped[0][1] <= 70


def test_the_user_s_quietest_real_phrase_passes():
    gate = SpeechGate()
    heard(gate, (1.0, 2.0), (0.4, 1466.0), (0.7, 2.0))  # a short "좀", then the 0.7 s a final takes to arrive
    assert gate.allows(transcript("좀"))


def test_loud_speech_long_before_does_not_vouch_for_a_quiet_transcript_now():
    gate = SpeechGate()
    heard(gate, (2.0, 4000.0), (5.0, 2.0), (2.0, 60.0))
    assert not gate.allows(transcript("네"))


def test_a_long_phrase_is_judged_over_its_whole_length():
    # Speech for 6 s, then a quiet tail as long as the lookback: the phrase still passes, judged from its own start.
    gate = SpeechGate()
    heard(gate, (6.0, 3000.0), (3.5, 2.0))
    assert gate.allows(transcript("긴 이야기", start=0.0, end=6.0))
    assert not SpeechGate().allows(transcript("긴 이야기", start=0.0, end=6.0))  # nothing heard at all


def test_a_quiet_interim_is_dropped_without_a_report_and_other_events_pass():
    dropped = []
    gate = SpeechGate(on_drop=lambda text, level: dropped.append(text))
    heard(gate, (2.0, 60.0))
    assert not gate.allows(transcript("확인", final=False))
    assert gate.allows(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
    assert gate.allows(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
    assert gate.allows(transcript("   "))  # nothing to judge
    assert dropped == []


def test_listening_passes_every_frame_through_unchanged():
    gate = SpeechGate()
    original = frames(0.5, 1000.0)

    async def source():
        for frame in original:
            yield frame

    async def collect():
        return [frame async for frame in gate.listen(source())]

    passed = asyncio.run(collect())
    assert [bytes(f.data) for f in passed] == [bytes(f.data) for f in original]
    assert 900 <= gate.loudest(1.0) <= 1100


def test_the_call_ai_keeps_a_quiet_transcript_out_and_notes_it_in_the_call_record(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from livekit.agents import Agent

    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    quiet, loud = frames(2.0, 60.0), frames(1.0, 3000.0)

    async def recognizer(agent, audio, model_settings):
        # Stands in for Amazon Transcribe: it hears everything, and transcribes the echo too.
        count = 0
        async for _ in audio:
            count += 1
            if count == len(quiet):
                yield transcript("확인해보세요.")
            if count == len(quiet) + len(loud):
                yield transcript("네, 맞아요.")

    monkeypatch.setattr(Agent.default, "stt_node", recognizer)
    now = datetime(2026, 10, 15, 1, 23, tzinfo=ZoneInfo("Asia/Seoul"))
    rec = CallRecorder(tmp_path, room="r", now=now)
    agent = Coordinator(rec, SimpleNamespace(), tmp_path, clock=lambda: now)

    async def audio():
        for frame in quiet + loud:
            yield frame

    async def run():
        return [e async for e in agent.stt_node(audio(), None)]

    passed = asyncio.run(run())
    rec.close()
    assert [e.alternatives[0].text for e in passed] == ["네, 맞아요."]
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    dropped = [e for e in events if e["type"] == "transcript_dropped"]
    assert len(dropped) == 1 and dropped[0]["text"] == "확인해보세요." and dropped[0]["level"] < 100
    assert (rec.dir / "audio" / "user.wav").stat().st_size > 44  # the call's own recording still gets every frame


def test_the_wait_before_a_turn_ends_learns_the_caller_s_pauses(monkeypatch):
    # LiveKit fills unset keys from defaults that depend on the turn detector: for the cloud detector the server uses
    # (a streaming one) a fixed 0.3 s, which is what the 01:23 call of 2026-10-15 had. Ours: learned, from 0.3 s up to
    # 2.5 s (the user's choice, 2026-10-15, after the 14:22 call with 0.5-3.0 s: a median of 2.7 s from the end of
    # speech to the AI's voice, and 11 of 18 turns waiting at the cap, against 1.2 s with the fixed 0.3/2.5).
    from livekit.agents import inference
    from livekit.agents.voice.endpointing import DynamicEndpointing, create_endpointing
    from livekit.agents.voice.turn import _resolve_endpointing

    from voice.agent import TURN_ENDPOINTING

    monkeypatch.setenv("LIVEKIT_API_KEY", "test")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test")
    detector = inference.TurnDetector(version="v1")
    assert _resolve_endpointing(None, turn_detection=detector)["min_delay"] == 0.3  # LiveKit's own, before
    timer = create_endpointing(_resolve_endpointing(TURN_ENDPOINTING, turn_detection=detector))
    assert isinstance(timer, DynamicEndpointing)
    assert timer.min_delay == 0.3 and timer.max_delay == 2.5
