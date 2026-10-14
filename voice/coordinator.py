"""The coordinator: the voice the user talks to, its call rules and its tools.

It also tees the caller's audio and the AI's speech into the recorder.
Slow work goes to the worker through `delegate`; its news comes back as job notifications.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool, llm
from livekit.agents.llm import StopResponse
from livekit.agents.voice.generation import INSTRUCTIONS_MESSAGE_ID, update_instructions

from beelink import reads
from beelink.clock import now_text
from voice.language import CallLanguage
from voice.offers import OfferStore
from voice.recorder import CallRecorder
from voice.speech_filter import SpeechFilter
from voice.speech_gate import SpeechGate
from worker.runner import JobNotification, JobRunner, short

INSTRUCTIONS = """\
You are the voice of Bee Call. You phone the user to talk through their day and help them get one \
concrete thing done. You are speaking out loud on a phone call; the user hears you through AirPods.

How to speak:
1. Say one or two sentences at a time, short enough to say in one breath.
2. Lead with the conclusion. Give reasons only when asked.
3. List at most three items, and say how many first.
4. Never read out tables, code, links or symbols. Say it is in the app instead. Round numbers when you speak.
5. When something needs research, calculation or analysis, call delegate, say roughly how long it will take, \
and keep the conversation going while it runs.
6. Repeat back names and numbers that change the outcome, and ask whether you got them right.
7. If the user starts talking, stop and listen.
8. Save things to Bee (facts, todos) only after the user has said yes out loud.
9. When the user says goodbye or that they are done, say a short goodbye and call end_call.

