"""The coordinator: the voice the user talks to, its call rules and its tools.

It also tees the caller's audio and the AI's speech into the recorder.
Slow work goes to the worker through `delegate`; its news comes back as job notifications.

Coordinator is the voice for a person without Bee (Use Bee off): it has no Bee tools and its instructions say
nothing about Bee. BeeCoordinator, for a person who connected Bee, adds Bee's reads and writes and the rules for
them. The voice server picks one by the person's setting when the call starts (voice/agent.py).
"""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterable, Callable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool, llm
from livekit.agents.llm import StopResponse
from livekit.agents.voice.generation import INSTRUCTIONS_MESSAGE_ID, update_instructions

from beelink import reads
from beelink.calls import OurCalls
from beelink.clock import now_text
from beelink.writer import KINDS, BeeWrites, event_fields, item_label, label
from voice.language import CallLanguage, call_language
from voice.offers import OfferStore
from voice.recorder import CallRecorder
from voice.audio_tags import SPOKEN, without_tags
from voice.speech_filter import SpeechFilter
from voice.speech_gate import SpeechGate
from worker.runner import JobNotification, JobRunner, short

# How the call AI may write sounds (voice/audio_tags.py): no tags at all, or, when its voice performs them, a few
# ElevenLabs audio tags used sparingly. One of the "instructions for speech that sounds real" to tune by ear.
SPEECH_RULE = ('9. Every word you write is spoken aloud, so never write stage directions, sound effects or '
               'descriptions of the situation, in parentheses, brackets or asterisks, such as "(pauses)", "[laughs]" '
               'or "*sighs*".')
SPEECH_RULE_WITH_TAGS = (
    '9. Every word you write is spoken aloud, so never write stage directions or descriptions of the situation, in '
    'parentheses, brackets or asterisks, such as "(pauses)" or "*smiles*". Your voice can make a few sounds, written '
    'exactly as these tags: ' + ", ".join(f"[{tag}]" for tag in SPOKEN) + '. Use them sparingly, only where a '
    'person on the phone would make that sound: at most one in a reply, and most replies have none. Put a tag where '
    'the sound happens, as in "[laughs] That\'s a good one."')

INSTRUCTIONS_TEMPLATE = """\
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
{speech_rule}

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


def instructions(audio_tags: bool = False) -> str:
    """The call AI's instructions; with `audio_tags` (its voice performs them) it may write a few sounds as tags."""
    return INSTRUCTIONS_TEMPLATE.replace("{speech_rule}", SPEECH_RULE_WITH_TAGS if audio_tags else SPEECH_RULE)


