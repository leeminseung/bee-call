"""bee-call voice server: LiveKit Agents on this Mac.

Audio comes in from a LiveKit Cloud room, Amazon Transcribe (Sydney) turns it
into words, the coordinator answers, and Amazon Polly (Sydney) speaks. Every
call is recorded into its session folder by CallRecorder, the Bee context for
the call is saved into inputs/, and delegated jobs run in the background.

COORDINATOR_LLM picks the model in the coordinator slot:
  anthropic Claude on Anthropic's own API (the server's default from 2026-10-15), with the key in
            ANTHROPIC_API_KEY: the person's call_model setting picks Claude Haiku 5.5 (the default) or Claude
            Sonnet 5.5 and their settings (voice/call_models.py). GLM 5 on Bedrock stands by: without a key, or
            when the first request of a call fails, GLM 5 takes the call, and the record's coordinator_model event
            says which model answered and why (voice/fallback_llm.py).
  claude    Claude on Bedrock through the same adapter: Claude Sonnet 5.5, or the model in COORDINATOR_MODEL
            (Claude is blocked on this AWS account for now)
  fake      the Claude adapter with scripted replies and no network (test stand-in
            that checks every request the way the Messages API would)
  bedrock   any other Bedrock model through the Converse API, named by COORDINATOR_MODEL (GLM 5 when unset;
            probes/coordinator_bench.py compares them)
  nova      Amazon Nova 2 Lite on Bedrock (test stand-in that can call tools)
  echo      repeats the caller (test stand-in for the audio path)
With anthropic, the prep runs on Claude Haiku 5.5 and the meta agent on Claude Sonnet 5.5, each with GLM 5 standing
by (the model catalog, voice/model_catalog.py); the other modes run them on one Bedrock model, as mode_role says.
WORKER picks what runs delegated jobs:
  fake     pretends to work for a few seconds (test stand-in, the default)
  search   one Nova Web Grounding search per job, with sources (test stand-in, costs about 0.03 USD a job)
  harness  the person's worker_model (the server's): sonnet is Claude Sonnet 5.5 on Anthropic's API with its web
           search, page reading and code execution (worker/claude.py), glm is GLM 5 on the AgentCore Harness worker
           in Sydney (deploy it first: scripts/deploy_worker.py); one not set up on this machine hands the jobs to
           the other (job_worker)

The server joins only rooms it is dispatched to, as the agent named AGENT_NAME:
the app's token dispatches it for a call the user starts, and voice/dispatch.py
for a call the AI starts. The dispatch metadata says which, and for an AI call
why it is calling; the AI then waits for the phone to answer before speaking.
It also names the person (voice/people.py): the call is recorded in their folder,
in their language and voice, and reads only their Bee.

Run: uv run python -m voice.agent dev       (joins rooms on LiveKit Cloud)
     uv run python -m voice.agent console   (this Mac's mic and speakers)
"""

import asyncio
import itertools
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import boto3
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (AgentServer, AgentSession, JobContext, JobProcess, RecordingOptions, cli, inference, llm,
                            room_io)
from livekit.plugins import aws, silero
from livekit.agents.types import NOT_GIVEN
from livekit.plugins.aws.stt import Credentials

from beelink import cli as bee
from beelink.calls import OurCalls
from beelink.context import CallInputs, fetch_call_inputs, save_call_inputs
from beelink.writer import BeeWrites, event_fields, summary_notification
from voice.aws_session import aws_profile, aws_session
from voice import model_catalog, providers
from voice.call_models import CallModel, call_model_for, coordinator_llm
from voice.claude_llm import ClaudeBedrockLLM, default_model
from voice.continuation import earlier_call, last_calls, record_id
from voice.costs import SpendLog, day_budget
from meta.session import add_event, meta_notes
from voice import voip_push
from voice.coordinator import BeeCoordinator, Coordinator
from voice.dispatch import AGENT_NAME, RING_SECONDS, declined
from voice.echo_llm import EchoLLM
from voice.fake_claude import FakeAnthropic
from voice.fallback_llm import FirstRequestFallback
from voice.language import CallLanguage, call_language
from voice.people import People, Person, Settings, data_root, dev_person
from voice.prep import INBOUND_READ_TIMEOUT_S, OPENING_BY_S, Prep, Preparation, call_brief, prep_role
from voice.recorder import CallRecorder
from voice.titles import title_call
from voice.ringback import STATE_ATTRIBUTE, ring_back
from voice.voices import make_tts, voice_choice
from worker.fake import FakeWorker
from worker.runner import (RESTARTED, JobNotification, JobRunner, NoWorker, StandingBy, fail_jobs_left_running,
                           keep_sweeping, short)
from worker.search import SearchWorker

load_dotenv(".env.local")
PROFILE = aws_profile()  # None on EC2: the instance role
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", REGION)
COORDINATOR_LLM = coordinator_llm()
# COORDINATOR_LLM=bedrock runs COORDINATOR_MODEL (GLM 5 when unset; model_catalog.mode_role). GLM 5 speaks while it
# calls a tool. MiniMax M2.5 scored best in probes/coordinator_bench.py (2026-10-12) but calls tools without a word,
# and in the first real call (2026-10-13) the caller heard silence for 23-28 s whenever they kept talking meanwhile.
WORKER = os.environ.get("WORKER", "fake")
# A call the user started rings until the AI's voice starts, and at most this long after the caller is in the
# room: the opening is ready within OPENING_BY_S (6 s) and the voice takes about 2 s more (BC-025).
RINGING_AT_MOST_S = 8.0
# The ways of leaving a room that LiveKit ends the session for (livekit/agents/voice/room_io/types.py
# DEFAULT_CLOSE_ON_DISCONNECT_REASONS): the caller hung up. Others (a lost network) it waits out.
HANG_UPS = (rtc.DisconnectReason.CLIENT_INITIATED, rtc.DisconnectReason.ROOM_DELETED,
            rtc.DisconnectReason.USER_REJECTED)
