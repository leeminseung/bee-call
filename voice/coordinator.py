"""The coordinator: the voice the user talks to, its call rules and its tools.

It also tees the caller's audio and the AI's speech into the recorder.
Slow work goes to the worker through `delegate`; its news comes back as job notifications.

Coordinator is the voice for a person without Bee (Use Bee off): it has no Bee tools and its instructions say
nothing about Bee. BeeCoordinator, for a person who connected Bee, adds Bee's reads and writes and the rules for
them. The voice server picks one by the person's setting when the call starts (voice/agent.py).
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterable, Callable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool, llm
from livekit.agents.llm import StopResponse
from livekit.agents.voice.generation import INSTRUCTIONS_MESSAGE_ID, update_instructions

from beelink import reads
from beelink.clock import now_text
from beelink.writer import KINDS, BeeWrites, item_label, label
from voice.language import CallLanguage, call_language
from voice.offers import OfferStore
from voice.recorder import CallRecorder
from voice.speech_filter import SpeechFilter
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
8. When the user says goodbye or that they are done, say a short goodbye and call end_call.

Notes from the app arrive as system messages, as <instructions> blocks in a user turn, or as text in \
parentheses in a user turn, such as "(The phone call has connected.)": job notifications, tool results that came \
back late, and how much of your last reply the user actually heard. They come from the app, not from the user. \
Use them, but never read them out word for word.
If the user cut you off, reply to what they said. In the same reply, briefly add anything they missed that \
answers something they asked, unless they told you to drop it.
Background jobs: delegate starts a job and returns at once. When the job has news, the app sends a \
<job-notification> block with the job id, its status (running, completed or failed), the task, and a summary, an \
error or the files the job made. When a notification has a summary or an error, tell the user the key point in two \
or three short sentences (under 50 words) and say the details are in the app. A notification with only files needs \
no reply. Say a job is done, or give its result, only after its notification has arrived. When the user asks how \
a job is going, call job_status.
"""
# How to use the call prep note, for a person without Bee: it comes from earlier calls and the notes kept between
# calls (meta/agent.py).
NOTE_RULES = """\
The call prep note at the end of these instructions is what you noted just before the call, from your past \
calls with the user and the notes kept between calls. Bring up things from it only when they connect to what the \
user is talking about, or once the user's own topic is done. Never interrupt the user's topic with them. Ask about \
the user's day yourself; what they tell you is all you know of it.
"""
# For a person who connected Bee: the note is from Bee, and the call can read and save Bee.
BEE_NOTE_RULES = """\
The user wears Bee, a wearable that records and summarizes their conversations. The call prep note at the end of \
these instructions is what was looked up in Bee just before the call. Bring up things from it only when they \
connect to what the user is talking about, or once the user's own topic is done. Never interrupt the user's topic \
with them. For more from Bee during the call, use bee_today, bee_conversation and search_bee.
Saving to Bee happens right away, in the call, in three steps. First call propose_bee_save with the item, \
written in the user's language. Then ask one question that names Bee and the item, such as "Shall I add 'note \
the bug' to your Bee todos?" (write Bee as "Bee" in every language, as in "Bee에 할 일로 '버그 기록'을 \
넣을까요?"). Only after a clear yes to that question, call save_to_bee with the number propose_bee_save gave, then \
tell the user in a few words what you saved. A yes to any other question, or one given before you proposed the \
item, is no yes to saving. If the user asks you to take it out, call undo_bee_write. If saving does not go \
through, say what save_to_bee tells you to say.
When the prep note lists unconfirmed Bee facts, check them at a natural moment: propose a fact_confirm, then ask, \
naming Bee, for example "Bee heard that your bonus is 15 percent. Is that right?" If the user agrees, save it. If \
they correct it, propose a fact_update with the corrected text, ask whether to fix it in Bee, and after a yes save \
it.
"""

