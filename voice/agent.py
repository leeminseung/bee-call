"""bee-call voice server: LiveKit Agents on this Mac.

Audio comes in from a LiveKit Cloud room, Amazon Transcribe (Sydney) turns it
into words, the coordinator answers, and Amazon Polly (Sydney) speaks. Every
call is recorded into its session folder by CallRecorder, the Bee context for
the call is saved into inputs/, and delegated jobs run in the background.

COORDINATOR_LLM picks the model in the coordinator slot:
  claude  Claude Sonnet 5.5 on Bedrock (the product)
  fake    the Claude adapter with scripted replies and no network (test stand-in
          that checks every request the way the Messages API would)
  bedrock any other Bedrock model through the Converse API, named by COORDINATOR_MODEL
          (stand-in while Claude is blocked on this account; probes/coordinator_bench.py compares them)
  nova    Amazon Nova 2 Lite on Bedrock (test stand-in that can call tools)
  echo    repeats the caller (test stand-in for the audio path)
WORKER picks what runs delegated jobs:
  fake     pretends to work for a few seconds (test stand-in, the default)
  search   one Nova Web Grounding search per job, with sources (test stand-in, costs about 0.03 USD a job)
  harness  the AgentCore Harness worker in Sydney (deploy it first: scripts/deploy_worker.py)

The server joins only rooms it is dispatched to, as the agent named AGENT_NAME:
the app's token dispatches it for a call the user starts, and voice/dispatch.py
for a call the AI starts. The dispatch metadata says which, and for an AI call
why it is calling; the AI then waits for the phone to answer before speaking.

Run: uv run python -m voice.agent dev       (joins rooms on LiveKit Cloud)
     uv run python -m voice.agent console   (this Mac's mic and speakers)
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import boto3
from dotenv import load_dotenv
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli, inference, llm
from livekit.plugins import aws, silero
from livekit.plugins.aws.stt import Credentials

from beelink.context import CallInputs, fetch_call_inputs, save_call_inputs
from voice.aws_session import aws_profile, aws_session
from voice.claude_llm import ClaudeBedrockLLM
from voice.continuation import earlier_call
from meta.session import add_event, meta_notes
from voice.coordinator import Coordinator
from voice.dispatch import AGENT_NAME, RING_SECONDS
from voice.echo_llm import EchoLLM
from voice.fake_claude import FakeAnthropic
from voice.language import call_language
from voice.prep import INBOUND_READ_TIMEOUT_S, OPENING_BY_S, Prep, Preparation, call_brief, prep_model
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
LANGUAGE = call_language()  # CALL_LANGUAGE: en (default) or ko, see voice/language.py
VOICE = voice_choice(LANGUAGE)  # TTS: polly (default) or elevenlabs, see voice/voices.py
COORDINATOR_LLM = os.environ.get("COORDINATOR_LLM", "echo")
# For COORDINATOR_LLM=bedrock. GLM 5 speaks while it calls a tool. MiniMax M2.5 scored best in
# probes/coordinator_bench.py (2026-10-12) but calls tools without a word, and in the first real
# call (2026-10-13) the caller heard silence for 23-28 s whenever they kept talking meanwhile.
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", "zai.glm-5")
WORKER = os.environ.get("WORKER", "fake")
FIRST_WORDS_WAIT_S = 6.0  # ringing stops when the AI starts to speak, or after this long anyway (arbitrary)
# After the user answers an AI call, its prepared opening does not wait longer than this for the Bee read
# that only feeds the call record (arbitrary).
OUTBOUND_INPUTS_WAIT_S = 0.5
WORKSPACE = Path("workspace")

log = logging.getLogger("bee-call")


def transcribe_credentials(session: boto3.Session) -> Credentials:
    # The Transcribe plugin does not read ~/.aws profiles, so hand it the keys in memory, asked for at the start
    # of every call: an instance role's keys expire after a few hours, and the session renews them before that.
    # Nothing is printed or written to disk.
    found = session.get_credentials()
    if found is None:
        raise RuntimeError("no AWS credentials: set AWS_PROFILE, or run with an instance role")
    creds = found.get_frozen_credentials()
    return Credentials(access_key_id=creds.access_key, secret_access_key=creds.secret_key,
                       session_token=creds.token)


def make_llm(rec: CallRecorder) -> llm.LLM:
    if COORDINATOR_LLM == "echo":
        return EchoLLM()
    if COORDINATOR_LLM == "nova":
        return aws.LLM(model="us.amazon.nova-2-lite-v1:0", region="us-east-1")
    if COORDINATOR_LLM == "bedrock":
        return aws.LLM(model=COORDINATOR_MODEL, region=BEDROCK_REGION)
    if COORDINATOR_LLM == "claude":
        return ClaudeBedrockLLM(profile=PROFILE, region=BEDROCK_REGION, on_event=rec.event)
    if COORDINATOR_LLM == "fake":
        # Roughly Claude's pace: first words after 0.7 s, then about 30 words a second.
        client = FakeAnthropic(first_delay=0.7, word_delay=0.03, log_path=rec.dir / "fake-claude.jsonl")
        return ClaudeBedrockLLM(client=client, on_event=rec.event)
    raise SystemExit(f"unknown COORDINATOR_LLM {COORDINATOR_LLM!r}")


def as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def wire_events(session: AgentSession, rec: CallRecorder) -> None:
    # LiveKit calls these handlers inside its reply pipeline; an exception here would
    # cut the pipeline (a missing reply), so every handler is wrapped.
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
        rec.event("session_closed", reason=str(getattr(ev, "reason", "")))


def call_meta(raw: str | None) -> dict:
    try:
        meta = json.loads(raw) if raw else {}
    except ValueError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def greeting(meta: dict) -> str:
    """The first line when there is no prepared opening."""
    lines = LANGUAGE.greetings
    if COORDINATOR_LLM == "echo":
        return lines["echo"]
    if meta.get("earlier_call"):
        return lines["continue"]
    if meta.get("direction") == "outbound":
        if meta.get("kind") == "voice note":
            return lines["voice note"]
        if meta.get("topic"):
            return lines["topic"].format(topic=meta["topic"])
        return lines["outbound"]
    return lines["inbound"]


async def read_by(task: asyncio.Task, seconds: float) -> CallInputs | None:
    """The task's Bee inputs if they are read within `seconds`; None while still reading (the task keeps going)."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), seconds)
    except asyncio.TimeoutError:
        return None