# How long a call the user started waits to see the caller in the room: as long as the app keeps ringing once it
# joined (ios CallManager.pickUpSeconds), so a phone slow to join is never given up while it still rings.
CALLER_WAIT_S = 30.0
DECLINE_CHECK_S = 1.0  # how often a ringing AI call looks whether the user declined it (arbitrary)
# After a call, its failed Bee writes are tried again for at most this long, after the call's own record is closed
# (arbitrary).
BEE_RETRY_S = 5.0
# A background job still running when its call ends runs to its end (2026-10-16), in the call's process, at most this
# long after the call; then it fails saying so. Its end lands in the call's record after call_ended, and the person
# gets one plain notification. LiveKit gives a job's process shutdown_process_timeout to end once it is asked to
# close (10 s by default, and LiveKit may ask as soon as the room is gone): the server's is set past this.
AFTER_CALL_JOBS_S = 30 * 60
# A result that came in the call's last seconds may not have been heard (it ended while the AI said goodbye, once):
# a job that ended this long before the call did is notified as well (arbitrary).
RECENT_RESULT_S = 20.0
# In the data root while the voice server is stopping (VoiceServer.drain: a deploy, a restart): a job still running
# after its call then stops at once, failing on record and notified, so the stop waits only for calls in progress
# (systemd gives it 10 minutes, and it takes no new call meanwhile). The next start clears it.
STOPPING = "voice-server-stopping"
STOP_CHECK_S = 1.0  # how often a call's process waiting on its jobs looks for it (arbitrary)
# Changed after each reconnect of the voice server's own room connection (it is "1" from the pick-up on): captions
# are not sent while it is away, though what it hears is still recorded, so the app stops matching caption lines to
# the call view's counts for that call (app contract section 3, rule 6).
CAPTIONS_ATTRIBUTE = "bee_call.captions"
# How long LiveKit waits after the caller stops before it ends their turn, learned during the call between these
# bounds (LiveKit 1.8.6 EndpointingOptions). The user's choice (2026-10-15): 0.5-3.0 s left most turns waiting at
# the cap, about 2.4 s; 0.3-2.5 s.
TURN_ENDPOINTING = {"mode": "dynamic", "min_delay": 0.3, "max_delay": 2.5}
NOISE_CANCELLATIONS = ("bvc", "nc", "off")


def noise_setting(env: Mapping[str, str] = os.environ) -> str:
    """NOISE_CANCELLATION: the model of LiveKit Cloud's enhanced noise cancellation (livekit-plugins-noise-cancellation,
    Krisp's models) that a call uses when the person's switch is on (setting noise_cancellation, on by default): bvc
    (also when unset) takes out other people's voices as well as noise, nc noise only; off turns it off for everyone,
    whatever the switches say. It works on the caller's audio before speech recognition, turn detection and
    interruptions hear it. A call's record keeps the audio the voice server heard, so audio/user.wav is the cleaned
    audio when it is on."""
    value = (env.get("NOISE_CANCELLATION") or "").strip().lower() or "bvc"
    if value not in NOISE_CANCELLATIONS:
        raise SystemExit(f"NOISE_CANCELLATION must be one of {', '.join(NOISE_CANCELLATIONS)}, not {value!r}")
    return value


# Replaying recorded calls through LiveKit Cloud (2026-10-16): about 20 ms later, the caller's words transcribed the
# same; BVC took out faint voices that were not the caller's, NC turned two sounds that had no words into "음". The
# user turned it on for everyone, with a switch for each person in the app (2026-10-16).
NOISE_CANCELLATION = noise_setting()


def noise_for(settings: Settings) -> str:
    """The noise filter a call uses, from the person's settings as the call starts: NOISE_CANCELLATION's model when
    their switch is on, else off; off for everyone when NOISE_CANCELLATION is off."""
    return NOISE_CANCELLATION if settings.noise_cancellation and NOISE_CANCELLATION != "off" else "off"

log = logging.getLogger("bee-call")


def transcribe_credentials(session: boto3.Session | None) -> Credentials | None:
    # The Transcribe plugin finds environment keys and an EC2 instance role by itself, afresh for every stream it
    # opens, but it does not read ~/.aws profiles. On EC2 (no session) it is left to do that, so even a stream
    # reopened late in a long call has current keys. With a profile (a Mac) it gets the profile's keys in memory,
    # as they are at the start of each call. Nothing is printed or written to disk.
    if session is None:
        return None
    found = session.get_credentials()
    if found is None:
        raise RuntimeError("no AWS credentials: set AWS_PROFILE, or run with an instance role")
    creds = found.get_frozen_credentials()
    return Credentials(access_key_id=creds.access_key, secret_access_key=creds.secret_key,
                       session_token=creds.token)


def ignored_settings() -> list[str]:
    """The environment settings that are set but change nothing: ELEVENLABS_VOICE (the voice catalog decides each
    person's voice, voice/voice_catalog.json), and with COORDINATOR_LLM=anthropic the other modes' model settings
    (voice/call_models.py alone decides them)."""
    names = ["ELEVENLABS_VOICE"]
    if COORDINATOR_LLM == "anthropic":
        names += ["COORDINATOR_MODEL", "COORDINATOR_THINKING", "COORDINATOR_EFFORT"]
    return sorted(name for name in names if os.environ.get(name, "").strip())


def coordinator_model(call_model: CallModel) -> str:
    """The model a call's AI starts with in this COORDINATOR_LLM mode, for the record (with anthropic, GLM 5 can
    still take the call: voice/fallback_llm.py)."""
    if COORDINATOR_LLM == "anthropic":
        return call_model.model
    if COORDINATOR_LLM == "claude":
        return os.environ.get("COORDINATOR_MODEL") or default_model(BEDROCK_REGION)
    if COORDINATOR_LLM in ("bedrock", "nova"):
        return model_catalog.mode_role("call", COORDINATOR_LLM).choices[0].offering.model
    return COORDINATOR_LLM


def make_llm(rec: CallRecorder, language: CallLanguage, call_model: CallModel) -> llm.LLM:
    if COORDINATOR_LLM == "anthropic":
        return call_ai_with_standby(rec, language, call_model)
    if COORDINATOR_LLM == "echo":
        return EchoLLM()
    if COORDINATOR_LLM in ("bedrock", "nova"):  # one Bedrock model, as the model catalog's mode_role names it
        choice = model_catalog.mode_role("call", COORDINATOR_LLM).choices[0]
        return providers.adapter(choice.offering.provider).call_llm(choice, on_event=rec.event,
                                                                     refusal_line=language.refusal)
    if COORDINATOR_LLM == "claude":
        return ClaudeBedrockLLM(profile=PROFILE, region=BEDROCK_REGION, on_event=rec.event)
    if COORDINATOR_LLM == "fake":
        # Roughly Claude's pace: first words after 0.7 s, then about 30 words a second.
        client = FakeAnthropic(first_delay=0.7, word_delay=0.03, log_path=rec.dir / "fake-claude.jsonl")
        return ClaudeBedrockLLM(client=client, on_event=rec.event)
    raise SystemExit(f"unknown COORDINATOR_LLM {COORDINATOR_LLM!r}")


def call_ai_with_standby(rec: CallRecorder, language: CallLanguage, call_model: CallModel) -> llm.LLM:
    """COORDINATOR_LLM=anthropic: the person's call model as the model catalog's call role runs it (Claude on
    Anthropic's own API), its standby (GLM 5 on Bedrock) answering when the call model's provider is not set up here
    or its first request fails. The record's coordinator_model event names the model that answered the call, and why
    it is not the person's when it is not."""
    first, standby = model_catalog.choice("call", call_model.name), model_catalog.role("call").standby

    def build(choice: model_catalog.Choice) -> llm.LLM:
        return providers.adapter(choice.offering.provider).call_llm(choice, on_event=rec.event,
                                                                     refusal_line=language.refusal)

    def chosen(model: llm.LLM, reason: str | None) -> None:
        rec.event("coordinator_model", model=model.model, provider=model.provider,
                  **({"instead_of": first.offering.model, "reason": reason} if reason else {}))

    problem = providers.adapter(first.offering.provider).setup_problem()
    if problem:
        model = build(standby)
        chosen(model, problem)
        return model
    return FirstRequestFallback(build(first), lambda: build(standby), on_choice=chosen)


