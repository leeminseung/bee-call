"""The Claude worker (worker/claude.py): background jobs on Claude through Anthropic's Messages API, with Anthropic's
web search, web fetch and code execution and the job's own tools, against a scripted API (no network, no cost)."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from anthropic.types import Message

from tests.helpers import SEOUL
from voice import costs, model_catalog
from voice.model_catalog import Choice
from worker import claude
from worker.claude import ClaudeWorker
from worker.runner import Job

SONNET = Choice("sonnet", "Claude Sonnet 5.5", "Anthropic", {"ko": "조사", "en": "Research"},
                model_catalog.CLAUDE_SONNET, default=True, thinking="adaptive", effort="high", max_tokens=32000,
                extra={"anthropic": {"cache_control": {"type": "ephemeral"}}})


def reply(content: list[dict], stop: str = "tool_use", *, searches: int = 0, fetches: int = 0,
          tokens: tuple[int, int, int, int] = (100, 10, 0, 0), container: str | None = None) -> Message:
    """One API answer: content blocks, stop reason, usage (input, output, cache read, cache write) and server tools."""
    fresh, out, read, write = tokens
    return Message.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": content,
        "stop_reason": stop, "stop_sequence": None,
        "container": {"id": container, "expires_at": "2026-10-15T12:00:00Z"} if container else None,
        "usage": {"input_tokens": fresh, "output_tokens": out, "cache_read_input_tokens": read,
                  "cache_creation_input_tokens": write,
                  "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches}}})


def tool(tool_id: str, tool_name: str, /, **args) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": tool_name, "input": args}


def searched(tool_id: str, query: str) -> list[dict]:
    return [{"type": "server_tool_use", "id": tool_id, "name": "web_search", "input": {"query": query}},
            {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": [
                {"type": "web_search_result", "url": "https://apple.example/kr", "title": "MacBook Air",
                 "encrypted_content": "ENC-" + tool_id, "page_age": None}]}]


def ran_code(tool_id: str, *file_ids: str) -> list[dict]:
    return [{"type": "server_tool_use", "id": tool_id, "name": "bash_code_execution",
             "input": {"command": "python table.py && cp prices.csv \"$OUTPUT_DIR/\""}},
            {"type": "bash_code_execution_tool_result", "tool_use_id": tool_id, "content": {
                "type": "bash_code_execution_result", "stdout": "", "stderr": "", "return_code": 0,
                "content": [{"type": "bash_code_execution_output", "file_id": f} for f in file_ids]}}]


def text(words: str) -> dict:
    return {"type": "text", "text": words}


class Stream:
    def __init__(self, answer: Message | BaseException, hold: asyncio.Event | None) -> None:
        self.answer, self.hold = answer, hold

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def events():
            yield SimpleNamespace(type="message_start")
            if self.hold is not None:
                await self.hold.wait()  # a request still running
            if isinstance(self.answer, BaseException):
                raise self.answer
        return events()

    async def get_final_message(self):
        return self.answer


class Messages:
    def __init__(self, answers: list, hold: asyncio.Event | None = None, files: "Files | None" = None) -> None:
        self.answers, self.hold, self.files, self.sent = list(answers), hold, files, []

    def stream(self, **params):
        self.sent.append(json.loads(json.dumps(params)))
        # As the real API does: a request whose conversation names a file deleted from the Files API is refused.
        gone = [f for f in (self.files.deleted if self.files else []) if f in json.dumps(params)]
        if gone:
            import anthropic
            import httpx

            raise anthropic.NotFoundError(f"File `{gone[0]}` not found.", body=None, response=httpx.Response(
                404, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
        return Stream(self.answers.pop(0), self.hold)


class Files:
    """Anthropic's Files API, for the files code execution left in $OUTPUT_DIR: (file name, bytes) by file id."""

    def __init__(self, files: dict[str, tuple[str, bytes]]) -> None:
        self.files, self.deleted = files, []

    async def retrieve_metadata(self, file_id):
        name, data = self.files[file_id]
        return SimpleNamespace(id=file_id, filename=name, size_bytes=len(data))

    async def download(self, file_id):
        data = self.files[file_id][1]

        async def read():
            return data
        return SimpleNamespace(read=read)

    async def delete(self, file_id):
        self.deleted.append(file_id)


def api(answers: list, files: dict | None = None, hold: asyncio.Event | None = None) -> SimpleNamespace:
    stored = Files(files or {})
    return SimpleNamespace(messages=Messages(answers, hold, stored), files=stored)


