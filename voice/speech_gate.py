"""Keeps speech recognition results that were too quiet to be the user out of the conversation.

On 2026-10-15 a call over AirPods let the AI's own voice back into the microphone for about two seconds, some
35 dB below the user's speech, and Amazon Transcribe still heard words in it: "확인해보세요" and "네" were taken
as the user's and the AI answered them. ElevenLabs Scribe, run on the same two seconds of microphone audio,
heard the AI's sentence there. In that call the loudest 0.1 s of the microphone in the 3 s before each of the
user's 86 real phrases was 1,466 to 13,363 (16-bit RMS); before the two phantom phrases it was 69 and 64.

So a transcript whose audio never got louder than MIN_SPEECH_RMS is not passed on: the AI neither hears it nor
gets interrupted by it, and the app shows no caption for it. A dropped final transcript is reported with its
level (the call record keeps it), so the level can be checked again as calls accumulate.
"""

from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable

import numpy as np
from livekit import rtc
from livekit.agents import stt

MIN_SPEECH_RMS = 300.0  # between the measured 69 (the AI's voice coming back) and 1,466 (the quietest real phrase)
LOOKBACK_S = 3.0  # a transcript's audio is looked for this far back; finals arrived about 0.7 s after the speech
WINDOW_S = 0.1  # loudness is measured over windows this long, as in the measurement above
KEEP_S = 30.0  # windows older than this are forgotten
TRANSCRIPTS = (stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT)


class SpeechGate:
    """Hears the audio on its way to speech recognition, then judges each transcript that comes back."""

    def __init__(self, on_drop: Callable[[str, float], None] | None = None,
                 min_rms: float = MIN_SPEECH_RMS, lookback_s: float = LOOKBACK_S) -> None:
        self._on_drop = on_drop
        self._min_rms = min_rms
        self._lookback_s = lookback_s
        self._windows: deque[tuple[float, float]] = deque()  # (stream time at the window's end, its RMS)
        self._now = 0.0  # seconds of audio heard so far
        self._squares = 0.0  # the window being filled: sum of squared samples, sample count, length in seconds
        self._count = 0
        self._filled = 0.0

    async def listen(self, audio: AsyncIterable[rtc.AudioFrame]) -> AsyncIterator[rtc.AudioFrame]:
        async for frame in audio:
            self.hear(frame)
            yield frame

    def hear(self, frame: rtc.AudioFrame) -> None:
        samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float64)
        if not samples.size or not frame.sample_rate:
            return
        self._squares += float(np.dot(samples, samples))
        self._count += samples.size
        seconds = frame.samples_per_channel / frame.sample_rate
        self._filled += seconds
        self._now += seconds
        if self._filled >= WINDOW_S:
            self._windows.append((self._now, (self._squares / self._count) ** 0.5))
            self._squares, self._count, self._filled = 0.0, 0, 0.0
            while self._windows and self._windows[0][0] < self._now - KEEP_S:
                self._windows.popleft()

    def loudest(self, seconds: float) -> float:
        """The loudest window among those that ended in the last `seconds` of audio, the one still filling
        included."""
        levels = [rms for end, rms in self._windows if end >= self._now - seconds]
        if self._count:
            levels.append((self._squares / self._count) ** 0.5)
        return max(levels, default=0.0)

    def allows(self, event: stt.SpeechEvent) -> bool:
        """False for a transcript whose audio stayed below MIN_SPEECH_RMS; every other event passes."""
        if event.type not in TRANSCRIPTS or not event.alternatives:
            return True
        best = event.alternatives[0]
        text = best.text.strip()
        if not text:
            return True
        lookback = self._lookback_s
        if best.end_time > best.start_time:  # a long phrase: look back over all of it
            lookback = max(lookback, best.end_time - best.start_time + 2.0)
        level = self.loudest(lookback)
        if level >= self._min_rms:
            return True
        if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and self._on_drop is not None:
            self._on_drop(text, level)
        return False