def as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


class TurnTiming:
    """Where the time of each user turn went, as one turn_timing event in the call record: when the user's speech
    ended, how long the transcript and the end-of-turn decision took, the model's first token, the voice's first
    audio and the total from the end of speech to it. From LiveKit's metrics on each message (MetricsReport): a user
    turn's own, and those of the reply that follows it; a turn the next user turn came before any reply to has
    reply None. Never raises into the call."""

    USER = {"user_started_at": "started_speaking_at", "user_stopped_at": "stopped_speaking_at",
            "transcription_delay": "transcription_delay", "end_of_turn_delay": "end_of_turn_delay",
            "on_user_turn_completed_delay": "on_user_turn_completed_delay"}
    REPLY = {"llm_ttft": "llm_node_ttft", "llm_ttfs": "llm_node_ttfs", "tts_ttfb": "tts_node_ttfb",
             "ai_started_at": "started_speaking_at", "speech_end_to_first_audio": "e2e_latency"}

    def __init__(self, rec: CallRecorder) -> None:
        self.rec = rec
        self.waiting: list[dict] = []  # user turns not answered yet

    def user(self, metrics) -> None:
        self.waiting.append(metrics if isinstance(metrics, dict) else {})

    def reply(self, metrics) -> None:
        if not self.waiting:
            return
        *unanswered, answered = self.waiting
        self.waiting = []
        for user in unanswered:
            self._write(user, None)
        self._write(answered, metrics if isinstance(metrics, dict) else {})

    def flush(self) -> None:
        for user in self.waiting:
            self._write(user, None)
        self.waiting = []

    def _write(self, user: dict, reply: dict | None) -> None:
        try:
            fields = {name: number_or_none(user.get(key)) for name, key in self.USER.items()}
            if reply is None:
                fields["reply"] = None
            else:
                fields |= {name: number_or_none(reply.get(key)) for name, key in self.REPLY.items()}
                if fields["speech_end_to_first_audio"] is None and None not in (fields["ai_started_at"],
                                                                                 fields["user_stopped_at"]):
                    fields["speech_end_to_first_audio"] = round(fields["ai_started_at"] - fields["user_stopped_at"], 3)
            self.rec.event("turn_timing", **fields)
        except Exception:
            log.exception("could not record a turn's timing")