def job_in(tmp_path, language: str | None = "ko") -> Job:
    return Job(job_id="01", dir=tmp_path, now=datetime(2026, 10, 15, 15, 0, tzinfo=SEOUL),
               task="맥북 에어와 프로 가격 비교", title="맥북 가격", language=language)


def run(job: Job, client, *, cancel_after: float | None = None) -> tuple[str | BaseException, list, list]:
    """Runs the job; (its summary or what it raised, the progress lines, what it relayed)."""
    progress, relayed = [], []

    async def relay(words):
        relayed.append(words)

    async def main():
        task = asyncio.create_task(ClaudeWorker(SONNET, client=client).run(job, progress.append, relay))
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            task.cancel()
        try:
            return await task
        except BaseException as exc:  # noqa: BLE001 - the test looks at it
            return exc

    return asyncio.run(main()), progress, relayed


def usage_of(job: Job) -> dict:
    return json.loads((job.dir / "usage.json").read_text())


def test_a_job_runs_on_claude_with_anthropic_s_server_tools_and_the_job_s_own_tools(tmp_path):
    """Claude searches, runs code and calls the job's tools; each Claude turn goes back exactly as it came, the job
    keeps one code execution container, and usage.json counts every request with its cache and its searches."""
    report = "# 맥북 가격\n\n에어는 159만 원부터예요.\n"
    first = [{"type": "thinking", "thinking": "plan", "signature": "SIG"}, text("가격을 찾아볼게요."),
             *searched("srvtoolu_1", "맥북 에어 가격"), tool("toolu_1", "progress", text="가격 찾는 중")]
    client = api([
        reply(first, searches=1, tokens=(1000, 50, 0, 900)),
        reply([*ran_code("srvtoolu_2", "file_1"), tool("toolu_2", "relay_to_user", text="에어는 159만 원부터예요."),
               tool("toolu_3", "save_file", name="report.md", content=report)],
              tokens=(300, 80, 900, 250), container="container_1"),
        reply([tool("toolu_4", "save_chart", name="prices", title="가격(원)", kind="bar",
                    series=[{"label": "Air", "points": [["8GB", 1590000]]}])], tokens=(100, 40, 1150, 60)),
        reply([text("에어와 프로 가격을 비교했어요. report.md와 prices.csv를 남겼어요.")], "end_turn",
              tokens=(50, 30, 1210, 20)),
    ], files={"file_1": ("prices.csv", b"model,price\nAir,1590000\n")})
    job = job_in(tmp_path)
    summary, progress, relayed = run(job, client)
    assert summary == "에어와 프로 가격을 비교했어요. report.md와 prices.csv를 남겼어요."
    assert progress == ["가격 찾는 중"] and relayed == ["에어는 159만 원부터예요."]
    assert (tmp_path / "report.md").read_text() == report
    assert (tmp_path / "prices.csv").read_bytes() == b"model,price\nAir,1590000\n"  # from code execution
    # Deleted from Anthropic's Files API once the job is over: its conversation names the file until then.
    assert client.files.deleted == ["file_1"]
    assert (tmp_path / "prices.svg").exists() and (tmp_path / "prices.png").exists()

    opening, second, third, last = client.messages.sent
    assert (opening["model"], opening["max_tokens"], opening["thinking"], opening["output_config"]) == (
        "claude-sonnet-5-5", 32000, {"type": "adaptive"}, {"effort": "high"})
    assert opening["cache_control"] == {"type": "ephemeral"} and opening["system"] == claude.SYSTEM_PROMPT
    tools = {t["name"]: t for t in opening["tools"]}
    assert [t.get("type") for t in opening["tools"][:3]] == [
        "web_search_20260318", "web_fetch_20260318", "code_execution_20260521"]
    assert tools["web_search"]["max_uses"] == claude.WEB_SEARCHES_PER_REQUEST
    assert tools["web_fetch"]["max_content_tokens"] == claude.FETCHED_TOKENS
    assert list(tools)[3:] == ["progress", "relay_to_user", "save_file", "save_chart"]
    asked = opening["messages"][0]["content"][0]["text"]
    assert asked.startswith("Job 01 (it is now ") and "맥북 에어와 프로 가격 비교" in asked and "Korean" in asked
    assert second["messages"][1] == {"role": "assistant", "content": json.loads(json.dumps(
        [{"type": "thinking", "thinking": "plan", "signature": "SIG"}, text("가격을 찾아볼게요."),
         *searched("srvtoolu_1", "맥북 에어 가격"), tool("toolu_1", "progress", text="가격 찾는 중")]))}
    assert second["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Shown on the job card."}]
    assert "container" not in second and third["container"] == last["container"] == "container_1"
    assert [r["tool_use_id"] for r in third["messages"][-1]["content"]] == ["toolu_2", "toolu_3"]

    used = usage_of(job)
    assert (used["provider"], used["model"], used["status"], used["stop_reason"], used["requests"]) == (
        "anthropic", "claude-sonnet-5-5", "completed", "end_turn", 4)
    assert (used["input_tokens"], used["output_tokens"], used["cache_read"], used["cache_write"]) == (
        1000 + 900 + 300 + 900 + 250 + 100 + 1150 + 60 + 50 + 1210 + 20, 200, 900 + 1150 + 1210, 900 + 250 + 60 + 20)
    assert used["server_tools"] == {"web_search": 1} and "estimated" not in used
    assert [c["name"] for step in used["steps"] for c in step["calls"]] == [
        "progress", "relay_to_user", "save_file", "save_chart"]
    assert costs.job_cost(used) > 0.01  # the search and the tokens, priced (voice/costs.py)


