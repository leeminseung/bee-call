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
The prep and the meta agent run on GLM 5 with anthropic (voice/prep.py prep_model).
WORKER picks what runs delegated jobs:
  fake     pretends to work for a few seconds (test stand-in, the default)
  search   one Nova Web Grounding search per job, with sources (test stand-in, costs about 0.03 USD a job)
  harness  the AgentCore Harness worker in Sydney (deploy it first: scripts/deploy_worker.py)

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
import json
import logging
import os
import shutil
import time
from pathlib import Path

import anthropic
import boto3
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, RecordingOptions, cli, inference, llm
from livekit.plugins import aws, silero
from livekit.plugins.aws.stt import Credentials

from beelink import cli as bee
from beelink.calls import OurCalls
from beelink.context import CallInputs, fetch_call_inputs, save_call_inputs
from beelink.writer import BeeWrites, event_fields, summary_notification
from voice.aws_session import aws_profile, aws_session
from voice.call_models import CallModel, call_model_for
from voice.claude_llm import ClaudeBedrockLLM, default_model
from voice.continuation import earlier_call
from voice.costs import SpendLog, day_budget
from meta.session import add_event, meta_notes
from voice import voip_push
from voice.coordinator import BeeCoordinator, Coordinator
from voice.dispatch import AGENT_NAME, RING_SECONDS, declined
from voice.echo_llm import EchoLLM
from voice.fake_claude import FakeAnthropic
from voice.fallback_llm import FirstRequestFallback
from voice.language import CallLanguage, call_language
from voice.people import People, dev_person
from voice.prep import GLM_5, INBOUND_READ_TIMEOUT_S, OPENING_BY_S, Prep, Preparation, call_brief, prep_model
from voice.recorder import CallRecorder
from voice.ringback import STATE_ATTRIBUTE, ring_back
from voice.voices import make_tts, voice_choice
from worker.fake import FakeWorker
from worker.harness import HarnessWorker
from worker.runner import JobNotification, JobRunner
from worker.search import SearchWorker

load_dotenv(".env.local")
PROFILE = aws_profile()  # None on EC2: the instance role
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", REGION)
COORDINATOR_LLM = os.environ.get("COORDINATOR_LLM", "echo")
# For COORDINATOR_LLM=bedrock. GLM 5 speaks while it calls a tool. MiniMax M2.5 scored best in
# probes/coordinator_bench.py (2026-10-12) but calls tools without a word, and in the first real
# call (2026-10-13) the caller heard silence for 23-28 s whenever they kept talking meanwhile.
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", GLM_5)
NOVA = "us.amazon.nova-2-lite-v1:0"
# Anthropic's API, as the call AI uses it: the SDK does not retry (a failed first request goes to GLM 5 at once, and
# LiveKit retries later ones); no route to it shows within 3 s.
ANTHROPIC_TIMEOUT = anthropic.Timeout(20.0, connect=3.0)
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
# After a call, its failed Bee writes are tried again for at most this long: LiveKit gives a job's shutdown about
# 10 s (shutdown_process_timeout), and the call's own record is closed first (arbitrary).
BEE_RETRY_S = 5.0
# How long LiveKit waits after the caller stops before it ends their turn, learned during the call between these
# bounds (LiveKit 1.8.6 EndpointingOptions). The user's choice (2026-10-15): 0.5-3.0 s left most turns waiting at
# the cap, about 2.4 s; 0.3-2.5 s.
TURN_ENDPOINTING = {"mode": "dynamic", "min_delay": 0.3, "max_delay": 2.5}

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
    """The environment settings of the other modes that are set but change nothing with COORDINATOR_LLM=anthropic,
    where voice/call_models.py alone decides the call model's settings."""
    if COORDINATOR_LLM != "anthropic":
        return []
    return sorted(name for name in ("COORDINATOR_MODEL", "COORDINATOR_THINKING", "COORDINATOR_EFFORT")
                  if os.environ.get(name, "").strip())


def coordinator_model(call_model: CallModel) -> str:
    """The model a call's AI starts with in this COORDINATOR_LLM mode, for the record (with anthropic, GLM 5 can
    still take the call: voice/fallback_llm.py)."""
    if COORDINATOR_LLM == "anthropic":
        return call_model.model
    if COORDINATOR_LLM == "claude":
        return os.environ.get("COORDINATOR_MODEL") or default_model(BEDROCK_REGION)
    return {"bedrock": COORDINATOR_MODEL, "nova": NOVA}.get(COORDINATOR_LLM, COORDINATOR_LLM)


