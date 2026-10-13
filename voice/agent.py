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
from pathlib import Path

import boto3
from dotenv import load_dotenv
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli, inference, llm
from livekit.plugins import aws, silero
from livekit.plugins.aws.stt import Credentials

from beelink.context import save_call_inputs
from voice.claude_llm import ClaudeBedrockLLM
from voice.coordinator import Coordinator
from voice.dispatch import AGENT_NAME, RING_SECONDS
from voice.echo_llm import EchoLLM
from voice.fake_claude import FakeAnthropic
from voice.recorder import CallRecorder
from worker.fake import FakeWorker
from worker.harness import HarnessWorker
from worker.runner import JobRunner
from worker.search import SearchWorker

load_dotenv(".env.local")
PROFILE = os.environ.get("AWS_PROFILE", "bee-call")
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", REGION)
VOICE = os.environ.get("POLLY_VOICE", "Ruth")
COORDINATOR_LLM = os.environ.get("COORDINATOR_LLM", "echo")
# For COORDINATOR_LLM=bedrock. GLM 5 speaks while it calls a tool. MiniMax M2.5 scored best in
# probes/coordinator_bench.py (2026-10-12) but calls tools without a word, and in the first real
# call (2026-10-13) the caller heard silence for 23-28 s whenever they kept talking meanwhile.
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", "zai.glm-5")
WORKER = os.environ.get("WORKER", "fake")
WORKSPACE = Path("workspace")

log = logging.getLogger("bee-call")


def transcribe_credentials() -> Credentials:
    # The Transcribe plugin does not read ~/.aws profiles, so hand it the
    # profile's keys in memory. Nothing is printed or written to disk.
    creds = boto3.Session(profile_name=PROFILE).get_credentials().get_frozen_credentials()
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
    if COORDINATOR_LLM == "echo":
        return "Hi! This is the echo test. Say something and I'll repeat it."
    if meta.get("direction") == "outbound":
        if meta.get("kind") == "voice note":
            return "Hi, it's Bee Call. I'm calling about your new voice note. Do you have a minute?"
        if meta.get("topic"):
            return f"Hi, it's Bee Call. I'm calling about your conversation: {meta['topic']}. Do you have a minute?"
        return "Hi, it's Bee Call. Do you have a minute to talk through your day?"
    return "Hi, it's Bee Call. What's on your mind?"


def bee_note(saved: dict, meta: dict | None = None) -> str | None:
    lines = []
    meta = meta or {}
    if meta.get("direction") == "outbound":
        lines.append(f"You placed this call. Why: {meta.get('reason') or "to talk through the user's day"}")
    elif meta.get("direction") == "inbound":
        lines.append("The user called you from the app.")
    card = saved["card"]
    if card.get("summary"):
        lines.append(f"Latest Bee conversation (id {card['conversation_id']}): {card['summary']}")
    for fact in saved["facts"][:10]:
        lines.append(f"Unconfirmed Bee fact (id {fact.get('id')}): {fact.get('text')}")
    for sugg in saved["suggestions"][:10]:
        lines.append(f"Bee todo suggestion (id {sugg.get('id')}): {sugg.get('text')}")
    return "Note from the app, call context:\n" + "\n".join(lines) if lines else None


def prewarm(proc: JobProcess) -> None:
    # Done once per worker process, before any call, so calls start without a stall.
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["transcribe_credentials"] = transcribe_credentials()


server = AgentServer(setup_fnc=prewarm, num_idle_processes=1)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    meta = call_meta(ctx.job.metadata)
    if meta.get("direction") == "outbound":
        # The AI placed this call: the room exists before the phone answers, so wait for it.
        await ctx.connect()
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), RING_SECONDS)
        except asyncio.TimeoutError:
            log.info("no answer within %ss in room %s", RING_SECONDS, ctx.room.name)
            try:
                await ctx.delete_room()
            except Exception:
                log.exception("could not close room %s", ctx.room.name)
            return
    rec = CallRecorder(WORKSPACE, ctx.job.room.name)
    rec.event("coordinator", llm=COORDINATOR_LLM)
    rec.event("call", direction=meta.get("direction", "inbound"), topic=meta.get("topic"), reason=meta.get("reason"))
    session = AgentSession(
        stt=aws.STT(region=REGION, language="en-US", credentials=ctx.proc.userdata["transcribe_credentials"]),
        llm=make_llm(rec),
        tts=aws.TTS(voice=VOICE, speech_engine="generative", region=REGION),
        vad=ctx.proc.userdata["vad"],
        turn_handling={"turn_detection": inference.TurnDetector()},
    )

    async def relay(job_id: str, text: str, failed: bool = False) -> None:
        if COORDINATOR_LLM == "echo":
            session.say(f"Sorry, background job {job_id} failed." if failed else text)
            return
        if failed:
            note = (f"Note from the app: background job {job_id} failed ({text}). Tell the user in one short "
                    "sentence, and offer to try again or another way.")
        else:
            note = (f"Note from the app: background job {job_id} finished. Tell the user the key result in "
                    f"your own words, in at most two sentences, and say the details are in the app. Result: {text}")
        # The note stays in the agent's history, so it still reaches the model if this reply is cut off.
        await agent.add_note(note)
        session.generate_reply()

    backend = {"search": SearchWorker, "harness": HarnessWorker}.get(WORKER, FakeWorker)()
    # Wake a cloud worker session now, so the first job does not wait for one to start.
    keep_warm = asyncio.create_task(backend.keep_warm()) if isinstance(backend, HarnessWorker) else None
    jobs = JobRunner(rec.dir, rec.event, backend, relay)
    agent = Coordinator(rec, jobs, WORKSPACE, notes_as_user=COORDINATOR_LLM not in ("claude", "fake"))

    async def shutdown() -> None:
        if keep_warm:
            keep_warm.cancel()
        await jobs.cancel_all()
        if isinstance(session.llm, ClaudeBedrockLLM):
            session.llm.settle()  # a last reply may still be waiting to be committed
            # Exactly what Claude was sent, for checking a call afterwards.
            (rec.dir / "claude-history.json").write_text(json.dumps(
                {"system": session.llm.system_prompt, "messages": session.llm.history}, ensure_ascii=False, indent=1))
        rec.close()

    ctx.add_shutdown_callback(shutdown)
    wire_events(session, rec)

    @session.on("close")
    def _end_room(ev) -> None:
        # delete_room() schedules itself and returns a future; ending the room ends the phone call.
        ctx.delete_room()

    async def load_bee_context() -> None:
        try:
            saved = await save_call_inputs(rec.dir, Path(meta["inbox"]) if meta.get("inbox") else None,
                                           meta.get("conversation_ids") or [])
        except Exception as exc:
            rec.event("error", source="bee_context", error=str(exc))
            return
        card = saved["card"]
        if card.get("conversation_id") or card.get("pending_fact_ids"):
            rec.event("why_calling", **card)
        note = bee_note(saved, meta)
        if note and COORDINATOR_LLM != "echo":
            chat_ctx = agent.chat_ctx.copy()
            agent.context_note = chat_ctx.add_message(role="system", content=note)
            await agent.update_chat_ctx(chat_ctx)

    # Start reading Bee now; the coordinator waits a little for it before its first reply.
    context_task = asyncio.create_task(load_bee_context())
    if COORDINATOR_LLM != "echo":
        agent.context_task = context_task
    await session.start(room=ctx.room, agent=agent)
    await session.say(greeting(meta))


if __name__ == "__main__":
    cli.run_app(server)