def test_a_paused_turn_goes_back_as_it_came_without_a_new_message(tmp_path):
    """A long server tool turn can pause (pause_turn): the paused turn is sent back unchanged, nothing added."""
    paused = searched("srvtoolu_1", "MacBook price")
    client = api([reply(paused, "pause_turn", searches=1), reply([text("Done.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "Done."
    later = client.messages.sent[1]["messages"]
    assert [m["role"] for m in later] == ["user", "assistant"] and later[1]["content"] == json.loads(json.dumps(paused))
    assert (tmp_path / "report.md").read_text() == "# Report for job 01\n\nDone.\n"  # none saved: the summary


def test_files_from_code_execution_get_safe_free_names_and_none_too_large(tmp_path):
    """A file name from the sandbox is only a name in the job folder: no folders, nothing this Mac writes itself
    (usage.json), no file already there replaced, and a name in other letters keeps its kind (.csv)."""
    (tmp_path / "report.md").write_text("from save_file")
    files = {"f1": ("../../etc/passwd", b"x"), "f2": ("가격표.csv", b"a,b\n"), "f3": ("report.md", b"# other"),
             "f4": ("usage.json", b"{}"), "f5": ("huge.bin", b"0" * (claude.MAX_FILE_BYTES + 1))}
    client = api([reply([*ran_code("srvtoolu_1", *files), text("Made the files.")], "end_turn")], files=files)
    run(job_in(tmp_path, None), client)
    assert (tmp_path / "passwd").read_bytes() == b"x" and not (tmp_path.parent / "etc").exists()
    assert (tmp_path / "output-2.csv").read_bytes() == b"a,b\n"
    assert (tmp_path / "report.md").read_text() == "from save_file" and (tmp_path / "report-2.md").exists()
    assert json.loads((tmp_path / "usage.json").read_text())["provider"] == "anthropic"
    assert (tmp_path / "output-4.json").read_bytes() == b"{}"
    assert not list(tmp_path.glob("huge*")) and sorted(client.files.deleted) == sorted(files)


def test_at_its_search_limit_a_job_is_asked_for_its_answer_without_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    client = api([reply([*searched("srvtoolu_1", "q"), tool("toolu_1", "progress", text="Searching")], searches=3),
                  reply([text("Air starts at 1.59 million won.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "Air starts at 1.59 million won."
    final = client.messages.sent[1]
    assert final["tool_choice"] == {"type": "none"} and "tool_choice" not in client.messages.sent[0]
    note = final["messages"][-1]["content"]
    assert note[0]["type"] == "tool_result" and "web searches" in note[-1]["text"]


def test_a_job_that_keeps_searching_past_its_limit_or_runs_out_of_requests_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    failed, _, _ = run(job_in(tmp_path, None), api([reply(searched("srvtoolu_1", "q"), "pause_turn", searches=4)]))
    assert isinstance(failed, RuntimeError) and "past this job's limit of 3 web searches" in str(failed)
    assert usage_of(job_in(tmp_path, None))["status"] == "failed"
    monkeypatch.setattr(claude, "MAX_REQUESTS", 2)
    pauses = [reply(searched(f"srvtoolu_{n}", "q"), "pause_turn", searches=1) for n in range(2)]
    failed, _, _ = run(job_in(tmp_path, None), api(pauses))
    assert isinstance(failed, RuntimeError) and "within 2 requests" in str(failed)
    stopped, _, _ = run(job_in(tmp_path, None), api([reply([text("Half an answer")], "max_tokens")]))
    assert isinstance(stopped, RuntimeError) and "max_tokens" in str(stopped)


def test_a_job_cancelled_while_a_request_runs_counts_an_estimate(tmp_path):
    """The call ended: the request is cut off, and what it sent is counted (marked estimated) with what came before."""
    hold = asyncio.Event()  # never set: the request is still running when the call ends
    client = api([reply([text("Never sent.")], "end_turn")], hold=hold)
    cancelled, _, _ = run(job_in(tmp_path), client, cancel_after=0.05)
    assert isinstance(cancelled, asyncio.CancelledError)
    used = usage_of(job_in(tmp_path))
    assert used["status"] == "cancelled" and used["estimated"] and used["input_tokens"] > 0


def test_a_request_that_fails_midway_is_tried_again(tmp_path, monkeypatch):
    import anthropic
    import httpx

    monkeypatch.setattr(claude, "RETRY_WAITS_S", (0, 0))
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    broken = anthropic.APIConnectionError(request=request)
    # An error event in the middle of the stream: the SDK raises it with the stream's own status, 200.
    overloaded = anthropic.APIStatusError("overloaded", response=httpx.Response(200, request=request), body={
        "type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    client = api([broken, overloaded, reply([text("Done after all.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "Done after all." and len(client.messages.sent) == 3
    assert usage_of(job_in(tmp_path, None))["requests"] == 3  # each try counted, the failed ones as estimates
    refused = anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    client = api([refused])
    failed, _, _ = run(job_in(tmp_path, None), client)
    assert isinstance(failed, anthropic.BadRequestError) and len(client.messages.sent) == 1  # not tried again


# ---------- which worker runs a person's jobs ----------

@pytest.fixture
def both_workers(monkeypatch):
    """The worker role with Claude Sonnet 5.5 (the default) and GLM 5 on the AgentCore harness, both set up here: an
    Anthropic key, and a harness (its AWS client a stand-in)."""
    from dataclasses import replace

    import worker.harness as harness
    from voice import providers
    from voice.model_catalog import Role
    from voice.providers.anthropic import Anthropic

    glm = replace(model_catalog.choice("worker", "glm"), default=False)
    monkeypatch.setitem(model_catalog.ROLES, "worker", Role("worker", person=True, choices=(SONNET, glm)))
    monkeypatch.setitem(providers._ADAPTERS, "anthropic", Anthropic())  # no client left over from another test
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(harness, "load_harness_arn", lambda: "arn:aws:bedrock-agentcore:ap-southeast-2:1:harness/h")
    monkeypatch.setattr(harness, "aws_session", lambda **_: SimpleNamespace(client=lambda *a, **k: "agentcore"))
    return harness


def test_the_person_s_worker_model_picks_what_runs_their_jobs(both_workers):
    """WORKER=harness (the server's): each person's worker_model, through its provider's adapter; the stand-ins
    (fake, search) whatever the person picked."""
    from voice.agent import job_worker
    from worker.fake import FakeWorker
    from worker.search import SearchWorker

    picked = job_worker("sonnet", "harness")
    assert isinstance(picked, ClaudeWorker) and picked.choice.offering.model == "claude-sonnet-5-5"
    assert isinstance(job_worker("glm", "harness"), both_workers.HarnessWorker)
    assert isinstance(job_worker("gpt-9", "harness"), ClaudeWorker)  # an id the catalog no longer has: the default
    assert isinstance(job_worker("sonnet", "fake"), FakeWorker) and isinstance(job_worker("glm", "search"), SearchWorker)


def test_a_worker_not_set_up_here_hands_the_jobs_to_one_that_is(both_workers, monkeypatch, tmp_path):
    """Without the Anthropic key the harness runs a person's jobs, and with neither, each job fails saying why."""
    from voice.agent import job_worker

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert isinstance(job_worker("sonnet", "harness"), both_workers.HarnessWorker)
    monkeypatch.setattr(both_workers, "load_harness_arn", lambda: None)
    nothing = job_worker("sonnet", "harness")

    async def relay(words):
        pass

    with pytest.raises(RuntimeError, match="no background worker is set up.*ANTHROPIC_API_KEY.*deploy_worker"):
        asyncio.run(nothing.run(job_in(tmp_path), lambda line: None, relay))