def make_llm(rec: CallRecorder, language: CallLanguage, call_model: CallModel) -> llm.LLM:
    if COORDINATOR_LLM == "anthropic":
        return claude_with_glm_standing_by(rec, language, call_model)
    if COORDINATOR_LLM == "echo":
        return EchoLLM()
    if COORDINATOR_LLM == "nova":
        return aws.LLM(model=NOVA, region="us-east-1")
    if COORDINATOR_LLM == "bedrock":
        return aws.LLM(model=COORDINATOR_MODEL, region=BEDROCK_REGION)
    if COORDINATOR_LLM == "claude":
        return ClaudeBedrockLLM(profile=PROFILE, region=BEDROCK_REGION, on_event=rec.event)
    if COORDINATOR_LLM == "fake":
        # Roughly Claude's pace: first words after 0.7 s, then about 30 words a second.
        client = FakeAnthropic(first_delay=0.7, word_delay=0.03, log_path=rec.dir / "fake-claude.jsonl")
        return ClaudeBedrockLLM(client=client, on_event=rec.event)
    raise SystemExit(f"unknown COORDINATOR_LLM {COORDINATOR_LLM!r}")


def anthropic_client(**options) -> anthropic.AsyncAnthropic:
    """Anthropic's own API (tests put a fake here)."""
    return anthropic.AsyncAnthropic(**options)


def claude_with_glm_standing_by(rec: CallRecorder, language: CallLanguage, call_model: CallModel) -> llm.LLM:
    """COORDINATOR_LLM=anthropic: the person's Claude model (voice/call_models.py) on Anthropic's own API, GLM 5 on
    Bedrock standing by. The record's coordinator_model event names the model that answered the call, and why it
    is not Claude when it is not."""
    def glm() -> llm.LLM:
        return aws.LLM(model=GLM_5, region=BEDROCK_REGION)

    def chosen(model: llm.LLM, reason: str | None) -> None:
        rec.event("coordinator_model", model=model.model, provider=model.provider,
                  **({"instead_of": call_model.model, "reason": reason} if reason else {}))

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        model = glm()
        chosen(model, "ANTHROPIC_API_KEY is not set")
        return model
    client = anthropic_client(api_key=key, max_retries=0, timeout=ANTHROPIC_TIMEOUT)
    claude = ClaudeBedrockLLM(client=client, model=call_model.model, thinking=call_model.thinking,
                              effort=call_model.effort, auto_cache=True, refusal_line=language.refusal,
                              on_event=rec.event)
    return FirstRequestFallback(claude, glm, on_choice=chosen)


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


async def brief_once_read(task: asyncio.Task, meta: dict) -> str | None:
    """The plain Bee context, once the task has read Bee (None if the read failed)."""
    try:
        return call_brief(await asyncio.shield(task), meta)
    except Exception:
        return None


def prewarm(proc: JobProcess) -> None:
    # Done once per worker process, before any call, so calls start without a stall.
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["aws"] = aws_session() if PROFILE else None  # none on EC2, see transcribe_credentials
    if proc.userdata["aws"] is not None:
        proc.userdata["aws"].get_credentials()  # read the profile now rather than at the first call


server = AgentServer(setup_fnc=prewarm, num_idle_processes=1)


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