INSTRUCTIONS = instructions()  # for a voice that performs no tags: Polly, or ElevenLabs before v4
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
written in the user's language. Then ask one question that names Bee and, in the question itself, quotes the \
item exactly as you proposed it, such as "Shall I add 'Note the bug' to your Bee todos?" (write Bee as "Bee" in \
every language, as in "Bee에 할 일로 '버그 기록'을 넣을까요?"). Only after a clear yes to that question, call \
save_to_bee with the number propose_bee_save gave, then tell the user in a few words what you saved. A yes to any \
other question, or one given before you proposed the item, is no yes to saving; only the answer to your first \
question about an item counts, so to ask again, propose it again first. If the user asks you to take it out, call \
undo_bee_write. If saving does not go through, say what save_to_bee tells you to say.
When the prep note lists unconfirmed Bee facts, check them at a natural moment: propose a fact_confirm with the \
fact as Bee has it, then ask, quoting it in the question itself, for example "Did Bee hear 'Bonus is 15%' \
right?" If the user agrees, save it. If they correct it, propose a fact_update with the corrected text, ask \
whether to fix it in Bee, quoting the new text, and after a yes save it.
"""

NOTE_WAIT_S = 2.0  # the first reply waits at most this long for a prep note still being written (arbitrary)
# A tool the call AI waits on (anything outside the call: Bee today) says the language's waiting line once the
# call has been silent this long, so the caller does not think it dropped (Matt, 2026-10-15). One rule for
# every such tool; how long they may wait at most is beelink/cli.py CALL_WAIT_S.
WAITING_LINE_AFTER_S = 2.0
HANGUL = re.compile(r"[\uac00-\ud7a3]")
QUESTION = re.compile(r"[?？]")
NAMES_BEE = re.compile(r"(?<![A-Za-z])Bee(?![a-z]| Call)")  # "Bee", "Bee에"; not "been", "beer" or "Bee Call"
# A sentence of the answer that starts by saying no. Such an answer never counts as a yes to saving, whatever the
# AI makes of it; it may be a partial yes ("No, just the milk."), so the AI asks about the item alone.
REFUSAL = re.compile(r"^\W*(?:no\b|nope\b|nah\b|don'?t\b|do not\b|not now\b|never ?mind\b|wait,? no\b|"
                     r"아니|아뇨|됐어|됐습니다|싫어|하지 ?마|넣지 ?마|필요 ?없)", re.IGNORECASE)
# The answer's first sentence has to start by saying yes (after a filler such as "음" or "well"); a hedge, a
# "later", or the AI's own question heard back is no yes (pre-deploy review, 2026-10-15).
AFFIRMATIVE = re.compile(r"^\W*(?:(?:음|어|아|그|저|um|uh|hmm|well|oh)\W+)?"
                         r"(?:yes\b|yeah\b|yep\b|yup\b|sure\b|ok\b|okay\b|please\b|go ahead\b|go for it\b|do it\b|"
                         r"absolutely\b|of course\b|sounds good\b|that'?s right\b|right\b|correct\b|"
                         r"네|넵|넹|예|응|그래|그럼|좋아|좋습니다|좋네요|넣어|해 ?줘|해 ?주세요|맞아|맞습니다|그렇게|부탁|"
                         r"물론|당연|오케이)", re.IGNORECASE)
SENTENCES = re.compile(r"(?<=[.!?。])\s+|\n")
QUOTES = "'\"‘’“”「」『』«»"
EXPIRED = "expired"  # a proposal whose question was answered and passed (BeeCoordinator._answer_to)
# How to ask about each kind of item; the item is quoted as proposed, and the AI says the question in the user's
# language.
ASK_LIKE = {"todo_create": "Shall I add '{text}' to your Bee todos?",
            "todo_accept": "Shall I add Bee's suggested todo '{text}'?",
            "fact_confirm": "Did Bee hear '{text}' right?",
            "fact_update": "Shall I fix it in Bee to '{text}'?"}
ENGLISH = call_language("en")  # what the tools tell the model is in English, like the rest of its instructions


def model_request(agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool]) -> dict[str, Any]:
    """What one request gives the model, model-agnostic: the whole chat as LiveKit holds it (the instructions are
    its first item), and each tool offered with its parameters. Never raises."""
    try:
        return _model_request(agent, chat_ctx, tools)
    except Exception as exc:
        return {"error": f"could not record the request: {exc!r}"}


def model_name(agent: Agent) -> str | None:
    """The model answering the agent's requests (with GLM 5 standing by, the one that took the call)."""
    try:
        return getattr(agent.session.llm, "model", None)
    except RuntimeError:  # no session (a direct call, as in tests)
        return None


def _model_request(agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool]) -> dict[str, Any]:
    model = model_name(agent)
    try:
        offered = [{"name": t["function"]["name"], "description": t["function"].get("description"),
                    "parameters": t["function"].get("parameters")}
                   for t in llm.ToolContext(tools).parse_function_tools("openai", strict=False)]
    except Exception as exc:  # a tool LiveKit cannot describe: its name at least
        offered = [{"name": getattr(getattr(t, "info", None), "name", repr(t)), "error": repr(exc)} for t in tools]
    return {"model": model, "chat": chat_ctx.to_dict(exclude_image=True, exclude_audio=True,
                                                       exclude_timestamp=False)["items"], "tools": offered}


