"""Job news reaches the coordinator as notifications in the style of Claude Code's background tasks, and a reply
the caller cuts off stays in the history as LiveKit leaves it: the words heard, marked interrupted."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from livekit.agents import AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.voice.io import TextOutput

from tests.helpers import seoul_now
from voice.coordinator import Coordinator, job_notification
from voice.recorder import CallRecorder
from worker.runner import JobNotification, JobRunner

TASK = "Check the weather in Seoul for Saturday"


class Worker:
    """Relays an answer, writes its report, then finishes (or fails)."""

    def __init__(self, relay_text: str | None = "Saturday looks dry.", fail: str | None = None) -> None:
        self.relay_text = relay_text
        self.fail = fail

    async def run(self, job, progress, relay) -> str:
        progress("Checking two forecasts")
        if self.relay_text:
            await relay(self.relay_text)
        (job.dir / "report.md").write_text("# Report\n")
        if self.fail:
            raise RuntimeError(self.fail)
        return "Both forecasts say dry."


def run_one_job(tmp_path, worker) -> tuple[JobRunner, list[JobNotification]]:
    news: list[JobNotification] = []

    async def on_notify(n: JobNotification) -> None:
        news.append(n)

    async def main() -> JobRunner:
        runner = JobRunner(tmp_path, lambda name, **f: None, worker, on_notify, clock=seoul_now)
        runner.start(TASK)
        await asyncio.gather(*runner.tasks.values())
        return runner

    return asyncio.run(main()), news


def test_the_answer_comes_while_the_job_runs_and_the_end_brings_only_the_files(tmp_path):
    runner, news = run_one_job(tmp_path, Worker())
    assert [(n.status, n.summary, n.files, n.needs_reply) for n in news] == [
        ("running", "Saturday looks dry.", [], True), ("completed", None, ["report.md"], False)]
    assert runner.status_text().startswith(f"Job 01 | completed after 0 s | task: {TASK} | files: report.md")


def test_a_job_that_never_relayed_ends_with_its_summary(tmp_path):
    _, news = run_one_job(tmp_path, Worker(relay_text=None))
    assert [(n.status, n.summary, n.files) for n in news] == [("completed", "Both forecasts say dry.", ["report.md"])]


def test_a_failure_after_the_answer_is_still_told(tmp_path):
    _, news = run_one_job(tmp_path, Worker(fail="disk full"))
    assert [n.status for n in news] == ["running", "failed"]
    assert news[1].error == "disk full" and news[1].needs_reply


def test_a_failure_without_a_message_is_still_told(tmp_path):
    class Silent:
        async def run(self, job, progress, relay):
            raise TimeoutError()

    _, news = run_one_job(tmp_path, Silent())
    assert [(n.status, n.error, n.needs_reply) for n in news] == [("failed", "TimeoutError", True)]


def test_an_empty_relay_does_not_count_and_the_summary_is_told_at_the_end(tmp_path):
    _, news = run_one_job(tmp_path, Worker(relay_text="  "))
    assert [(n.status, n.summary, n.needs_reply) for n in news] == [("completed", "Both forecasts say dry.", True)]


def test_job_status_shows_the_latest_step_of_a_running_job(tmp_path):
    async def main() -> str:
        started = asyncio.Event()

        class Slow:
            async def run(self, job, progress, relay):
                progress("Comparing two forecasts")
                started.set()
                await asyncio.sleep(10)

        async def ignore(n):
            pass

        runner = JobRunner(tmp_path, lambda name, **f: None, Slow(), ignore, clock=seoul_now)
        assert runner.status_text() == "No background jobs in this call."
        runner.start("Check Busan")
        await started.wait()
        text = runner.status_text()
        await runner.cancel_all()
        return text

    assert asyncio.run(main()) == "Job 01 | running for 0 s | task: Check Busan | latest step: Comparing two forecasts"


def test_the_notification_reads_as_the_app_and_reaches_the_model_in_turn_order(tmp_path):
    answer = JobNotification("01", "running", TASK, summary="Saturday looks dry.")
    note = job_notification(answer)
    assert note.splitlines()[:3] == ["[App notification, not the caller speaking]", "<job-notification>",
                                     "<job-id>01</job-id>"]
    assert "<summary>Saturday looks dry.</summary>" in note and "<files>" not in note

    async def main() -> Coordinator:
        agent = Coordinator(CallRecorder(tmp_path, room="r", now=seoul_now()), SimpleNamespace(), tmp_path,
                            clock=seoul_now)
        chat = agent.chat_ctx.copy()
        chat.add_message(role="user", content="Can you check the weather?")
        await agent.update_chat_ctx(chat)
        assert await agent.tell_job(answer) is True
        assert await agent.tell_job(JobNotification("01", "completed", TASK, files=["report.md"])) is False
        return agent

    agent = asyncio.run(main())
    messages, _ = agent.chat_ctx.to_provider_format(format="aws")
    texts = [block["text"] for block in messages[-1]["content"]]
    assert texts[0] == "Can you check the weather?"
    assert texts[1].startswith("<instructions>\n[App notification") and "<summary>" in texts[1]
    assert "<files>report.md</files>" in texts[2] and "<summary>" not in texts[2]


REPLY = ("Saturday looks dry, about a 10 percent chance of rain. "
         "Gwanaksan is the easier hike, about two and a half hours round trip.")


class Words(llm.LLM):
    """Streams REPLY word by word, fast: the model writes ahead of what the caller hears."""

    @property
    def model(self) -> str:
        return "words"

    @property
    def provider(self) -> str:
        return "test"

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool] | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN, tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
             extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN) -> "WordStream":
        return WordStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class WordStream(llm.LLMStream):
    async def _run(self) -> None:
        chunk_id = uuid.uuid4().hex
        for word in REPLY.split(" "):
            self._event_ch.send_nowait(llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant",
                                                                                         content=word + " ")))
            await asyncio.sleep(0.001)


class SlowCaptions(TextOutput):
    """Takes the reply's words at the pace of speech, the way audio playback paces what the caller hears."""

    def __init__(self) -> None:
        super().__init__(label="captions", next_in_chain=None)
        self.text = ""

    async def capture_text(self, text: str) -> None:
        self.text += text
        await asyncio.sleep(0.05)

    def flush(self) -> None:
        pass


def test_a_cut_off_reply_keeps_only_the_heard_words_and_nothing_is_added(tmp_path):
    """A real LiveKit session in text mode: the caller cuts the reply off after its first few words. Models on
    LiveKit's Bedrock adapter see only this; a note with the unheard part made GLM 5 less likely to say it again
    (probes/coordinator_bench.py, 2026-10-13)."""

    async def main():
        agent = Coordinator(CallRecorder(tmp_path, room="r", now=seoul_now()), SimpleNamespace(), tmp_path,
                            clock=seoul_now)
        async with AgentSession(llm=Words()) as session:
            await session.start(agent)
            captions = SlowCaptions()
            session.output.transcription = captions
            speech = session.generate_reply(user_input="Will it rain on Saturday?")
            while len(captions.text.split()) < 4:
                await asyncio.sleep(0.005)
            session.interrupt()
            await speech
            await asyncio.sleep(0.05)
            return [i for i in agent.chat_ctx.items[1:] if getattr(i, "type", None) == "message"]

    messages = asyncio.run(main())
    assert [m.role for m in messages] == ["user", "assistant"]
    heard = messages[1].text_content
    assert messages[1].interrupted and 4 <= len(heard.split()) < len(REPLY.split()) and REPLY.startswith(heard.strip())