async def run_call(ctx: JobContext, people: People) -> None:
    """One call, from the dispatch to the AI's first words; the rest runs in the session and its shutdown."""
    # LiveKit's cloud recording keeps nothing of a call, whatever the LiveKit project says. session.start says so
    # for a call that starts a session (record=False); this says it first, for a job that never does (missed,
    # declined, refused over budget, hung up while it rang), whose logs LiveKit would otherwise upload when the job
    # ends (livekit/agents/job.py _on_cleanup).
    ctx.init_recording(RecordingOptions(audio=False, traces=False, logs=False, transcript=False))
    meta = call_meta(ctx.job.metadata)
    started = time.monotonic()
    room = ctx.job.room.name  # the job's room (LiveKit gives ctx.room this name before the entrypoint runs)
    person = person_for_call(meta, people)
    if person is None:
        log.error("call in room %s is for no approved person (%r); not answering", room, meta.get("person"))
        ctx.shutdown(reason="no approved person for this call")
        return
    someone = people.person(person)  # their folder, settings, phone and clock (someone.now)
    if meta.get("direction") != "outbound":  # the AI's own calls were checked before they were placed
        budget = day_budget(someone, someone.now())
        if budget.used_up:  # the app's pass to the room lasts two hours; the budget is checked as the call starts
            log.info("not answering the call in room %s: %s", room, budget.why())
            await ctx.delete_room(room)  # ends the call on the phone
            ctx.shutdown(reason=budget.why())
            return
    workspace = people.workspace(person)
    settings = people.settings(person)
    language = call_language(settings.language)
    call_model = call_model_for(settings)  # the person's choice of the call AI's model, read with the language
    tts_voice = voice_choice(language, {**os.environ, **settings.voice_env()})
    # Only this person's Bee, in this call. Without Bee (Use Bee off) the call reads nothing from Bee, the AI has
    # no Bee tools, and nothing it is told mentions Bee (BC-057).
    uses_bee = settings.bee
    bee.use_bee(people.bee_dir(person) if uses_bee else None)
    if meta.get("continue_from"):  # the user tapped "Continue this call" in an earlier call's record
        meta["earlier_call"] = earlier_call(workspace, meta["continue_from"])
    meta["meta_notes"] = meta_notes(workspace)  # the meta agent's agenda and open threads, for the opening
    inbox = Path(meta["inbox"]) if meta.get("inbox") else None
    if inbox is not None and not inbox.resolve().is_relative_to(workspace.resolve()):
        inbox = None  # only the person's own Bee batches
    conversation_ids = meta.get("conversation_ids") or []
    backend = {"search": SearchWorker, "harness": HarnessWorker}.get(WORKER, FakeWorker)()
    # Wake a cloud worker session now, so the first job does not wait for one to start.
    keep_warm = asyncio.create_task(backend.keep_warm(SpendLog(workspace))) if isinstance(backend, HarnessWorker) \
        else None
    # Join the room at once: an AI call waits here for the phone, and a caller hears ringing from here.
    await ctx.connect()
    first_words = asyncio.Event()  # the AI's voice has started
    if meta.get("direction") == "outbound":
        # Prepared before the phone rang (voice/outbound.py). Bee is read again for the call's record meanwhile.
        inputs_task = asyncio.create_task(fetch_call_inputs(inbox, conversation_ids, ours=OurCalls.of(workspace)))
        answer = await wait_for_answer(ctx, workspace)
        if answer != "answered":
            log.info("the call in room %s was %s", ctx.room.name, answer)
            add_event(workspace, f"call_{answer}", now=someone.now(), room=ctx.room.name, topic=meta.get("topic"),
                      reason=meta.get("reason"))
            inputs_task.cancel()
            if keep_warm:
                keep_warm.cancel()
            try:
                await ctx.delete_room()
            except Exception:
                log.exception("could not close room %s", ctx.room.name)
            return
        prep = Prep.from_meta(meta.get("prep")) or Prep(error="the call was placed without a prep note")
        preparation = ringing = left = None
        # The AI speaks as soon as the phone answers (BC-022). This read is for the call record, and for the plain
        # Bee context when the call has no prep note, which then reaches the AI once the read lands.
        inputs = read_already(inputs_task)
    else:
        # The user called: they hear ringing while the AI reads Bee and starts its prep. It picks up as soon as
        # its opening is written; the note usually follows during the opening (voice/prep.py).
        ringing = asyncio.create_task(ring_back(ctx.room))

        async def stop_ringing() -> None:  # if the job ends before the AI picks up
            ringing.cancel()

        ctx.add_shutdown_callback(stop_ringing)
        preparation = inputs_task = None
        # The caller hung up before the AI's first words (BC-027): the call then leaves nothing but its cost. This is
        # watched until the first words, also after the ringing stops. A hang-up is a leave LiveKit itself ends the
        # session for; a phone that lost the network is not one, and a caller who comes back is no longer gone.
        left = asyncio.Event()

        def caller_left(participant) -> None:
            if the_caller(participant) and not first_words.is_set() \
                    and getattr(participant, "disconnect_reason", None) in HANG_UPS:
                left.set()

        ctx.room.on("participant_disconnected", caller_left)
        ctx.room.on("participant_connected", lambda participant: the_caller(participant) and left.clear())
        never_came = None  # why the caller is gone without having been seen, for the log
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), CALLER_WAIT_S)
            seen = time.monotonic()  # the opening's 6 s and the ringing's 8 s count from here (BC-025)
        except asyncio.TimeoutError:
            seen = time.monotonic()
            # The app's join made this room, so a caller not in it by now left before the voice server came, and no
            # disconnect will be seen. (LiveKit's console mode has no caller in a room.)
            if not ctx.is_fake_job():
                never_came = f"no caller came into the room within {CALLER_WAIT_S:g} s"
                left.set()
        except Exception as exc:  # LiveKit's wait raises when the room goes away meanwhile
            seen = time.monotonic()
            never_came = f"the room closed before the caller came into it ({exc})"
            left.set()

        async def get_ready() -> CallInputs:
            nonlocal preparation
            ours = OurCalls.of(workspace)  # what Bee made of earlier calls is not new context (BC-060)
            got = await fetch_call_inputs(inbox, conversation_ids, timeout=INBOUND_READ_TIMEOUT_S, ours=ours)
            preparation = Preparation(got, meta, prep_model(COORDINATOR_LLM), spend=SpendLog(workspace), bee=uses_bee,
                                      now=someone.now(), language=language, ours=ours).start()
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
            return
        inputs = ready.result()
        prep = preparation.prep
    # The phone sends the person's settings as it answers (contract section 1): whether audio is kept is read now.
    settings = people.settings(person)
    rec = CallRecorder(workspace, ctx.job.room.name, keep_audio=settings.record_on_server, now=someone.now())
    unused = ignored_settings()
    if unused:
        log.warning("%s set but not used with COORDINATOR_LLM=anthropic: the call model's settings come from "
                    "voice/call_models.py", ", ".join(unused))
    rec.event("coordinator", llm=COORDINATOR_LLM, model=coordinator_model(call_model), call_model=call_model.name,
              language=language.code, voice=tts_voice.describe(), person=person,
              audio_on_server=settings.record_on_server, audio_on_phone=settings.record_on_phone,
              **({"settings_not_used": unused} if unused else {}))
    rec.event("call", direction=meta.get("direction", "inbound"), topic=meta.get("topic"), reason=meta.get("reason"),
              continue_from=meta.get("continue_from") if meta.get("earlier_call") else None)

    def keep_inputs(got: CallInputs) -> None:
        if not uses_bee:
            return  # nothing was read from Bee, so there is nothing to keep
        try:
            card = save_call_inputs(rec.dir, got, settings.zone())
        except Exception as exc:  # the call goes on without its Bee record
            rec.event("error", source="bee_context", error=f"saving the Bee inputs failed: {exc}")
            return
        if card.get("conversation_id") or card.get("pending_fact_ids"):
            rec.event("why_calling", **card)
        if got.errors:
            rec.event("error", source="bee_context", error="; ".join(got.errors))

    async def keep_inputs_when_read() -> None:
        try:
            keep_inputs(await inputs_task)
        except Exception as exc:
            rec.event("error", source="bee_context", error=f"reading Bee failed: {exc}")

    inputs_saver = None
    if inputs is None:  # still being read: the call starts now, and the record gets it when it lands
        inputs, inputs_saver = CallInputs(), asyncio.create_task(keep_inputs_when_read())
    else:
        keep_inputs(inputs)
    rec.event("prep_ready", after_s=round(time.monotonic() - started, 2), opening=prep.opening,
              opening_s=prep.opening_s, note_ready=bool(prep.note))

    async def record_prep() -> None:
        if preparation is not None:
            await preparation.done
        rec.event("call_prep", seconds=prep.seconds, model=prep.model, reads=prep.reads, note=prep.note,
                  opening=prep.opening, error=prep.error, input_tokens=prep.input_tokens,
                  output_tokens=prep.output_tokens)
        if prep.note:
            (rec.dir / "prep.md").write_text(f"# Call prep note\n\n{prep.note}\n\nOpening: {prep.opening}\n")

    prep_recorder = asyncio.create_task(record_prep())
    session = make_session(ctx, language, tts_voice, rec, call_model)

    async def notify(news: JobNotification) -> None:
        if COORDINATOR_LLM == "echo":
            if news.needs_reply:
                session.say(f"Sorry, background job {news.job_id} failed." if news.error else news.summary)
            return
        # The note stays in the agent's history, so it still reaches the model if this reply is cut off.
        if await agent.tell_job(news):
            session.generate_reply()

    jobs = JobRunner(rec.dir, rec.event, backend, notify, clock=someone.now)
    # Until (or without) a prep note, the plain Bee context. A note still being written replaces it when it
    # arrives, up to the first reply.
    pending = preparation.done if preparation is not None and not preparation.done.done() else None
    if pending is None and not prep.note and inputs_saver is not None:  # an AI call answered before Bee was read
        pending = asyncio.ensure_future(brief_once_read(inputs_task, meta))
    agent = (BeeCoordinator if uses_bee else Coordinator)(
        rec, jobs, workspace, clock=someone.now, prep_note=prep.note or call_brief(inputs, meta), pending_note=pending,
        language=language, earlier_call=meta.get("earlier_call"), direction=meta.get("direction", "inbound"))

    def abandoned() -> bool:
        """The caller hung up before the AI's first words: the call leaves no record."""
        return left is not None and left.is_set()

    async def shutdown() -> None:
        # LiveKit runs this in a task of its own, which does not carry the Bee login chosen above: choose it again,
        # so the writes retried after the call go to this person's Bee and no one else's.
        bee.use_bee(people.bee_dir(person) if uses_bee else None)
        if keep_warm:
            keep_warm.cancel()
        if preparation is not None:
            preparation.cancel()  # the prep records its own model use (voice/costs.py SpendLog)
        await asyncio.gather(prep_recorder, *([inputs_saver] if inputs_saver else []), return_exceptions=True)
        await jobs.cancel_all()
        if abandoned():  # nothing of it is kept but what it cost
            spend = SpendLog(workspace)
            models = session_usage(session)
            if models:
                spend.add("call_hung_up_while_ringing", models=models, room=ctx.room.name)
            for usage in sorted((rec.dir / "jobs").glob("*/usage.json")):  # its folder goes, so its jobs' use moves
                try:
                    spend.add("job_in_call_left_before_the_ai_spoke", job=json.loads(usage.read_text()),
                              room=ctx.room.name)
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
        add_event(workspace, "call_ended", now=someone.now(), call=f"{rec.dir.parent.name}/{rec.dir.name}",
                  direction=meta.get("direction", "inbound"))
        if uses_bee:  # last, so a slow Bee never keeps the call's record open
            await after_bee_writes(rec, someone, language)

    ctx.add_shutdown_callback(shutdown)
    wire_events(session, rec)

    @session.on("close")
    def _end_room(ev) -> None:
        # delete_room() schedules itself and returns a future; ending the room ends the phone call.
        ctx.delete_room()

    session.on("agent_state_changed", lambda ev: ev.new_state == "speaking" and first_words.set())
    try:
        # LiveKit's own recording (audio, transcript, traces) stays off whatever the LiveKit project says: call
        # audio is kept only by our recorder, and only while the person keeps record_on_server on.
        await session.start(room=ctx.room, agent=agent, record=False)
        session.say(prep.opening or greeting(meta, language))
        if ringing is not None:  # ring on through the session start and the voice's first syllable
            spoke, gone = asyncio.create_task(first_words.wait()), asyncio.create_task(left.wait())
            done, _ = await asyncio.wait({spoke, gone}, timeout=max(0.0, RINGING_AT_MOST_S - (time.monotonic() - seen)),
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
    if abandoned():
        log.info("the caller hung up while it rang, in room %s", ctx.room.name)
        ctx.shutdown(reason="the caller hung up while it rang")
        return
    # The app shows a call the user started as connected from here.
    await ctx.room.local_participant.set_attributes({STATE_ATTRIBUTE: "answered"})
    rec.event("picked_up", after_s=round(time.monotonic() - started, 2))


if __name__ == "__main__":
    cli.run_app(server)