NOTE_WAIT_S = 2.0  # the first reply waits at most this long for a prep note still being written (arbitrary)
HANGUL = re.compile(r"[\uac00-\ud7a3]")
QUESTION = re.compile(r"[?？]")
ENGLISH = call_language("en")  # what the tools tell the model is in English, like the rest of its instructions


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
    """The voice for a person without Bee: no Bee tools, and nothing about Bee in its instructions."""

    NOTE_RULES = NOTE_RULES
    NOTE_HEADER = "Call prep note (written just before the call):"

    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path, *, clock: Callable[[], datetime],
                 prep_note: str | None = None, pending_note: "asyncio.Future[str | None] | None" = None,
                 language: CallLanguage | None = None, earlier_call: str | None = None) -> None:
        # `clock` is the person's clock (Person.now): the time the call starts goes into the instructions, and
        # book_call reads it again when the user books.
        # The date and the prep note go into the instructions before the call's first reply, and stay as they
        # are after it (Claude checks its earlier thinking against them). A note still being written when the
        # AI picks up (pending_note) replaces prep_note, the plain Bee context, if it arrives before that.
        self._clock = clock
        rule = f"\n{language.rule}\n" if language and language.rule else ""
        # A call the user continued from an earlier call's record (voice/continuation.py).
        earlier = (f"\nThe user started this call to continue an earlier call with you. That call, as it was "
                   f"recorded:\n{earlier_call.strip()}\n") if earlier_call else ""
        self._base_instructions = INSTRUCTIONS + self.NOTE_RULES + rule + earlier + \
            f"\nIt is now {now_text(clock())}. Use this for words like today, tomorrow and this Saturday.\n"
        super().__init__(instructions=self._with_note(prep_note))
        self._recorder = recorder
        self._jobs = jobs
        self._workspace = workspace
        self._language = language or call_language()
        self._pending_note = pending_note
        self._note_open = pending_note is not None  # until the first reply
        self._note_task: asyncio.Task | None = None
        self._booked: str | None = None  # the call back booked in this call (book_call), if any

    def _with_note(self, note: str | None) -> str:
        if not note:
            return self._base_instructions
        return self._base_instructions + f"\n{self.NOTE_HEADER}\n{note.strip()}\n"

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
        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in audio:
                self._recorder.user_audio.write(frame)
                yield frame

        return Agent.default.stt_node(self, tee(), model_settings)

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
    async def book_call(self, context: RunContext, topic: str, at: str | None = None,
                        in_minutes: int | None = None) -> str:
        """Book a call back at a time the user asked for or agreed to out loud, such as "call me tomorrow at 8" or
        "call me back in ten minutes". At that time you call them, picking up from this call. Never book a call
        the user did not ask for. Give the time one way: in_minutes for a time counted from now, at for a clock
        time. The answer says the time booked; read it back to the user.

        Args:
            topic: What the call is for, in a few words.
            at: For a clock time: the local date and time to call, in ISO 8601, like 2026-10-15T08:00.
            in_minutes: For a time from now, like "in ten minutes" (10) or "in an hour" (60): minutes from now.
        """
        now = self._clock()  # the person's clock now, not the time this call started
        if (at is None) == (in_minutes is None):
            return "Give the time one way: in_minutes for a time from now, or at for a clock time."
        if in_minutes is not None:
            when = now + timedelta(minutes=in_minutes)
        else:
            try:
                when = datetime.fromisoformat(at.strip())
            except ValueError:
                return "Give the time in ISO 8601, like 2026-10-15T08:00."
            # A time without a zone is the user's own; one with a zone is the same moment on their clock.
            when = when.replace(tzinfo=now.tzinfo) if when.tzinfo is None else when.astimezone(now.tzinfo)
        if when < now + timedelta(minutes=1):
            return "That time has passed. Ask the user for a later time."
        if when > now + timedelta(days=7):
            return "Calls can be booked at most a week ahead."
        store = OfferStore(self._workspace)
        moved = self._booked is not None and store.call_off(self._booked, now, "moved to a new time") is not None
        session = f"{self._recorder.dir.parent.name}/{self._recorder.dir.name}"  # the booked call continues this one
        offer = store.book(topic.strip() or "a call back", when, now, continue_from=session)
        self._booked = offer.id  # one call back per call: booking again moves it
        self._recorder.event("call_booked", offer=offer.id, at=offer.call_at, topic=offer.topic)
        verb = "Moved the call back" if moved else "Booked"
        day = "today" if when.date() == now.date() else "tomorrow" if when.date() == now.date() + timedelta(days=1) \
            else f"on {when:%A}"
        return (f"{verb}: you will call at {when:%-I:%M %p} {day} ({when:%A, %B %-d}); it is {now:%-I:%M %p} now. "
                "Read this time back to the user in a few words.")

    @function_tool
    async def cancel_call(self, context: RunContext) -> str:
        """Call off the call back booked in this call, when the user no longer wants it."""
        offer = OfferStore(self._workspace).call_off(self._booked, self._clock(), "called off in the call") \
            if self._booked else None
        if offer is None:
            return "No call back is booked in this call."
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