def number_or_none(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def wire_events(session: AgentSession, rec: CallRecorder) -> None:
    # LiveKit calls these handlers inside its reply pipeline; an exception here would
    # cut the pipeline (a missing reply), so every handler is wrapped.
    timing = TurnTiming(rec)

    def on(event_name: str):
        def register(handler):
            def safe(ev) -> None:
                try:
                    handler(ev)
                except Exception as exc:
                    log.exception("event handler for %s failed", event_name)
                    try:
                        rec.event("error", source=f"handler:{event_name}", error=str(exc))
                    except Exception:
                        pass

            session.on(event_name, safe)
            return handler

        return register

    @on("user_input_transcribed")
    def _(ev) -> None:
        if ev.is_final:
            rec.event("user_transcript", text=ev.transcript)

    @on("conversation_item_added")
    def _(ev) -> None:
        item = ev.item
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            rec.event("item", kind=type(item).__name__)
            return
        text = item.text_content or ""
        interrupted = bool(getattr(item, "interrupted", False))
        metrics = as_dict(item.metrics) if getattr(item, "metrics", None) else None
        rec.event("message", role=role, text=text, interrupted=interrupted, metrics=metrics)
        rec.line("User" if role == "user" else "AI", text, interrupted)
        (timing.user if role == "user" else timing.reply)(metrics)

    @on("function_tools_executed")
    def _(ev) -> None:
        for call, output in zip(ev.function_calls, ev.function_call_outputs):
            rec.event("tool", name=call.name, arguments=call.arguments,
                      output=getattr(output, "output", None) if output else None)

    @on("agent_state_changed")
    def _(ev) -> None:
        rec.event("agent_state", old=ev.old_state, new=ev.new_state)

    @on("user_state_changed")
    def _(ev) -> None:
        rec.event("user_state", old=ev.old_state, new=ev.new_state)

    @on("agent_false_interruption")
    def _(ev) -> None:
        rec.event("false_interruption")

    @on("error")
    def _(ev) -> None:
        rec.event("error", source=type(getattr(ev, "source", None)).__name__, error=str(getattr(ev, "error", ev)))

    @on("close")
    def _(ev) -> None:
        timing.flush()
        rec.event("session_closed", reason=str(getattr(ev, "reason", "")))


def person_for_call(meta: dict, people: People) -> str | None:
    """Whose call this is: the person in the dispatch metadata, or on a Mac the dev person for a call without one
    (console, test callers). None when that is no approved person: the call is not answered."""
    person = meta.get("person") or dev_person()
    return person if isinstance(person, str) and people.known(person) else None


def session_usage(session: AgentSession) -> list[dict] | None:
    """The models LiveKit saw the call use (speech recognition, the model, the voice), for the daily budget."""
    try:
        return [entry.model_dump() for entry in session.usage.model_usage]
    except Exception as exc:  # the budget then counts the call by its minutes
        log.warning("no usage for this call: %s", exc)
        return None


def write_usage(rec: CallRecorder, session: AgentSession) -> None:
    """The call's minutes and model use, for the person's daily budget (voice/costs.py)."""
    models = session_usage(session)
    if models is not None:
        (rec.dir / "usage.json").write_text(json.dumps({"minutes": round(rec.elapsed() / 60, 2), "models": models},
                                                       default=str))


async def wait_for_answer(ctx: JobContext, workspace: Path) -> str:
    """The AI's call rings: "answered" when the phone joins, "declined" when the user declined it in the app
    (voice/dispatch.py decline_ring), "missed" after RING_SECONDS, or when the room went away first (LiveKit's
    wait_for_participant raises then)."""
    joined = asyncio.ensure_future(ctx.wait_for_participant())
    deadline = time.monotonic() + RING_SECONDS
    try:
        while not joined.done():
            if declined(workspace, ctx.room.name):
                return "declined"
            left = deadline - time.monotonic()
            if left <= 0:
                return "missed"
            await asyncio.wait({joined}, timeout=min(DECLINE_CHECK_S, left))
        if joined.cancelled() or joined.exception() is not None:
            return "declined" if declined(workspace, ctx.room.name) else "missed"
        return "answered"
    finally:
        joined.cancel()


async def after_bee_writes(rec: CallRecorder, person, language: CallLanguage) -> None:
    """After a call, once its record is closed: its failed Bee writes are tried once more, then one notification
    says what went into Bee (contract sections 5 and 6)."""
    writes = BeeWrites(rec.dir)
    try:
        await asyncio.wait_for(writes.retry_failed(
            done=lambda write: rec.after_call("bee_write", **event_fields(write), retried=True)), BEE_RETRY_S)
    except asyncio.TimeoutError:
        log.warning("retrying the Bee writes of %s took too long", rec.dir)
    except Exception:  # what was written stays in bee-writes.jsonl for the writer command
        log.exception("retrying the Bee writes of %s failed", rec.dir)
    payload = summary_notification(writes.all(), language)
    if payload is not None:
        try:
            await voip_push.alert(payload, person, collapse_id=f"bee-{rec.dir.parent.name}-{rec.dir.name}")
        except Exception as exc:
            log.warning("the Bee summary after %s did not go out: %s", rec.dir, exc)


def the_caller(participant) -> bool:
    """Anyone in a call's room but a voice server is the person on the phone."""
    return getattr(participant, "kind", None) != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT


def call_meta(raw: str | None) -> dict:
    try:
        meta = json.loads(raw) if raw else {}
    except ValueError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def greeting(meta: dict, language: CallLanguage) -> str:
    """The first line when there is no prepared opening."""
    lines = language.greetings
    if COORDINATOR_LLM == "echo":
        return lines["echo"]
    if meta.get("earlier_call"):
        return lines["continue"]
    if meta.get("direction") == "outbound":
        if meta.get("kind") == "voice note":
            return lines["voice note"]
        if meta.get("topic"):  # "your conversation" only when a Bee conversation is behind the call
            return lines["topic" if meta.get("kind") == "conversation" else "about"].format(topic=meta["topic"])
        return lines["outbound"]
    return lines["inbound"]


def read_already(task: asyncio.Task) -> CallInputs | None:
    """The task's Bee inputs if it has read them already; None while it is still reading (or it failed)."""
    if task.done() and not task.cancelled() and task.exception() is None:
        return task.result()
    return None


async def brief_once_read(task: asyncio.Task, meta: dict, zone=None) -> str | None:
    """The plain Bee context, once the task has read Bee (None if the read failed)."""
    try:
        return call_brief(await asyncio.shield(task), meta, zone)
    except Exception:
        return None


def prewarm(proc: JobProcess) -> None:
    # Done once per worker process, before any call, so calls start without a stall.
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["aws"] = aws_session() if PROFILE else None  # none on EC2, see transcribe_credentials
    if proc.userdata["aws"] is not None:
        proc.userdata["aws"].get_credentials()  # read the profile now rather than at the first call


class VoiceServer(AgentServer):
    """LiveKit's worker, marking the data root as it starts to stop (STOPPING)."""

    async def drain(self, timeout: Any = NOT_GIVEN) -> None:
        try:
            data_root().mkdir(parents=True, exist_ok=True)
            (data_root() / STOPPING).write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
        except OSError as exc:  # the jobs after calls then run on until systemd's limit, as before
            log.warning("could not mark the voice server as stopping: %s", exc)
        await super().drain(timeout)


server = VoiceServer(setup_fnc=prewarm, num_idle_processes=1, shutdown_process_timeout=AFTER_CALL_JOBS_S + 60)


def noise_filter(setting: str) -> rtc.NoiseCancellationOptions:
    """The noise cancellation model of a setting, bvc or nc. Importing the plugin loads its filter into LiveKit's
    native layer (about 0.1 s), once in a process."""
    from livekit.plugins import noise_cancellation

    return noise_cancellation.BVC() if setting == "bvc" else noise_cancellation.NC()


def noise_problem(setting: str) -> str | None:
    """Why the setting's filter cannot be used here, or None (off always can)."""
    if setting == "off":
        return None
    try:
        noise_filter(setting)
    except Exception as exc:  # a call is answered without it rather than not at all
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


if NOISE_CANCELLATION != "off" and (_problem := noise_problem(NOISE_CANCELLATION)):  # loaded here, not in a call
    log.error("NOISE_CANCELLATION=%s cannot be used (%s); calls run without it", NOISE_CANCELLATION, _problem)


def caller_audio(setting: str) -> room_io.RoomOptions:
    """What the call's session hears of the caller (noise_for): LiveKit's room input, through noise cancellation
    when it is on and its filter loads (else without it). LiveKit's gain control stays on either way,
    after the noise cancellation: LiveKit turns it off by default when noise cancellation is set (livekit/agents#7064,
    "the models manage input levels"), but Krisp's NC and BVC kept the levels as they came in a replay of our calls
    (RMS 1419 against 1340 and 1358), the speech gate's floor was measured with the gain on (voice/speech_gate.py),
    and a comparison in a real call should change one thing."""
    if setting == "off" or noise_problem(setting):
        return room_io.RoomOptions(audio_input=room_io.AudioInputOptions())
    return room_io.RoomOptions(audio_input=room_io.AudioInputOptions(noise_cancellation=noise_filter(setting),
                                                                      auto_gain_control=True))


def make_session(ctx: JobContext, language: CallLanguage, tts_voice, rec: CallRecorder,
                 call_model: CallModel) -> AgentSession:
    """The call's speech recognition, model, voice and turn-taking (tests put a stand-in here)."""
    # Never keys frozen at prewarm: an instance role's keys expire after a few hours.
    ctx.proc.userdata["transcribe_credentials"] = transcribe_credentials(ctx.proc.userdata["aws"])
    return AgentSession(
        stt=aws.STT(region=REGION, language=language.transcribe, credentials=ctx.proc.userdata["transcribe_credentials"]),
        llm=make_llm(rec, language, call_model),
        tts=make_tts(tts_voice, language, REGION),
        vad=ctx.proc.userdata["vad"],
        # Named rather than left to LiveKit's defaults, which follow the launch mode: `dev` (a Mac) picks LiveKit's
        # cloud turn detector and adaptive interruptions, `start` (the server) a local model and voice activity only.
        # The wait before a turn counts as over learns how long this caller pauses mid-sentence (TURN_ENDPOINTING:
        # 0.3-2.5 s): with LiveKit's fixed 0.3 s, the 2026-10-15 call cut one answer into five turns at pauses of
        # 1-1.9 s.
        turn_handling={"turn_detection": inference.TurnDetector(version="v1"), "interruption": {"mode": "adaptive"},
                       "endpointing": TURN_ENDPOINTING},
    )


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    await run_call(ctx, People())


@dataclass
class Caller:
    """Whose call it is and what their settings say, read once as the call starts: a call keeps its language, its
    call model and its voice to the end (BC-108)."""
    id: str
    someone: Person  # their folder, settings, phone and clock (someone.now)
    workspace: Path
    settings: Settings
    language: CallLanguage
    call_model: CallModel
    tts_voice: Any
    uses_bee: bool  # Use Bee on: the call reads only this person's Bee; off, nothing of Bee at all (BC-057)
    noise: str = "off"  # the noise filter on their audio (noise_for): bvc, nc or off


def job_worker(worker_model: str, mode: str | None = None) -> Any:
    """What runs a call's background jobs (WORKER, or `mode`): a test stand-in, or with harness the person's
    worker_model through its provider's adapter (voice/model_catalog.py). A choice whose provider is not set up on
    this machine hands the jobs to the role's next one that is; with none, each job fails saying why."""
    mode = WORKER if mode is None else mode
    if mode == "search":
        return SearchWorker()
    if mode != "harness":
        return FakeWorker()
    role = model_catalog.role("worker")
    first = model_catalog.choice("worker", worker_model)
    problems = []
    for choice in (first, *(c for c in role.choices if c is not first)):
        adapter = providers.adapter(choice.offering.provider)
        problem = adapter.setup_problem()
        if problem is None:
            if problems:
                log.warning("the worker %s cannot run here (%s); %s runs this call's jobs", first.id,
                            "; ".join(problems), choice.id)
            worker = adapter.worker(choice)
            standby = role.standby
            if standby is None or standby.offering == choice.offering:
                return worker
            backup = providers.adapter(standby.offering.provider)
            if backup.setup_problem() is not None:
                return worker
            return StandingBy(worker, lambda: backup.worker(standby), choice.name, standby.name)
        problems.append(f"{choice.id}: {problem}")
    log.error("no background worker can run here: %s", "; ".join(problems))
    return NoWorker("; ".join(problems))


def caller_for(meta: dict, people: People) -> Caller | None:
    """The person a call is for and their settings; None when that is no approved person (the call is not
    answered)."""
    person = person_for_call(meta, people)
    if person is None:
        return None
    settings = people.settings(person)
    language = call_language(settings.language)
    return Caller(person, people.person(person), people.workspace(person), settings, language,
                  call_model_for(settings), voice_choice(language, settings.voice),
                  settings.bee, noise_for(settings))


@dataclass
class Pickup:
    """What the AI picks up with: its prep and the Bee read, and for a call the user started, the ringing and the
    watch for a hang-up before the AI's first words."""
    prep: Prep
    inputs: CallInputs | None  # None while the Bee read is still going (an AI call answered at once)
    inputs_task: asyncio.Task | None = None
    preparation: Preparation | None = None
    ringing: asyncio.Task | None = None
    left: asyncio.Event | None = None  # set when the caller hung up before the AI's first words
    seen: float = 0.0  # time.monotonic() when the caller came into the room


async def answer_ai_call(ctx: JobContext, caller: Caller, meta: dict, inbox: Path | None,
                         conversation_ids: list[int], keep_warm: asyncio.Task | None) -> Pickup | None:
    """A call the AI places, prepared before the phone rang (voice/outbound.py): it rings until the phone answers.
    None when it was missed or declined, which the meta agent hears about."""
    # Bee is read again meanwhile, for the call's record.
    inputs_task = asyncio.create_task(fetch_call_inputs(inbox, conversation_ids, ours=OurCalls.of(caller.workspace),
                                                        memo_ids=meta.get("memo_ids") or []))
    answer = await wait_for_answer(ctx, caller.workspace)
    if answer != "answered":
        log.info("the call in room %s was %s", ctx.room.name, answer)
        add_event(caller.workspace, f"call_{answer}", now=caller.someone.now(), room=ctx.room.name,
                  topic=meta.get("topic"), reason=meta.get("reason"))
        inputs_task.cancel()
        if keep_warm:
            keep_warm.cancel()
        try:
            await ctx.delete_room()
        except Exception:
            log.exception("could not close room %s", ctx.room.name)
        return None
    prep = Prep.from_meta(meta.get("prep")) or Prep(error="the call was placed without a prep note")
    # The AI speaks as soon as the phone answers (BC-022). The read is for the call record, and for the plain Bee
    # context when the call has no prep note, which then reaches the AI once the read lands.
    return Pickup(prep, read_already(inputs_task), inputs_task)


async def ring_and_prepare(ctx: JobContext, caller: Caller, meta: dict, inbox: Path | None,
                           conversation_ids: list[int], keep_warm: asyncio.Task | None,
                           first_words: asyncio.Event) -> Pickup | None:
    """A call the user started: they hear ringing while the AI reads Bee and starts its prep. It picks up as soon as
    its opening is written; the note usually follows during the opening (voice/prep.py). None when the caller hung
    up or never came: the call then leaves nothing but its cost."""
    ringing = asyncio.create_task(ring_back(ctx.room))

    async def stop_ringing() -> None:  # if the job ends before the AI picks up
        ringing.cancel()

    ctx.add_shutdown_callback(stop_ringing)
    preparation = None
    # The caller hung up before the AI's first words (BC-027). This is watched until the first words, also after
    # the ringing stops. A hang-up is a leave LiveKit itself ends the session for; a phone that lost the network is
    # not one, and a caller who comes back is no longer gone.
    left = asyncio.Event()

    def caller_left(participant) -> None:
        if the_caller(participant) and not first_words.is_set() \
                and getattr(participant, "disconnect_reason", None) in HANG_UPS:
            left.set()

    ctx.room.on("participant_disconnected", caller_left)
    ctx.room.on("participant_connected", lambda participant: the_caller(participant) and left.clear())
    never_came = None  # why the caller is gone without having been seen, for the log
    try:
        if not ctx.is_fake_job():  # LiveKit's console mode has no caller in a room to wait for
            await asyncio.wait_for(ctx.wait_for_participant(), CALLER_WAIT_S)
        seen = time.monotonic()  # the opening's 6 s and the ringing's 8 s count from here (BC-025)
    except asyncio.TimeoutError:
        seen = time.monotonic()
        # The app's join made this room, so a caller not in it by now left before the voice server came, and no
        # disconnect will be seen.
        never_came = f"no caller came into the room within {CALLER_WAIT_S:g} s"
        left.set()
    except Exception as exc:  # LiveKit's wait raises when the room goes away meanwhile
        seen = time.monotonic()
        never_came = f"the room closed before the caller came into it ({exc})"
        left.set()

    async def get_ready() -> CallInputs:
        nonlocal preparation
        ours = OurCalls.of(caller.workspace)  # what Bee made of earlier calls is not new context (BC-060)
        got = await fetch_call_inputs(inbox, conversation_ids, timeout=INBOUND_READ_TIMEOUT_S, ours=ours)
        preparation = Preparation(got, meta, prep_role(COORDINATOR_LLM), spend=SpendLog(caller.workspace),
                                  bee=caller.uses_bee, now=caller.someone.now(), language=caller.language,
                                  ours=ours).start()
        await preparation.opening(timeout=OPENING_BY_S - (time.monotonic() - seen))
        return got

    ready = None
    if not left.is_set():  # a caller already gone gets no prep at all
        ready, hung_up = asyncio.create_task(get_ready()), asyncio.create_task(left.wait())
        await asyncio.wait({ready, hung_up}, return_when=asyncio.FIRST_COMPLETED)
        hung_up.cancel()
    if left.is_set():  # also when the prep was ready at the same moment
        why = never_came or "the caller hung up while it rang"
        log.info("%s, in room %s", why, ctx.room.name)
        for task in (ready, ringing, keep_warm):
            if task:
                task.cancel()
        if preparation is not None:
            preparation.cancel()
            await asyncio.gather(preparation.done, return_exceptions=True)  # its cost is recorded by then
        await asyncio.gather(*[task for task in (ready, ringing) if task], return_exceptions=True)
        ctx.shutdown(reason=why)
        return None
    return Pickup(preparation.prep, ready.result(), preparation=preparation, ringing=ringing, left=left, seen=seen)


def open_record(ctx: JobContext, people: People, caller: Caller, meta: dict) -> tuple[CallRecorder, Settings]:
    """The call's record, with what the call runs on. The phone sends the person's settings as it answers (contract
    section 1): whether audio is kept is read now, and those settings are returned."""
    settings = people.settings(caller.id)
    rec = CallRecorder(caller.workspace, ctx.job.room.name, keep_audio=settings.record_on_server,
                       now=caller.someone.now())
    unused = ignored_settings()
    if unused:
        log.warning("%s set but not used: a call's voice comes from the voice catalog, and with "
                    "COORDINATOR_LLM=anthropic its model's settings from voice/call_models.py", ", ".join(unused))
    noise = noise_problem(caller.noise)  # the filter the call uses: none when it cannot load
    rec.event("coordinator", llm=COORDINATOR_LLM, model=coordinator_model(caller.call_model),
              call_model=caller.call_model.name, language=caller.language.code, voice=caller.tts_voice.describe(),
              person=caller.id, audio_on_server=settings.record_on_server, audio_on_phone=settings.record_on_phone,
              noise_cancellation="off" if noise else caller.noise,
              **({"settings_not_used": unused} if unused else {}))
    if noise:
        log.error("noise cancellation %s cannot be used (%s); the call runs without it", caller.noise, noise)
        rec.event("error", source="noise_cancellation", error=noise)
    rec.event("call", direction=meta.get("direction", "inbound"), topic=meta.get("topic"), reason=meta.get("reason"),
              kind=meta.get("kind"), continue_from=meta.get("continue_from") if meta.get("earlier_call") else None)
    return rec, settings


def keep_inputs(rec: CallRecorder, got: CallInputs, uses_bee: bool, zone) -> None:
    """The Bee read, kept in the call's inputs/ (and why the AI called, from it)."""
    if not uses_bee:
        return  # nothing was read from Bee, so there is nothing to keep
    try:
        card = save_call_inputs(rec.dir, got, zone)
    except Exception as exc:  # the call goes on without its Bee record
        rec.event("error", source="bee_context", error=f"saving the Bee inputs failed: {exc}")
        return
    if card.get("conversation_id") or card.get("pending_fact_ids"):
        rec.event("why_calling", **card)
    if got.errors:
        rec.event("error", source="bee_context", error="; ".join(got.errors))


async def keep_inputs_when_read(rec: CallRecorder, task: asyncio.Task, uses_bee: bool, zone) -> None:
    try:
        keep_inputs(rec, await task, uses_bee, zone)
    except Exception as exc:
        rec.event("error", source="bee_context", error=f"reading Bee failed: {exc}")


async def record_prep(rec: CallRecorder, prep: Prep, preparation: Preparation | None) -> None:
    """The prep in the record once it is done, and its note as prep.md (BC-031)."""
    if preparation is not None:
        await preparation.done
    rec.event("call_prep", seconds=prep.seconds, model=prep.model, standby_reason=prep.standby_reason,
              reads=prep.reads, bee=prep.bee, note=prep.note, opening=prep.opening, error=prep.error,
              input_tokens=prep.input_tokens, output_tokens=prep.output_tokens)
    if prep.note:
        (rec.dir / "prep.md").write_text(f"# Call prep note\n\n{prep.note}\n\nOpening: {prep.opening}\n")


@dataclass
class Call:
    """One call once its record is open: what its job news (tell_about_job) and its end (end_call) work with."""
    ctx: JobContext
    people: People
    caller: Caller
    meta: dict
    rec: CallRecorder
    session: AgentSession
    keep_warm: asyncio.Task | None = None
    fresh: asyncio.Task | None = None  # refreshes the record while the call runs (CallRecorder.keep_fresh)
    preparation: Preparation | None = None
    prep_recorder: asyncio.Task | None = None
    inputs_saver: asyncio.Task | None = None
    left: asyncio.Event | None = None
    jobs: JobRunner | None = None
    agent: Coordinator | None = None
    over: bool = False  # the call has ended: a job's news goes to the person's phone, not to the call AI
    ended_at: float | None = None  # when it ended (time.monotonic)
    unfinished: set[str] = field(default_factory=set)  # the jobs still running when it ended
    notified: set[str] = field(default_factory=set)  # jobs whose end went to the person's phone: one notification each

    def abandoned(self) -> bool:
        """The caller hung up before the AI's first words: the call leaves no record."""
        return self.left is not None and self.left.is_set()


async def tell_about_job(call: Call, news: JobNotification) -> None:
    if call.over or (call.agent is not None and call.agent.ending):  # the call is over or ending: no more speech,
        # and its end reaches the person's phone
        if news.status in ("completed", "failed"):
            await job_end_alert(call, news.job_id, done=news.status == "completed")
        return
    if COORDINATOR_LLM == "echo":
        if news.needs_reply:
            call.session.say(f"Sorry, background job {news.job_id} failed." if news.error else news.summary)
        return
    # The note stays in the agent's history, so it still reaches the model if this reply is cut off.
    if await call.agent.tell_job(news):
        call.session.generate_reply()


async def job_end_alert(call: Call, job_id: str, *, done: bool) -> None:
    """One plain notification for a job that ended after its call: "Results are in: <title>", or that it could not
    finish, in the call's language. Tapping it opens the app, where the job's card has its result. Once a job,
    whichever of its news and the call's end sees it first; the meta agent hears of a job that ended after the call
    (it looked at the call as it ended)."""
    if job_id in call.notified:
        return
    call.notified.add(job_id)
    job = call.jobs.jobs.get(job_id) if call.jobs else None
    title = (job.title if job and job.title else None) or short(job.task if job else "", 60) or f"Job {job_id}"
    if job is not None and job.ended_at is not None and call.ended_at is not None and job.ended_at >= call.ended_at:
        add_event(call.caller.workspace, "job_ended", now=call.caller.someone.now(),
                  call=f"{call.rec.dir.parent.name}/{call.rec.dir.name}", job=job_id, title=title,
                  outcome="done" if done else "failed")
    korean = call.caller.language.code == "ko"
    if done:
        body = f"결과가 나왔어요: {title}" if korean else f"Results are in: {title}"
    else:
        body = f"작업을 끝내지 못했어요: {title}" if korean else f"Couldn't finish: {title}"
    payload = {"aps": {"alert": {"title": "Bee Call", "body": body}, "sound": "default", "thread-id": "jobs"},
               "kind": "job", "call": call.ctx.room.name}  # the call's id (contract section 2.7)
    try:
        await voip_push.alert(payload, call.caller.someone, collapse_id=f"job-{call.ctx.room.name}"[:60] + f"-{job_id}")
    except Exception as exc:  # the result is in the app either way
        log.warning("the notification for job %s of %s did not go out: %s", job_id, call.rec.dir, exc)


async def finish_jobs(call: Call) -> None:
    """The call's jobs still running at its end run to their end, at most AFTER_CALL_JOBS_S more, or until the voice
    server is stopping (STOPPING); any still running then stops and fails saying why, and of those the person is told
    (as done when its work was done and only its page was left). A job that ended in the call's last RECENT_RESULT_S,
    or since, is notified now (its news may have come as the AI said goodbye, or it is still making its page). Each
    job is notified once at most (job_end_alert)."""
    if not call.jobs:
        return
    ended_at = call.ended_at if call.ended_at is not None else time.monotonic()
    for job in list(call.jobs.jobs.values()):
        if job.status in ("completed", "failed") and job.ended_at is not None \
                and ended_at - job.ended_at <= RECENT_RESULT_S:
            await job_end_alert(call, job.job_id, done=job.status == "completed")
    stopping = call.people.root / STOPPING
    deadline = time.monotonic() + AFTER_CALL_JOBS_S
    pending = set(call.jobs.running())
    while pending and not stopping.exists() and (left := deadline - time.monotonic()) > 0:
        _, pending = await asyncio.wait(pending, timeout=min(STOP_CHECK_S, left))
    if not pending:
        return
    minutes = max(1, round(AFTER_CALL_JOBS_S / 60))
    await call.jobs.cancel_all(RESTARTED if stopping.exists() else f"still running {minutes} minutes after the call "
                               "ended")
    for job in list(call.jobs.jobs.values()):  # only the jobs the call left running: the rest were told in it
        if job.job_id in call.unfinished and job.status in ("completed", "failed", "cancelled"):
            await job_end_alert(call, job.job_id, done=job.status == "completed")


async def end_call(call: Call) -> None:
    """The call's end (the job's shutdown): what it cost, its record closed, the meta agent told, then its title
    written (voice/titles.py) while its failed Bee writes are tried again; then its jobs still running run to their
    end (finish_jobs), which keeps the call's process until they do."""
    caller, rec, session = call.caller, call.rec, call.session
    call.over, call.ended_at = True, time.monotonic()
    call.unfinished = {job_id for job_id, task in call.jobs.tasks.items() if not task.done()} if call.jobs else set()
    # LiveKit runs this in a task of its own, which does not carry the Bee login chosen for the call: choose it
    # again, so the writes retried after the call go to this person's Bee and no one else's.
    bee.use_bee(call.people.bee_dir(caller.id) if caller.uses_bee else None)
    for task in (call.keep_warm, call.fresh):
        if task:
            task.cancel()
    if call.preparation is not None:
        call.preparation.cancel()  # the prep records its own model use (voice/costs.py SpendLog)
    await asyncio.gather(call.prep_recorder, *([call.inputs_saver] if call.inputs_saver else []),
                         return_exceptions=True)
    if call.abandoned():  # nothing of it is kept but what it cost
        await call.jobs.cancel_all()
        spend = SpendLog(caller.workspace)
        models = session_usage(session)
        if models:
            spend.add("call_hung_up_while_ringing", models=models, room=call.ctx.room.name)
        for usage in sorted((rec.dir / "jobs").glob("*/usage*.json")):  # its folder goes, so its jobs' use moves
            try:
                spend.add("job_in_call_left_before_the_ai_spoke", job=json.loads(usage.read_text()),
                          room=call.ctx.room.name)
            except (OSError, ValueError) as exc:
                log.warning("the use of %s is lost: %s", usage, exc)
        rec.close()
        shutil.rmtree(rec.dir, ignore_errors=True)
        return
    claude = getattr(session.llm, "active", session.llm)  # behind GLM 5 standing by, the model that answered
    if isinstance(claude, ClaudeBedrockLLM):
        claude.settle()  # a last reply may still be waiting to be committed
        # Exactly what Claude was sent, for checking a call afterwards.
        (rec.dir / "claude-history.json").write_text(json.dumps(
            {"system": claude.system_prompt, "messages": claude.history}, ensure_ascii=False, indent=1))
    write_usage(rec, session)
    rec.close()
    # The meta agent looks at every call once it is over (meta/agent.py).
    add_event(caller.workspace, "call_ended", now=caller.someone.now(), call=f"{rec.dir.parent.name}/{rec.dir.name}",
              direction=call.meta.get("direction", "inbound"))
    # The user said yes in the call to organizing what Bee recorded over the past days: the meta agent does it now,
    # in the background, and its one notification comes after the call (organize_bee, meta/agent.py).
    request = getattr(call.agent, "organize_request", None)
    if request and caller.uses_bee:
        add_event(caller.workspace, "organize_bee", now=caller.someone.now(),
                  call=f"{rec.dir.parent.name}/{rec.dir.name}", language=caller.language.code, **request)
    # The title (at most 5 s) runs beside the Bee retry, after the record is closed, so neither keeps it open.
    titled = asyncio.create_task(title_call(rec.dir, caller.language, workspace=caller.workspace))
    if caller.uses_bee:
        await after_bee_writes(rec, caller.someone, caller.language)
    await asyncio.gather(titled, return_exceptions=True)
    await finish_jobs(call)


async def run_call(ctx: JobContext, people: People) -> None:
    """One call, from the dispatch to the AI's first words: whose call it is (caller_for), the phone answering or
    ringing while the AI prepares (answer_ai_call, ring_and_prepare), the record (open_record), the session, and
    the pick-up. The rest runs in the session, and its end in end_call."""
    # LiveKit's cloud recording keeps nothing of a call, whatever the LiveKit project says. session.start says so
    # for a call that starts a session (record=False); this says it first, for a job that never does (missed,
    # declined, refused over budget, hung up while it rang), whose logs LiveKit would otherwise upload when the job
    # ends (livekit/agents/job.py _on_cleanup).
    ctx.init_recording(RecordingOptions(audio=False, traces=False, logs=False, transcript=False))
    meta = call_meta(ctx.job.metadata)
    started = time.monotonic()
    room = ctx.job.room.name  # the job's room (LiveKit gives ctx.room this name before the entrypoint runs)
    caller = caller_for(meta, people)
    if caller is None:
        log.error("call in room %s is for no approved person (%r); not answering", room, meta.get("person"))
        ctx.shutdown(reason="no approved person for this call")
        return
    if meta.get("direction") != "outbound":  # the AI's own calls were checked before they were placed
        budget = day_budget(caller.someone, caller.someone.now())
        if budget.used_up:  # the app's pass to the room lasts two hours; the budget is checked as the call starts
            log.info("not answering the call in room %s: %s", room, budget.why())
            await ctx.delete_room(room)  # ends the call on the phone
            ctx.shutdown(reason=budget.why())
            return
    workspace = caller.workspace
    bee.use_bee(people.bee_dir(caller.id) if caller.uses_bee else None)  # only this person's Bee, in this call
    if meta.get("continue_from"):  # the user tapped "Continue this call" in an earlier call's record
        meta["earlier_call"] = earlier_call(workspace, meta["continue_from"])
        meta["continue_from"] = record_id(workspace, meta["continue_from"])  # recorded by its folder, either way
    meta["meta_notes"] = meta_notes(workspace)  # the meta agent's agenda and open threads, for the opening
    # The last calls, from their records (the user's amendment of 2026-10-18); the one a continued call has whole is
    # left out.
    meta["last_calls"] = last_calls(workspace, caller.someone.now().tzinfo, skip=meta.get("continue_from"))
    inbox = Path(meta["inbox"]) if meta.get("inbox") else None
    if inbox is not None and not inbox.resolve().is_relative_to(workspace.resolve()):
        inbox = None  # only the person's own Bee batches
    conversation_ids = meta.get("conversation_ids") or []
    backend = job_worker(caller.settings.worker_model)
    # Wake a cloud worker session now, so the first job does not wait for one to start (the AgentCore harness).
    keep_warm = asyncio.create_task(backend.keep_warm(SpendLog(workspace))) if hasattr(backend, "keep_warm") \
        else None
    # Join the room at once: an AI call waits here for the phone, and a caller hears ringing from here.
    await ctx.connect()
    first_words = asyncio.Event()  # the AI's voice has started
    if meta.get("direction") == "outbound":
        pickup = await answer_ai_call(ctx, caller, meta, inbox, conversation_ids, keep_warm)
    else:
        pickup = await ring_and_prepare(ctx, caller, meta, inbox, conversation_ids, keep_warm, first_words)
    if pickup is None:
        return
    prep = pickup.prep
    rec, answered = open_record(ctx, people, caller, meta)
    inputs, inputs_saver = pickup.inputs, None
    if inputs is None:  # still being read: the call starts now, and the record gets it when it lands
        inputs, inputs_saver = CallInputs(), asyncio.create_task(
            keep_inputs_when_read(rec, pickup.inputs_task, caller.uses_bee, answered.zone()))
    else:
        keep_inputs(rec, inputs, caller.uses_bee, answered.zone())
    rec.event("prep_ready", after_s=round(time.monotonic() - started, 2), opening=prep.opening,
              opening_s=prep.opening_s, note_ready=bool(prep.note))
    session = make_session(ctx, caller.language, caller.tts_voice, rec, caller.call_model)
    call = Call(ctx, people, caller, meta, rec, session, keep_warm=keep_warm,
                fresh=asyncio.create_task(rec.keep_fresh()), preparation=pickup.preparation,
                prep_recorder=asyncio.create_task(record_prep(rec, prep, pickup.preparation)),
                inputs_saver=inputs_saver, left=pickup.left)

    async def notify(news: JobNotification) -> None:
        await tell_about_job(call, news)

    call.jobs = JobRunner(rec.dir, rec.event_or_after, backend, notify, clock=caller.someone.now)
    # Until (or without) a prep note, the plain Bee context. A note still being written replaces it when it
    # arrives, up to the first reply.
    preparation = pickup.preparation
    pending = preparation.done if preparation is not None and not preparation.done.done() else None
    if pending is None and not prep.note and inputs_saver is not None:  # an AI call answered before Bee was read
        pending = asyncio.ensure_future(brief_once_read(pickup.inputs_task, meta, caller.someone.now().tzinfo))
    call.agent = (BeeCoordinator if caller.uses_bee else Coordinator)(
        rec, call.jobs, workspace, clock=caller.someone.now,
        prep_note=prep.note or call_brief(inputs, meta, caller.someone.now().tzinfo),
        pending_note=pending, language=caller.language, earlier_call=meta.get("earlier_call"),
        direction=meta.get("direction", "inbound"), audio_tags=caller.tts_voice.performs_tags,
        budget=lambda: day_budget(caller.someone, caller.someone.now()))

    async def shutdown() -> None:
        await end_call(call)

    ctx.add_shutdown_callback(shutdown)
    wire_events(session, rec)

    @session.on("close")
    def _end_room(ev) -> None:
        # delete_room() schedules itself and returns a future; ending the room ends the phone call.
        ctx.delete_room()

    session.on("agent_state_changed", lambda ev: ev.new_state == "speaking" and first_words.set())
    ringing = pickup.ringing
    try:
        # LiveKit's own recording (audio, transcript, traces) stays off whatever the LiveKit project says: call
        # audio is kept only by our recorder, and only while the person keeps record_on_server on.
        await session.start(room=ctx.room, agent=call.agent, record=False,
                            room_options=caller_audio(caller.noise))
        session.say(prep.opening or greeting(meta, caller.language))
        if ringing is not None:  # ring on through the session start and the voice's first syllable
            spoke, gone = asyncio.create_task(first_words.wait()), asyncio.create_task(pickup.left.wait())
            done, _ = await asyncio.wait({spoke, gone},
                                         timeout=max(0.0, RINGING_AT_MOST_S - (time.monotonic() - pickup.seen)),
                                         return_when=asyncio.FIRST_COMPLETED)
            spoke.cancel(), gone.cancel()
            if not done:
                log.warning("no first words %ss after the caller came, in room %s", RINGING_AT_MOST_S, ctx.room.name)
    finally:
        if ringing is not None:
            ringing.cancel()
            (rang,) = await asyncio.gather(ringing, return_exceptions=True)
            if isinstance(rang, Exception):
                log.error("ringback failed in room %s: %r", ctx.room.name, rang)
    if call.abandoned():
        log.info("the caller hung up while it rang, in room %s", ctx.room.name)
        ctx.shutdown(reason="the caller hung up while it rang")
        return
    # The app shows a call the user started as connected from here.
    await ctx.room.local_participant.set_attributes({STATE_ATTRIBUTE: "answered", CAPTIONS_ATTRIBUTE: "1"})
    report_reconnects(ctx.room, rec)
    rec.event("picked_up", after_s=round(time.monotonic() - started, 2))


def report_reconnects(room, rec: CallRecorder) -> None:
    """Each time the voice server's own room connection comes back, CAPTIONS_ATTRIBUTE changes (and the record
    says so)."""
    reconnects = itertools.count(2)

    async def tell(value: str) -> None:
        try:
            await room.local_participant.set_attributes({CAPTIONS_ATTRIBUTE: value})
        except Exception as exc:  # the room is going away: nothing to tell any more
            log.warning("could not report the reconnect to the app: %s", exc)

    def reconnected(*_) -> None:
        value = str(next(reconnects))
        rec.event("room_reconnected", captions=value)
        asyncio.ensure_future(tell(value))

    room.on("reconnected", reconnected)


def main() -> None:
    """The voice server: the stop that came before is over (STOPPING); jobs a restart or a crash cut off are failed on
    record, now and every few minutes after (worker/runner.py), then LiveKit's CLI."""
    root = data_root()
    (root / STOPPING).unlink(missing_ok=True)
    fail_jobs_left_running(root)
    keep_sweeping(root)
    cli.run_app(server)


if __name__ == "__main__":
    main()
