"""Ringing the caller hears while the AI gets ready to pick up (voice/prep.py).

It comes from the voice server as an audio track in the room, so the app needs no audio of its own: it plays
the room's audio as always. The app reports the call as connected only once the voice server sets
STATE_ATTRIBUTE to "answered", right before the AI's first words.
"""

import numpy as np
from livekit import rtc

STATE_ATTRIBUTE = "bee_call.state"
SAMPLE_RATE = 24000
FRAME_MS = 20
# 440 Hz and 480 Hz together, as in North American ringback, but on for 1 s and off for 2 s instead of
# 2 s and 4 s: the ring lasts only a few seconds, and a long silence would sound like a dead line.
TONE_S, PAUSE_S = 1.0, 2.0
LEVEL = 0.08  # each tone's amplitude, about -22 dBFS (arbitrary)
FADE_S = 0.01  # no clicks at the edges of the tone


def ring_cycle() -> list[rtc.AudioFrame]:
    """One ring and its pause, in frames."""
    t = np.arange(int(SAMPLE_RATE * TONE_S)) / SAMPLE_RATE
    tone = LEVEL * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t))
    fade = np.linspace(0.0, 1.0, int(SAMPLE_RATE * FADE_S))
    tone[:len(fade)] *= fade
    tone[-len(fade):] *= fade[::-1]
    pcm = (np.concatenate([tone, np.zeros(int(SAMPLE_RATE * PAUSE_S))]) * 32767).astype(np.int16)
    n = SAMPLE_RATE * FRAME_MS // 1000
    return [rtc.AudioFrame(pcm[i:i + n].tobytes(), SAMPLE_RATE, 1, n) for i in range(0, len(pcm) - n + 1, n)]


async def ring_back(room: rtc.Room) -> None:
    """Plays ringing into the room until cancelled, then takes the track away."""
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("ringback", source)
    publication = await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    frames = ring_cycle()
    try:
        while True:
            for frame in frames:
                await source.capture_frame(frame)  # waits while about a second of audio is queued
    finally:
        source.clear_queue()  # stop at once, not after the queued second
        await room.local_participant.unpublish_track(publication.sid)
        await source.aclose()