@dataclass
class Proposal:
    """An item the AI means to ask about saving to Bee. The user's yes counts only if it comes after this point in
    the conversation (the id of the user's last turn when it was proposed; None: before they spoke)."""
    item: dict
    after: str | None
    saved: bool = False


class BeeCoordinator(Coordinator):
    """The voice for a person who connected Bee: it reads their Bee during the call and saves to it what the user
    agrees to."""

    NOTE_RULES = BEE_NOTE_RULES
    NOTE_HEADER = "Call prep note (from Bee, just before the call):"

    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path, **kwargs) -> None:
        super().__init__(recorder, jobs, workspace, **kwargs)
        self._bee_writes = BeeWrites(recorder.dir)  # this call's Bee writes, kept in its folder
        self._proposals: dict[int, Proposal] = {}  # items the AI means to ask about, by number (propose_bee_save)

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
        return await reads.today(self._clock().tzinfo)

    @function_tool
    async def bee_conversation(self, context: RunContext, conversation_id: int) -> str:
        """One conversation the Bee wearable recorded: its summary and its last lines.

        Args:
            conversation_id: The Bee conversation id, from the prep note, bee_today or search_bee.
        """
        return await reads.conversation(conversation_id, self._clock().tzinfo)

    @function_tool
    async def propose_bee_save(self, context: RunContext, kind: str, text: str | None = None,
                               fact_id: int | None = None, suggestion_id: int | None = None,
                               alarm_at: str | None = None) -> str:
        """Before you ask the user whether to save something to their Bee: fixes the item and gives it a number.
        Then ask one question that names Bee and the item, and call save_to_bee with the number only after the
        user says yes to it.

        Args:
            kind: One of fact_confirm, fact_update, todo_create, todo_accept.
            text: The todo or fact as it reads, in the user's language (needed for fact_update and todo_create).
            fact_id: The Bee fact id (fact_confirm, fact_update).
            suggestion_id: The Bee todo suggestion id (todo_accept).
            alarm_at: Optional ISO 8601 time for a todo alarm (todo_create).
        """
        if kind not in KINDS:
            return f"Unknown kind {kind!r}; use one of {', '.join(KINDS)}."
        item: dict = {"kind": kind}
        if kind in ("fact_confirm", "fact_update"):
            if fact_id is None:
                return "fact_id is required."
            item["fact_id"] = fact_id
        if kind in ("fact_update", "todo_create") and not text:
            return "text is required."
        if text:
            if self._language.code == "ko" and not HANGUL.search(text):
                return "Write the text in Korean, the user's language, then propose it again."
            item["text"] = text.strip()
        if kind == "todo_create" and alarm_at:
            item["alarm_at"] = alarm_at
        if kind == "todo_accept":
            if suggestion_id is None:
                return "suggestion_id is required."
            item["suggestion_id"] = suggestion_id
        number = len(self._proposals) + 1
        self._proposals[number] = Proposal(item, after=self._last_user_turn())
        return (f"Proposal {number}: {item_label(item, ENGLISH)}. Ask the user now, in one question that names Bee "
                "and the item, whether to save it (unless you already asked it in this reply). Call save_to_bee with "
                f"proposal {number} only after they say yes to that question.")

    @function_tool
    async def save_to_bee(self, context: RunContext, proposal: int) -> str:
        """Save a proposed item to the user's Bee now, after the user said yes to your question about it.

        Args:
            proposal: The number propose_bee_save gave the item.
        """
        found = self._proposals.get(proposal)
        if found is None:
            return f"There is no proposal {proposal}; call propose_bee_save first."
        if found.saved:
            return f"Proposal {proposal} was already saved; it is not saved twice."
        question, answer = self._answer_to(found)
        if question is None or answer is None:
            return ("Not saved: no yes to it yet. Ask the user now, in one question that names Bee and the item, "
                    "such as \"Shall I add 'note the bug' to your Bee todos?\", and call save_to_bee again only after "
                    "they say yes to that question.")
        found.saved = True
        write = await self._bee_writes.write(found.item, question, answer)
        what = label(write, ENGLISH)
        self._recorder.event("bee_write", key=write.key, kind=write.kind, text=write.text, item=write.item,
                             question=question, answer=answer, result="ok" if write.status == "ok" else "failed",
                             error=write.error)
        if write.status == "ok":
            return f"Saved to Bee: {what}. Tell the user in a few words what you saved."
        return (f"Saving {what} to Bee did not go through ({write.error}). Tell the user it did not go through and "
                "that it will be tried again after the call.")

    @function_tool
    async def undo_bee_write(self, context: RunContext) -> str:
        """Take the last item you saved to Bee in this call back out, when the user asks you to."""
        saved = [w for w in self._bee_writes.all() if w.status == "ok"]
        if not saved:
            return "Nothing saved to Bee in this call is left to take out."
        write = await self._bee_writes.undo(saved[-1].key)
        what = label(write, ENGLISH)
        if write.status != "undone":
            return f"Could not take {what} out of Bee ({write.error}). Tell the user."
        self._recorder.event("bee_write_undone", key=write.key, kind=write.kind, text=write.text)
        return f"Took it out of Bee: {what}. Tell the user in a few words."

    def _last_user_turn(self) -> str | None:
        """The id of the user's last turn so far (None before they said anything)."""
        users = [m.id for m in self._turns() if m.role == "user"]
        return users[-1] if users else None

    def _turns(self) -> list:
        return [item for item in self.chat_ctx.items
                if getattr(item, "type", None) == "message" and item.role in ("user", "assistant")]

    def _answer_to(self, proposal: Proposal) -> tuple[str | None, str | None]:
        """The question the AI asked about a proposed item and the user's answer, as the conversation shows them:
        the user's last turn, if it came after the item was proposed, and the AI's turn right before that turn, if it
        too came after the proposal, names Bee and asks ("?"). (None, None) when there is no such answer yet: the
        AI saved in the reply that asked, or the yes was to another question or came before the item was fixed."""
        turns = self._turns()
        ids = [turn.id for turn in turns]
        if proposal.after is None:
            later = turns
        elif proposal.after in ids:
            later = turns[ids.index(proposal.after) + 1:]
        else:
            return None, None  # the turn it followed is no longer in the conversation: ask again
        users = [i for i, turn in enumerate(later) if turn.role == "user"]
        if not users:
            return None, None
        asked = [turn for turn in later[:users[-1]] if turn.role == "assistant"]
        if not asked:
            return None, None
        question = asked[-1].text_content or ""
        if "bee" not in question.lower() or not QUESTION.search(question):
            return None, None
        return question, later[users[-1]].text_content