def keep_reply(reply: dict[str, Any], chunk: Any) -> None:
    """Adds a streamed chunk of the model's answer to `reply`: its text and its tool calls."""
    if isinstance(chunk, str):
        reply["text"] += chunk
    elif isinstance(chunk, llm.ChatChunk) and chunk.delta:
        reply["text"] += chunk.delta.content or ""
        for call in chunk.delta.tool_calls or []:
            reply["tool_calls"].append({"name": call.name, "arguments": call.arguments, "call_id": call.call_id})
        if chunk.usage:
            reply["usage"] = chunk.usage.model_dump(mode="json")


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
                 language: CallLanguage | None = None, earlier_call: str | None = None,
                 direction: str = "inbound", audio_tags: bool = False) -> None:
        # `clock` is the person's clock (Person.now): the time the call starts goes into the instructions, and
        # book_call reads it again when the user books.
        # The date and the prep note go into the instructions before the call's first reply, and stay as they
        # are after it (Claude checks its earlier thinking against them). A note still being written when the
        # AI picks up (pending_note) replaces prep_note, the plain Bee context, if it arrives before that.
        self._clock = clock
        rule = f"\n{language.rule}\n" if language and language.rule else ""
        # A call that continues an earlier one (voice/continuation.py): the user started it from that call's record,
        # or the AI places it because the user booked it in that call (BC-042).
        how = ("You placed this call, as the user booked it in an earlier call with you; it continues that call"
               if direction == "outbound" else "The user started this call to continue an earlier call with you")
        earlier = (f"\n{how}. That call, as it was recorded:\n{earlier_call.strip()}\n") if earlier_call else ""
        # `audio_tags`: the call's voice performs ElevenLabs audio tags such as [laughs] (voice/audio_tags.py).
        self._audio_tags = audio_tags
        self._base_instructions = instructions(audio_tags) + self.NOTE_RULES + rule + earlier + \
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
        # Every request, whatever the model, goes into the call's model log as the model received it, with what
        # came back before the filter (voice/recorder.py MODEL_LOG).
        sent = model_request(self, chat_ctx, tools)
        reply: dict[str, Any] = {"text": "", "tool_calls": []}
        ended = "error"
        speech = SpeechFilter()
        try:
            async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
                keep_reply(reply, chunk)
                if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.content:
                    kept = speech.feed(chunk.delta.content)
                    if kept != chunk.delta.content:
                        chunk = chunk.model_copy(update={"delta": chunk.delta.model_copy(update={"content": kept})})
                elif isinstance(chunk, str):
                    chunk = speech.feed(chunk)
                yield chunk
            ended = "done"
        except (GeneratorExit, asyncio.CancelledError):
            ended = "cut off"  # the user spoke, or the call ended
            raise
        except Exception as exc:
            ended = f"error: {exc!r}"[:500]
            raise
        finally:
            # The model is named once the reply is in: the first request of a call can go to GLM 5 instead.
            self._recorder.model_request({**sent, "model": model_name(self), "reply": reply, "ended": ended})
        tail = speech.flush()
        if tail:
            yield tail
        if speech.dropped:
            self._recorder.event("speech_markup_dropped", text=speech.dropped)

    # ---------- recording ----------

    @asynccontextmanager
    async def waiting(self, context: RunContext | None):
        """Around what a tool waits on outside the call: LiveKit says the language's waiting line once the call has
        been silent for WAITING_LINE_AFTER_S (RunContext.with_filler)."""
        if context is None:  # a tool called directly, as in tests
            yield
            return
        async with context.with_filler(self._language.waiting, delay=WAITING_LINE_AFTER_S):
            yield

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
        if not self._audio_tags:  # a voice that would read a tag out never gets one (voice/audio_tags.py)
            text = without_tags(text)

        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in Agent.default.tts_node(self, text, model_settings):
                self._recorder.ai_audio.write(frame)
                yield frame

        return tee()

    def transcription_node(self, text: AsyncIterable[str], model_settings: ModelSettings) -> AsyncIterable[str]:
        # The captions never show an audio tag, and neither do the conversation's history and the call record, which
        # LiveKit fills from this same text; the model log keeps the reply as the model wrote it.
        return without_tags(text)

    # ---------- tools ----------

    @function_tool
    async def delegate(self, context: RunContext, task: str, title: str) -> str:
        """Hand slow work (research, calculation, analysis) to the background worker. Returns at once.

        A started job cannot be changed. For more or different work, call delegate again.

        Args:
            task: A self-contained description of the work and the result to bring back.
            title: A few words naming the job for the user's screen, in the language you speak with the user, such
                as "Saturday rain in Seoul by the hour".
        """
        job = self._jobs.start(task, title=" ".join(title.split()) or None, language=self._language.code)
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
        offer = store.book(topic.strip() or "a call back", when, now, continue_from=session, language=self._language)
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
    the conversation (the id of the user's last turn when it was proposed; None: before they spoke), and only as
    the answer to the first question about it; after that answer has passed, the proposal has expired."""
    item: dict
    after: str | None
    saved: bool = False
    expired: bool = False


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
        async with self.waiting(context):
            return await reads.search(query, ours=OurCalls.of(self._workspace))

    @function_tool
    async def bee_today(self, context: RunContext) -> str:
        """What the user's Bee wearable has for today: the daily summary, active todos, notes and recent conversations."""
        async with self.waiting(context):
            return await reads.today(self._clock().tzinfo, ours=OurCalls.of(self._workspace))

    @function_tool
    async def bee_conversation(self, context: RunContext, conversation_id: int) -> str:
        """One conversation the Bee wearable recorded: its summary and its last lines.

        Args:
            conversation_id: The Bee conversation id, from the prep note, bee_today or search_bee.
        """
        async with self.waiting(context):
            return await reads.conversation(conversation_id, self._clock().tzinfo, ours=OurCalls.of(self._workspace))

    @function_tool
    async def propose_bee_save(self, context: RunContext, kind: str, text: str, fact_id: int | None = None,
                               suggestion_id: int | None = None, alarm_at: str | None = None) -> str:
        """Before you ask the user whether to save something to their Bee: fixes the item and gives it a number.
        Then ask one question that names Bee and quotes the item, and call save_to_bee with the number only after
        the user says yes to it.

        Args:
            kind: One of fact_confirm, fact_update, todo_create, todo_accept.
            text: The item as your question will quote it: the todo, or the fact as it should read, in the user's
                language; for fact_confirm and todo_accept, the fact or the suggested todo as Bee has it.
            fact_id: The Bee fact id (fact_confirm, fact_update).
            suggestion_id: The Bee todo suggestion id (todo_accept).
            alarm_at: Optional ISO 8601 time for a todo alarm (todo_create).
        """
        if kind not in KINDS:
            return f"Unknown kind {kind!r}; use one of {', '.join(KINDS)}."
        if not text or not text.strip():
            return "text is required: the item as your question will quote it."
        if kind in ("fact_update", "todo_create") and self._language.code == "ko" and not HANGUL.search(text):
            return "Write the text in Korean, the user's language, then propose it again."
        item: dict = {"kind": kind, "text": text.strip()}
        if kind in ("fact_confirm", "fact_update"):
            if fact_id is None:
                return "fact_id is required."
            item["fact_id"] = fact_id
        if kind == "todo_create" and alarm_at:
            item["alarm_at"] = alarm_at
        if kind == "todo_accept":
            if suggestion_id is None:
                return "suggestion_id is required."
            item["suggestion_id"] = suggestion_id
        number = len(self._proposals) + 1
        self._proposals[number] = Proposal(item, after=self._last_user_turn())
        return (f"Proposal {number}: {item_label(item, ENGLISH)}. Ask the user now (unless you already asked it in "
                f"this reply), in one question that names Bee and quotes the item exactly, such as: "
                f"{ASK_LIKE[kind].format(text=item['text'])} Call save_to_bee with proposal {number} only after they "
                "say yes to that question.")

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
        heard = EXPIRED if found.expired else self._answer_to(found)
        if heard is None:
            return (f"Not saved: the user has not yet said yes to a question about proposal {proposal}. Ask now, in "
                    "one question that names Bee and quotes the item exactly, such as: "
                    f"{ASK_LIKE[found.item['kind']].format(text=found.item['text'])} Call save_to_bee again only "
                    "after they say yes to it.")
        if heard == EXPIRED:
            found.expired = True
            return (f"Not saved: proposal {proposal} was asked about, and the conversation went on past the answer. If "
                    "the user still wants it, propose it again with propose_bee_save and ask about it alone.")
        question, breaths = heard
        answer = " ".join(breaths)
        if any(REFUSAL.search(sentence) for breath in breaths for sentence in SENTENCES.split(breath)):
            found.expired = True
            return (f"Not saved: the answer sounds like a no (\"{answer}\"). If the user did want it, propose it "
                    "again with propose_bee_save and ask about it alone.")
        if not AFFIRMATIVE.search(SENTENCES.split(answer.strip())[0]):
            found.expired = True
            return (f"Not saved: the answer is not a yes (\"{answer}\"). If the user wants it, propose it again with "
                    "propose_bee_save and ask about it alone.")
        found.saved = True
        async with self.waiting(context):
            write = await self._bee_writes.begin(found.item, question, answer)
            try:
                await self._bee_writes.apply(write)
            finally:  # also when the tool is cut off mid-write: the record shows the write, and that Bee may have it
                self._recorder.event("bee_write", **event_fields(write))
        what = label(write, ENGLISH)
        if write.status == "ok":
            return f"Saved to Bee: {what}. Tell the user in a few words what you saved."
        if write.status == "unknown":
            return (f"Bee did not confirm saving {what} ({write.error}): it may or may not be in Bee, and it will not "
                    "be saved again by itself. Tell the user that, and that they can check Bee after the call.")
        return (f"Saving {what} to Bee did not go through ({write.error}). Tell the user it did not go through and "
                "that it will be tried again after the call.")

    @function_tool
    async def undo_bee_write(self, context: RunContext) -> str:
        """Take the last item you saved to Bee in this call back out, when the user asks you to."""
        saved = [w for w in self._bee_writes.all() if w.status == "ok"]
        if not saved:
            return "Nothing saved to Bee in this call is left to take out."
        async with self.waiting(context):
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

    def _answer_to(self, proposal: Proposal) -> tuple[str, list[str]] | str | None:
        """The question the AI asked about a proposed item and the user's answer to it, as the conversation shows
        them: (question, the answer's breaths). None while there is none yet: no question since the proposal (a save
        in the reply that asks cannot see that reply yet), or no answer to it yet. EXPIRED once the answer is past.
        - The question is the first AI turn since the proposal that names Bee (the word), quotes the item whole and
          asks ("?"); if the AI said it again before the user answered, its last saying.
        - The answer is the user's turns right after it (one breath or several), with nothing between but the AI's
          words that ask nothing.
        - The answer is past once the conversation went on: the user spoke again after the AI did, or the AI asked
          something else. A yes after that, to whatever, is no answer to this proposal."""
        turns = self._turns()
        ids = [turn.id for turn in turns]
        if proposal.after is None:
            later = turns
        elif proposal.after in ids:
            later = turns[ids.index(proposal.after) + 1:]
        else:
            return EXPIRED  # the turn it followed is no longer in the conversation
        item = proposal.item["text"]
        asked = [i for i, turn in enumerate(later) if turn.role == "assistant" and asks_about(turn.text_content, item)]
        if not asked:
            return None
        answered = next((i for i in range(asked[0] + 1, len(later)) if later[i].role == "user"), None)
        if answered is None:
            return None
        question = max(i for i in asked if i < answered)
        if any(QUESTION.search(turn.text_content or "") for turn in later[question + 1:answered]):
            return EXPIRED  # the AI asked something else before the answer
        end = next((i for i in range(answered, len(later)) if later[i].role != "user"), len(later))
        if any(turn.role == "user" or QUESTION.search(turn.text_content or "") for turn in later[end:]):
            return EXPIRED
        breaths = [(turn.text_content or "").strip() for turn in later[answered:end]]
        return (later[question].text_content or "", [b for b in breaths if b]) if any(breaths) else None


def asks_about(text: str | None, item: str) -> bool:
    """Whether an AI turn asks about the item: it names Bee (the word), and one of its sentences is a question
    ("?") that quotes the item's text whole, in quotation marks. So "I'll add 'Buy milk' to Bee. Anything else?"
    asks about something else, and 'Buy milk and eggs' does not quote 'Buy milk'."""
    text = re.sub(r"\s+", " ", text or "")
    if not NAMES_BEE.search(text):
        return False
    quote = f"[{re.escape(QUOTES)}]"
    quoted = re.compile(f"{quote}{re.escape(_plain(item))}[.!?。]?{quote}")
    return any(quoted.search(sentence.casefold()) for sentence in SENTENCES.split(text)
               if QUESTION.search(sentence.rstrip(QUOTES + " ")[-1:]))


def _plain(text: str) -> str:
    """Text compared loosely: spaces collapsed, case and end punctuation ignored."""
    return re.sub(r"\s+", " ", text).strip().strip(".!?。").casefold()