def prewarm(proc: JobProcess) -> None:
    # Done once per worker process, before any call, so calls start without a stall.
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["aws"] = aws_session()
    proc.userdata["aws"].get_credentials()  # finds them now, so a call does not wait for the instance metadata


server = AgentServer(setup_fnc=prewarm, num_idle_processes=1)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    meta = call_meta(ctx.job.metadata)
    started = time.monotonic()
    if meta.get("continue_from"):  # the user tapped "Continue this call" in an earlier call's record
        meta["earlier_call"] = earlier_call(WORKSPACE, meta["continue_from"])
    meta["meta_notes"] = meta_notes(WORKSPACE)  # the meta agent's agenda and open threads, for the opening
    inbox = Path(meta["inbox"]) if meta.get("inbox") else None
    conversation_ids = meta.get("conversation_ids") or []
    backend = {"search": SearchWorker, "harness": HarnessWorker}.get(WORKER, FakeWorker)()
    # Wake a cloud worker session now, so the first job does not wait for one to start.
    keep_warm = asyncio.create_task(backend.keep_warm()) if isinstance(backend, HarnessWorker) else None
    # Join the room at once: an AI call waits here for the phone, and a caller hears ringing from here.
    await ctx.connect()
    if meta.get("direction") == "outbound":
        # Prepared before the phone rang (voice/outbound.py). Bee is read again for the call's record meanwhile.
        inputs_task = asyncio.create_task(fetch_call_inputs(inbox, conversation_ids))
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), RING_SECONDS)
        except asyncio.TimeoutError:
            log.info("no answer within %ss in room %s", RING_SECONDS, ctx.room.name)
            add_event(WORKSPACE, "call_missed", room=ctx.room.name, topic=meta.get("topic"), reason=meta.get("reason"))
            inputs_task.cancel()
            if keep_warm:
                keep_warm.cancel()
            try:
                await ctx.delete_room()
            except Exception:
                log.exception("could not close room %s", ctx.room.name)
            return
        prep = Prep.from_meta(meta.get("prep")) or Prep(error="the call was placed without a prep note")
        preparation = ringing = None
        # This read is for the call record, and for the plain Bee context only when the call has no prep note.
        inputs = await read_by(inputs_task, OUTBOUND_INPUTS_WAIT_S if prep.note else INBOUND_READ_TIMEOUT_S)
    else:
        # The user called: they hear ringing while the AI reads Bee and starts its prep. It picks up as soon as
        # its opening is written; the note usually follows during the opening (voice/prep.py).
        ringing = asyncio.create_task(ring_back(ctx.room))

        async def stop_ringing() -> None:  # if the job ends before the AI picks up
            ringing.cancel()

        ctx.add_shutdown_callback(stop_ringing)
        preparation = inputs_task = None
        left = asyncio.Event()  # the caller hung up while it rang
        try:
            caller = await asyncio.wait_for(ctx.wait_for_participant(), 5)
            ctx.room.on("participant_disconnected", lambda p: p.identity == caller.identity and left.set())
        except asyncio.TimeoutError:
            pass

        async def get_ready() -> CallInputs:
            nonlocal preparation
            got = await fetch_call_inputs(inbox, conversation_ids, timeout=INBOUND_READ_TIMEOUT_S)
            preparation = Preparation(got, meta, prep_model(COORDINATOR_LLM), language=LANGUAGE).start()
            await preparation.opening(timeout=OPENING_BY_S - (time.monotonic() - started))
            return got

        ready, hung_up = asyncio.create_task(get_ready()), asyncio.create_task(left.wait())
        await asyncio.wait({ready, hung_up}, return_when=asyncio.FIRST_COMPLETED)
        hung_up.cancel()
        if not ready.done():
            log.info("the caller hung up while it rang, in room %s", ctx.room.name)
            for task in (ready, ringing, keep_warm):
                if task:
                    task.cancel()
            if preparation is not None:
                preparation.cancel()
            await asyncio.gather(ready, ringing, return_exceptions=True)
            ctx.shutdown(reason="the caller hung up while it rang")
            return
        inputs = ready.result()
        prep = preparation.prep
    rec = CallRecorder(WORKSPACE, ctx.job.room.name)
    rec.event("coordinator", llm=COORDINATOR_LLM, language=LANGUAGE.code, voice=VOICE.describe())
    rec.event("call", direction=meta.get("direction", "inbound"), topic=meta.get("topic"), reason=meta.get("reason"),
              continue_from=meta.get("continue_from") if meta.get("earlier_call") else None)

    def keep_inputs(got: CallInputs) -> None:
        try:
            card = save_call_inputs(rec.dir, got)
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
                  opening=prep.opening, error=prep.error)
        if prep.note:
            (rec.dir / "prep.md").write_text(f"# Call prep note\n\n{prep.note}\n\nOpening: {prep.opening}\n")

    prep_recorder = asyncio.create_task(record_prep())
    # The keys as they are at this call's start: an instance role's keys expire after a few hours.
    ctx.proc.userdata["transcribe_credentials"] = transcribe_credentials(ctx.proc.userdata["aws"])
    session = AgentSession(
        stt=aws.STT(region=REGION, language=LANGUAGE.transcribe, credentials=ctx.proc.userdata["transcribe_credentials"]),
        llm=make_llm(rec),
        tts=make_tts(VOICE, LANGUAGE, REGION),
        vad=ctx.proc.userdata["vad"],
        turn_handling={"turn_detection": inference.TurnDetector()},
    )

    async def notify(news: JobNotification) -> None:
        if COORDINATOR_LLM == "echo":
            if news.needs_reply:
                session.say(f"Sorry, background job {news.job_id} failed." if news.error else news.summary)
            return
        # The note stays in the agent's history, so it still reaches the model if this reply is cut off.
        if await agent.tell_job(news):
            session.generate_reply()

    jobs = JobRunner(rec.dir, rec.event, backend, notify)
    # Until (or without) a prep note, the plain Bee context. A note still being written replaces it when it
    # arrives, up to the first reply.
    pending = preparation.done if preparation is not None and not preparation.done.done() else None
    agent = Coordinator(rec, jobs, WORKSPACE, prep_note=prep.note or call_brief(inputs, meta), pending_note=pending,
                        language=LANGUAGE, earlier_call=meta.get("earlier_call"))

    async def shutdown() -> None:
        if keep_warm:
            keep_warm.cancel()
        if preparation is not None:
            preparation.cancel()
        await asyncio.gather(prep_recorder, *([inputs_saver] if inputs_saver else []), return_exceptions=True)
        await jobs.cancel_all()
        if isinstance(session.llm, ClaudeBedrockLLM):
            session.llm.settle()  # a last reply may still be waiting to be committed
            # Exactly what Claude was sent, for checking a call afterwards.
            (rec.dir / "claude-history.json").write_text(json.dumps(
                {"system": session.llm.system_prompt, "messages": session.llm.history}, ensure_ascii=False, indent=1))
        rec.close()
        # The meta agent looks at every call once it is over (meta/agent.py).
        add_event(WORKSPACE, "call_ended", call=f"{rec.dir.parent.name}/{rec.dir.name}",
                  direction=meta.get("direction", "inbound"))

    ctx.add_shutdown_callback(shutdown)
    wire_events(session, rec)

    @session.on("close")
    def _end_room(ev) -> None:
        # delete_room() schedules itself and returns a future; ending the room ends the phone call.
        ctx.delete_room()

    first_words = asyncio.Event()
    session.on("agent_state_changed", lambda ev: ev.new_state == "speaking" and first_words.set())
    try:
        await session.start(room=ctx.room, agent=agent)
        session.say(prep.opening or greeting(meta))
        if ringing is not None:  # ring on through the session start and the voice's first syllable
            await asyncio.wait_for(first_words.wait(), FIRST_WORDS_WAIT_S)
    except asyncio.TimeoutError:
        log.warning("no first words within %ss in room %s", FIRST_WORDS_WAIT_S, ctx.room.name)
    finally:
        if ringing is not None:
            ringing.cancel()
            (rang,) = await asyncio.gather(ringing, return_exceptions=True)
            if isinstance(rang, Exception):
                log.error("ringback failed in room %s: %r", ctx.room.name, rang)
    # The app shows a call the user started as connected from here.
    await ctx.room.local_participant.set_attributes({STATE_ATTRIBUTE: "answered"})
    rec.event("picked_up", after_s=round(time.monotonic() - started, 2))


if __name__ == "__main__":
    cli.run_app(server)
