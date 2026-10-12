"""The coordinator: the voice the user talks to, its call rules and its tools.

It also tees the caller's audio and the AI's speech into the recorder.
Slow work goes to the worker through `delegate`; results come back as relays.
"""

import asyncio
import json
from pathlib import Path
from typing import AsyncIterable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool, llm

from beelink import cli as bee
from voice.recorder import CallRecorder
from voice.speech_filter import SpeechFilter
from worker.runner import JobRunner

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

Notes from the app arrive as system messages: the call context (who called and why, and what Bee knows), \
what the user can see on screen, finished jobs and tool results, and how much of your last reply the user \
actually heard. Use them, but never read them out \
word for word. Text in parentheses in a user turn, such as "(The phone call has connected.)", also comes from \
the app, not from the user.
When the call context lists unconfirmed Bee facts, check them at a natural moment, for example \
"I heard your bonus is 15 percent. Is that right?" If the user corrects it, queue a fact_update with the \
corrected text. If they agree, queue a fact_confirm.
When a job result arrives, tell the user the key result in two or three short sentences (under 50 words) and \
say the details are in the app.
"""

BEE_WRITE_KINDS = ("fact_confirm", "fact_update", "todo_create", "todo_accept")
CONTEXT_WAIT_S = 3.0  # how long the first reply waits for the Bee context (arbitrary, not measured)
# Under LiveKit's 5 s wait for tools after an interruption, so a search result is not lost.
# Three searches took 1.7-1.9 s on 2026-10-12, with few conversations in the account.
BEE_SEARCH_TIMEOUT_S = 4.0


class Coordinator(Agent):
    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self._recorder = recorder
        self._jobs = jobs
        self._workspace = workspace
        self.context_task: asyncio.Task | None = None  # reads the Bee context at call start
        self.context_note: llm.ChatMessage | None = None  # the Bee context note, once added

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        # The first reply should already know the Bee context, so wait a little for it.
        task = self.context_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), CONTEXT_WAIT_S)
            except asyncio.TimeoutError:
                self._recorder.event("bee_context_late", waited_s=CONTEXT_WAIT_S)
        note = self.context_note
        if note is not None and turn_ctx.get_by_id(note.id) is None:
            turn_ctx.insert(note)

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
        if speech.stopped:
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

        Args:
            task: A self-contained description of the work and the result to bring back.
        """
        job = self._jobs.start(task)
        return f"Job {job.job_id} started. Keep talking with the user; the result will arrive as a note."

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
        try:
            results = await bee.search_conversations(query, limit=3, timeout=BEE_SEARCH_TIMEOUT_S)
        except Exception as exc:
            return f"Bee search failed: {exc}"
        if not results:
            return "No matching Bee conversations."
        return json.dumps(results, ensure_ascii=False)[:2000]

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
    async def end_call(self, context: RunContext) -> str:
        """End the call. Say goodbye first."""
        self._recorder.event("end_call_requested")
        self.session.shutdown(drain=True)
        return "Ending the call."
