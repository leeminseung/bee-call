"""Writes one call's raw record into the session folder on this Mac.

workspace/<date>/<time>/
  transcript.md   what both sides said, with times and interruptions
  events.jsonl    everything that happened, one JSON object per line
  model-log.jsonl every request the call AI's model received, whatever the model, one line each: the whole chat
                  (instructions, both sides, tool calls and their results), the tools offered, and what came back.
                  For debugging; kept on the server only (it holds Bee data), never served (voice/session_server.py)
  audio/user.wav  the caller's microphone audio as received
  audio/ai.wav    the AI's synthesized speech, utterances back to back

Files are append-only and only the voice server writes them. The audio files are
written only when the person keeps call audio on the server (voice/people.py
record_on_server, on by default); the record and transcript are always written.

A call writes lines only when something happens, and a record that shows no life for 120 s reads as dropped
(voice/call_view.py, contract section 1). So while a call runs its record's file time is refreshed every REFRESH_S
(keep_fresh), and a quiet stretch never reads as dropped.
"""

import asyncio
import json
import logging
import os
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from livekit import rtc

log = logging.getLogger("voice.recorder")


class WavSink:
    def __init__(self, path: Path | None) -> None:
        self._path = path  # None: audio is not kept for this call
        self._wav: wave.Wave_write | None = None
        self._format: tuple[int, int] | None = None
        self.skipped_frames = 0

    def write(self, frame: rtc.AudioFrame) -> None:
        if self._path is None:
            return
        fmt = (frame.sample_rate, frame.num_channels)
        if self._wav is None:
            self._wav = wave.open(str(self._path), "wb")
            self._wav.setnchannels(frame.num_channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(frame.sample_rate)
            self._format = fmt
        if fmt != self._format:
            self.skipped_frames += 1
            return
        self._wav.writeframes(bytes(frame.data))

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


MODEL_LOG = "model-log.jsonl"
REFRESH_S = 30.0  # how often a running call's record shows life without a line (a quarter of call_view.LIVE_S)


class CallRecorder:
    def __init__(self, workspace: Path, room: str, keep_audio: bool = True, *, now: datetime) -> None:
        """`now`: when the call started, on the person's clock (Person.now()); the folder is named by it."""
        base = workspace / now.strftime("%Y-%m-%d") / now.strftime("%H%M")
        self.dir = base
        n = 2
        while self.dir.exists():
            self.dir = base.with_name(f"{base.name}-{n}")
            n += 1
        self.dir.mkdir(parents=True)
        if keep_audio:
            (self.dir / "audio").mkdir()
        self._t0 = time.monotonic()
        self._events = (self.dir / "events.jsonl").open("a")
        self._transcript = (self.dir / "transcript.md").open("a")
        self._transcript.write(f"# Call {now:%Y-%m-%d %H:%M} (room {room})\n\n")
        self.user_audio = WavSink(self.dir / "audio" / "user.wav" if keep_audio else None)
        self.ai_audio = WavSink(self.dir / "audio" / "ai.wav" if keep_audio else None)
        self.event("call_started", room=room, started_at=now.isoformat(timespec="seconds"))

    def refresh(self) -> None:
        """The record shows life without a line: its file's time becomes now."""
        try:
            os.utime(self.dir / "events.jsonl")
        except OSError as exc:  # the call goes on; the record may read as dropped a little early
            log.warning("could not refresh the record of %s: %s", self.dir, exc)

    async def keep_fresh(self) -> None:
        """Refreshes the record every REFRESH_S until it is closed (or this is cancelled)."""
        while not self._events.closed:
            await asyncio.sleep(REFRESH_S)
            if not self._events.closed:
                self.refresh()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def event(self, event_type: str, /, **data: Any) -> None:
        # Positional-only, so any field name (name, kind, type...) can go in **data.
        self._events.write(json.dumps({"t": self.elapsed(), **data, "type": event_type}, default=str) + "\n")
        self._events.flush()

    def line(self, role: str, text: str, interrupted: bool = False) -> None:
        minutes, seconds = divmod(self.elapsed(), 60)
        stamp = f"{int(minutes):02d}:{seconds:04.1f}"
        note = " (interrupted)" if interrupted else ""
        self._transcript.write(f"- {stamp} **{role}:** {text}{note}\n")
        self._transcript.flush()

    def close(self) -> None:
        self.event(
            "call_ended",
            user_audio_skipped_frames=self.user_audio.skipped_frames,
            ai_audio_skipped_frames=self.ai_audio.skipped_frames,
        )
        self.user_audio.close()
        self.ai_audio.close()
        self._events.close()
        self._transcript.close()

    def model_request(self, entry: dict[str, Any]) -> None:
        """One request the call AI's model received and what came back (MODEL_LOG), with the call's time. Never
        raises: the log must not break a call (a call left before the AI spoke has no folder any more)."""
        line = {"t": round(time.monotonic() - self._t0, 3), "at": datetime.now().astimezone().isoformat(), **entry}
        try:
            with (self.dir / MODEL_LOG).open("a") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not keep a model request of %s: %s", self.dir, exc)

    @property
    def closed(self) -> bool:
        """The call's end is on record (call_ended); later events go after it (after_call)."""
        return self._events.closed

    def event_or_after(self, event_type: str, /, **data: Any) -> None:
        """An event of something that may outlive the call (a background job): in the record while the call is on,
        after call_ended once it is over."""
        (self.after_call if self.closed else self.event)(event_type, **data)

    def after_call(self, event_type: str, /, **data: Any) -> None:
        """An event that comes after call_ended (Bee writes tried again once the call is over, a job's end)."""
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"t": self.elapsed(), **data, "type": event_type}, default=str) + "\n")
