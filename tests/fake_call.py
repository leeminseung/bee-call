"""A stand-in for LiveKit around the voice server's call (voice/agent.py run_call), so its decisions can be tested
with no network: the room and the job, the caller who joins and hangs up, and the session that would speak.

    call = FakeCall(people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)                # run_call's session and ringback are the fakes
    call.join()                                 # the caller is in the room
    await agent.run_call(call, people)          # the call, up to the AI's first words
    await call.end()                            # the job ends: LiveKit's shutdown callbacks run
"""

import asyncio
import contextvars
import json
from types import SimpleNamespace
from typing import Any


class FakeEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, callback=None):
        if callback is None:  # used as a decorator
            def register(handler):
                self.handlers.setdefault(event, []).append(handler)
                return handler
            return register
        self.handlers.setdefault(event, []).append(callback)
        return callback

    def emit(self, event: str, *args) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(*args)


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.attributes: dict[str, str] = {}

    async def set_attributes(self, attributes: dict[str, str]) -> None:
        self.attributes.update(attributes)


class FakeRoom(FakeEmitter):
    """Like the job's rtc.Room, it carries the room's name from the start: LiveKit names it from the job before the
    entrypoint runs (livekit/agents/ipc/job_proc_lazy_main.py), not when the job connects."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.local_participant = FakeLocalParticipant()


class FakeSession(FakeEmitter):
    """What run_call uses of LiveKit's AgentSession. Its voice starts `first_words_after` seconds after say()
    (None: never)."""

    def __init__(self, first_words_after: float | None = 0.05) -> None:
        super().__init__()
        self.first_words_after = first_words_after
        self.started: dict[str, Any] | None = None
        self.said: list[str] = []
        self.llm = None
        self.usage = SimpleNamespace(model_usage=[])
        self.closed = False

    async def start(self, **kwargs) -> None:
        self.started = kwargs

    def say(self, text: str) -> None:
        self.said.append(text)
        if self.first_words_after is not None:
            asyncio.get_running_loop().call_later(
                self.first_words_after, self.emit, "agent_state_changed",
                SimpleNamespace(old_state="thinking", new_state="speaking"))

    def generate_reply(self, **kwargs) -> None:
        pass

    def shutdown(self, drain: bool = False) -> None:
        self.closed = True


class FakeCall:
    """One dispatched call: the job's context, the room, the caller and the session."""

    def __init__(self, people, meta: dict[str, Any], room: str = "call-matt-20261015-090000",
                 session: FakeSession | None = None) -> None:
        self.people = people
        self.room_name = room
        self.room = FakeRoom(room)
        self.session = session or FakeSession()
        self.caller = SimpleNamespace(identity=meta.get("person") or "caller", kind=0)
        self.joined = asyncio.Event()
        self.shutdown_callbacks: list = []
        self.shutdown_reason: str | None = None
        self.deleted: str | None = None  # the room deleted, by name
        self.recording: dict | None = None  # what the job asked LiveKit's cloud recording to keep (init_recording)
        self.ringing: list[str] = []  # "start", "stop" as the voice server's ringback plays and stops
        self.job = SimpleNamespace(metadata=json.dumps(meta), room=SimpleNamespace(name=room))
        self.proc = SimpleNamespace(userdata={"vad": None, "aws": None})

    # ---------- JobContext ----------

    async def connect(self) -> None:
        pass

    async def wait_for_participant(self):
        await self.joined.wait()
        return self.caller

    def init_recording(self, options) -> None:
        if self.recording is None:  # as LiveKit's: the first call decides
            self.recording = dict(options)

    def is_fake_job(self) -> bool:
        """LiveKit's console mode (no room, this Mac's mic) is a fake job; a dispatched call is not."""
        return False

    def add_shutdown_callback(self, callback) -> None:
        self.shutdown_callbacks.append(callback)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reason = reason

    def delete_room(self, room_name: str | None = None):
        """As JobContext.delete_room: the room named, or the job's room."""
        self.deleted = room_name or self.room.name
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    # ---------- the test's side ----------

    def join(self) -> None:
        """The caller (the app) is in the room: it started the call, or answered the AI's."""
        self.joined.set()

    def hang_up(self) -> None:
        self.room.emit("participant_disconnected", self.caller)

    async def ring_back(self, room) -> None:
        """voice/ringback.py ring_back, without audio."""
        self.ringing.append("start")
        try:
            await asyncio.Event().wait()
        finally:
            self.ringing.append("stop")

    async def end(self) -> None:
        """The job ends, as LiveKit ends it: every shutdown callback runs, each as a task the job itself starts, so
        it does not see what the call set in its own context (whose Bee login, for one)."""
        for callback in self.shutdown_callbacks:
            await asyncio.get_running_loop().create_task(callback(), context=contextvars.Context())


def use_fakes(monkeypatch, call: FakeCall) -> None:
    """Points voice/agent.py at the fakes of this call."""
    from voice import agent

    monkeypatch.setattr(agent, "make_session", lambda ctx, language, voice, rec: call.session)
    monkeypatch.setattr(agent, "ring_back", call.ring_back)