Notes from the app arrive as system messages, as <instructions> blocks in a user turn, or as text in \
parentheses in a user turn, such as "(The phone call has connected.)": job notifications, tool results that came \
back late, and how much of your last reply the user actually heard. They come from the app, not from the user. \
Use them, but never read them out word for word.
The call prep note at the end of these instructions is what was looked up in Bee just before the call. Bring up \
things from it only when they connect to what the user is talking about, or once the user's own topic is done. \
Never interrupt the user's topic with them. For more from Bee during the call, use bee_today, bee_conversation \
and search_bee.
When the prep note lists unconfirmed Bee facts, check them at a natural moment, for example \
"I heard your bonus is 15 percent. Is that right?" If the user corrects it, queue a fact_update with the \
corrected text. If they agree, queue a fact_confirm.
If the user cut you off, reply to what they said. In the same reply, briefly add anything they missed that \
answers something they asked, unless they told you to drop it.
Background jobs: delegate starts a job and returns at once. When the job has news, the app sends a \
<job-notification> block with the job id, its status (running, completed or failed), the task, and a summary, an \
error or the files the job made. When a notification has a summary or an error, tell the user the key point in two \
or three short sentences (under 50 words) and say the details are in the app. A notification with only files needs \
no reply. Say a job is done, or give its result, only after its notification has arrived. When the user asks how \
a job is going, call job_status.
"""

BEE_WRITE_KINDS = ("fact_confirm", "fact_update", "todo_create", "todo_accept")
NOTE_WAIT_S = 2.0  # the first reply waits at most this long for a prep note still being written (arbitrary)


def job_notification(news: JobNotification) -> str:
    """The note for news about a background job, modeled on how Claude Code tells its model about a background
    task: a label, then a tagged block. What to do with it is in INSTRUCTIONS, not in each note."""
    lines = ["[App notification, not the caller speaking]", "<job-notification>", f"<job-id>{news.job_id}</job-id>",
             f"<status>{news.status}</status>", f"<task>{short(news.task)}</task>"]
    if news.summary:
        lines.append(f"<summary>{news.summary}</summary>")
    if news.error:
        lines.append(f"<error>{news.error}</error>")
    if news.files:
        lines.append(f"<files>{', '.join(news.files)}</files>")
    lines.append("</job-notification>")
    return "\n".join(lines)


class Coordinator(Agent):
    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path, *, now: datetime | None = None,
                 prep_note: str | None = None, pending_note: "asyncio.Future[str | None] | None" = None,
                 language: CallLanguage | None = None, earlier_call: str | None = None) -> None:
        # The date and the prep note go into the instructions before the call's first reply, and stay as they
        # are after it (Claude checks its earlier thinking against them). A note still being written when the
        # AI picks up (pending_note) replaces prep_note, the plain Bee context, if it arrives before that.
        rule = f"\n{language.rule}\n" if language and language.rule else ""
        # A call the user continued from an earlier call's record (voice/continuation.py).
        earlier = (f"\nThe user started this call to continue an earlier call with you. That call, as it was "
                   f"recorded:\n{earlier_call.strip()}\n") if earlier_call else ""
        self._base_instructions = INSTRUCTIONS + rule + earlier + f"\nIt is now {now_text(now)}. Use this for " \
                                                                  "words like today, tomorrow and this Saturday.\n"
        super().__init__(instructions=self._with_note(prep_note))
        self._recorder = recorder
        self._jobs = jobs
        self._workspace = workspace
        self._pending_note = pending_note
        self._note_open = pending_note is not None  # until the first reply
        self._note_task: asyncio.Task | None = None
        self._booked: str | None = None  # the call back booked in this call (book_call), if any

    def _with_note(self, note: str | None) -> str:
        if not note:
            return self._base_instructions
        return self._base_instructions + f"\nCall prep note (from Bee, just before the call):\n{note.strip()}\n"

    async def add_note(self, note: str) -> None:
        """Adds an app note to the conversation in turn order. The Claude adapter sends it as a system
        message after the next user message; for other models LiveKit's provider format keeps it in place
        as a user turn wrapped in <instructions> tags (convert_mid_conversation_instructions)."""
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(role="system", content=note)
        await self.update_chat_ctx(chat_ctx)

    async def on_enter(self) -> None:
        if self._pending_note is not None:
            self._note_task = asyncio.create_task(self._take_note_when_ready())

    async def _take_note_when_ready(self) -> None:
        await self._take_note(await asyncio.shield(self._pending_note))

    async def _take_note(self, note: str | None) -> None:
        if note and self._note_open:
            self._note_open = False
            await self.update_instructions(self._with_note(note))
            self._recorder.event("prep_note_taken")

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        pending = self._pending_note
        if not self._note_open or pending is None:
            return
        if not pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(pending), NOTE_WAIT_S)
            except asyncio.TimeoutError:
                self._recorder.event("prep_note_late", waited_s=NOTE_WAIT_S)
        if pending.done() and not pending.cancelled():
            await self._take_note(pending.result())
        self._note_open = False
        # This turn's context was copied from the agent before the note landed in its instructions.
        current = turn_ctx.get_by_id(INSTRUCTIONS_MESSAGE_ID)
        if current is not None and current.text_content != self.instructions:
            update_instructions(turn_ctx, instructions=self.instructions, add_if_missing=False)

    async def tell_job(self, news: JobNotification) -> bool:
        """Adds news about a background job to the conversation; True when the user should hear it now."""
        await self.add_note(job_notification(news))
        return news.needs_reply

    # ---------- speech ----------

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings):
        # Tool-call markup written as reply text is cut before it reaches the voice, the captions
        # and the history (see voice/speech_filter.py). Tool calls themselves pass through.
        speech = SpeechFilter()
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.content:
                kept = speech.feed(chunk.delta.content)
                if kept != chunk.delta.content:
                    chunk = chunk.model_copy(update={"delta": chunk.delta.model_copy(update={"content": kept})})
            elif isinstance(chunk, str):
                chunk = speech.feed(chunk)
            yield chunk
        tail = speech.flush()
        if tail:
            yield tail
        if speech.dropped:
            self._recorder.event("speech_markup_dropped", text=speech.dropped)

    # ---------- recording ----------

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings):
        # A transcript too quiet to be the user (the AI's own voice coming back faintly) is kept out of the
        # conversation and noted in the call record (voice/speech_gate.py).
        gate = SpeechGate(on_drop=lambda text, level: self._recorder.event("transcript_dropped", text=text,
                                                                           level=round(level)))

        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in audio:
                self._recorder.user_audio.write(frame)
                yield frame

        async for event in Agent.default.stt_node(self, gate.listen(tee()), model_settings):
            if gate.allows(event):
                yield event

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in Agent.default.tts_node(self, text, model_settings):
                self._recorder.ai_audio.write(frame)
                yield frame

        return tee()

    # ---------- tools ----------

    @function_tool
    async def delegate(self, context: RunContext, task: str) -> str:
        """Hand slow work (research, calculation, analysis) to the background worker. Returns at once.

        A started job cannot be changed. For more or different work, call delegate again.

        Args:
            task: A self-contained description of the work and the result to bring back.
        """
        job = self._jobs.start(task)
        return (f"Job {job.job_id} is running in the background. A job notification will come when it has news. "
                "If you already told the user you are on it, do not say it again; carry on the conversation.")

    @function_tool
    async def job_status(self, context: RunContext) -> str:
        """See how this call's background jobs are going: the latest step of each running job, or how it ended."""
        return self._jobs.status_text()

    @function_tool
    async def search_workspace(self, context: RunContext, query: str) -> str:
        """Look up what the user and you said in earlier Bee Call phone calls.

        Not for the user's conversations with other people; use search_bee for those.

        Args:
            query: A few keywords to look for.
        """
        words = [w.lower() for w in query.split() if len(w) > 2]
        hits = []
        for transcript in sorted(self._workspace.glob("*/*/transcript.md"), reverse=True):
            if transcript.parent == self._recorder.dir:
                continue
            for line in transcript.read_text().splitlines():
                if words and all(w in line.lower() for w in words):
                    hits.append(f"{transcript.parent.parent.name} {transcript.parent.name}: {line.strip('- ')}")
                    if len(hits) >= 5:
                        return "\n".join(hits)
        return "\n".join(hits) or "Nothing found in earlier calls."

    @function_tool
    async def search_bee(self, context: RunContext, query: str) -> str:
        """Search, by meaning, the user's conversations with other people that their Bee wearable recorded.

        Use it when the user asks what they discussed, planned or agreed with someone.

        Args:
            query: What to look for, in plain words.
        """
        return await reads.search(query)

    @function_tool
    async def bee_today(self, context: RunContext) -> str:
        """What the user's Bee wearable has for today: the daily summary, active todos, notes and recent conversations."""
        return await reads.today()

    @function_tool
    async def bee_conversation(self, context: RunContext, conversation_id: int) -> str:
        """One conversation the Bee wearable recorded: its summary and its last lines.

        Args:
            conversation_id: The Bee conversation id, from the prep note, bee_today or search_bee.
        """
        return await reads.conversation(conversation_id)

    @function_tool
    async def queue_bee_write(self, context: RunContext, kind: str, text: str | None = None,
                              fact_id: int | None = None, suggestion_id: int | None = None,
                              alarm_at: str | None = None) -> str:
        """Queue something the user agreed to out loud, to be saved to Bee after the call.

        Args:
            kind: One of fact_confirm, fact_update, todo_create, todo_accept.
            text: The fact or todo text (fact_update, todo_create).
            fact_id: The Bee fact id (fact_confirm, fact_update).
            suggestion_id: The Bee todo suggestion id (todo_accept).
            alarm_at: Optional ISO 8601 time for a todo alarm (todo_create).
        """
        if kind not in BEE_WRITE_KINDS:
            return f"Unknown kind {kind!r}; use one of {', '.join(BEE_WRITE_KINDS)}."
        item: dict = {"kind": kind}
        if kind in ("fact_confirm", "fact_update"):
            if fact_id is None:
                return "fact_id is required."
            item["fact_id"] = fact_id
        if kind in ("fact_update", "todo_create"):
            if not text:
                return "text is required."
            item["text"] = text
        if kind == "todo_create" and alarm_at:
            item["alarm_at"] = alarm_at
        if kind == "todo_accept":
            if suggestion_id is None:
                return "suggestion_id is required."
            item["suggestion_id"] = suggestion_id
        with (self._recorder.dir / "bee-queue.jsonl").open("a") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        self._recorder.event("bee_write_queued", kind=kind, text=text or "", item=item)
        return "Queued. It will be saved to Bee after the call."

    @function_tool
    async def book_call(self, context: RunContext, at: str, topic: str) -> str:
        """Book a call back at a time the user asked for or agreed to out loud, such as "call me tomorrow at 8".
        At that time you call them, picking up from this call. Never book a call the user did not ask for.

        Args:
            at: The local date and time to call, in ISO 8601, like 2026-10-15T08:00.
            topic: What the call is for, in a few words.
        """
        try:
            when = datetime.fromisoformat(at.strip())
        except ValueError:
            return "Give the time in ISO 8601, like 2026-10-15T08:00."
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)  # this Mac's local time, like the rest of the workspace
        now = datetime.now()
        if when < now + timedelta(minutes=1):
            return "That time has passed. Ask the user for a later time."
        if when > now + timedelta(days=7):
            return "Calls can be booked at most a week ahead."
        store = OfferStore(self._workspace)
        moved = self._booked is not None and store.mark(self._booked, "declined", now, error="moved to a new time")
        session = f"{self._recorder.dir.parent.name}/{self._recorder.dir.name}"  # the booked call continues this one
        offer = store.book(topic.strip() or "a call back", when, now, continue_from=session)
        self._booked = offer.id  # one call back per call: booking again moves it
        self._recorder.event("call_booked", offer=offer.id, at=offer.call_at, topic=offer.topic)
        verb = "Moved the call back" if moved else "Booked"
        return f"{verb}: you will call at {when:%a %b %-d, %-I:%M %p}. Confirm it to the user in a few words."

    @function_tool
    async def cancel_call(self, context: RunContext) -> str:
        """Call off the call back booked in this call, when the user no longer wants it."""
        offer = OfferStore(self._workspace).get(self._booked) if self._booked else None
        if offer is None or offer.state != "booked":
            return "No call back is booked in this call."
        OfferStore(self._workspace).mark(offer.id, "declined", datetime.now(), error="called off in the call")
        self._booked = None
        self._recorder.event("call_cancelled", offer=offer.id)
        return "Called off. Tell the user in a few words."

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """End the call. Say goodbye first."""
        self._recorder.event("end_call_requested")
        self.session.shutdown(drain=True)
        # No reply to this tool: the goodbye is already said, and GLM 5 once answered the tool result
        # by saying "end_call" out loud (2026-10-13).
        raise StopResponse()
