"""The Claude worker (worker/claude.py): background jobs on Claude through Anthropic's Messages API, with Anthropic's
web search, web fetch and code execution and the job's own tools, against a scripted API (no network, no cost)."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
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
                {"type": "web_search_result", "url": "https://shop.example/kr", "title": "13-inch laptop",
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
        refuse_a_turn_ended_with_a_server_tool_waiting(self.sent[-1]["messages"])
        # As the real API does: a request whose conversation names a file deleted from the Files API is refused.
        gone = [f for f in (self.files.deleted if self.files else []) if f in json.dumps(params)]
        if gone:
            import anthropic
            import httpx

            raise anthropic.NotFoundError(f"File `{gone[0]}` not found.", body=None, response=httpx.Response(
                404, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
        return Stream(self.answers.pop(0), self.hold)


def refuse_a_turn_ended_with_a_server_tool_waiting(messages: list[dict]) -> None:
    """As the real API does (server tools docs, "Mixing server tools and client tools in one turn"): after a turn whose
    server tool call is still waiting for its result, the user's message may hold nothing but tool_result blocks; a
    block after them ends the turn, and the request is refused with a 400."""
    import anthropic
    import httpx

    for said, answer in zip(messages, messages[1:]):
        if said["role"] != "assistant" or answer["role"] != "user" or not isinstance(said["content"], list):
            continue
        waiting = {b["id"] for b in said["content"] if b.get("type") == "server_tool_use"} - \
            {b.get("tool_use_id") for b in said["content"]}
        if waiting and any(b.get("type") != "tool_result" for b in answer["content"]):
            raise anthropic.BadRequestError(
                f"`web_search` tool use with id `{sorted(waiting)[0]}` was found without a corresponding "
                "`web_search_tool_result` block", body=None,
                response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))


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
               task="13인치와 15인치 노트북 가격 비교", title="노트북 가격", language=language)


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
    report = "# 노트북 가격\n\n13인치는 159만 원부터예요.\n"
    first = [{"type": "thinking", "thinking": "plan", "signature": "SIG"}, text("가격을 찾아볼게요."),
             *searched("srvtoolu_1", "13인치 노트북 가격"), tool("toolu_1", "progress", text="가격 찾는 중")]
    client = api([
        reply(first, searches=1, tokens=(1000, 50, 0, 900)),
        reply([*ran_code("srvtoolu_2", "file_1"), tool("toolu_2", "relay_to_user", text="13인치는 159만 원부터예요."),
               tool("toolu_3", "save_file", name="report.md", content=report)],
              tokens=(300, 80, 900, 250), container="container_1"),
        reply([tool("toolu_4", "save_chart", name="prices", title="가격(원)", kind="bar",
                    series=[{"label": "13-inch", "points": [["8GB", 1590000]]}])], tokens=(100, 40, 1150, 60)),
        reply([text("13인치와 15인치 가격을 비교했어요. report.md와 prices.csv를 남겼어요.")], "end_turn",
              tokens=(50, 30, 1210, 20)),
    ], files={"file_1": ("prices.csv", b"model,price\n13-inch,1590000\n")})
    job = job_in(tmp_path)
    summary, progress, relayed = run(job, client)
    assert summary == "13인치와 15인치 가격을 비교했어요. report.md와 prices.csv를 남겼어요."
    assert progress == ["가격 찾는 중"] and relayed == ["13인치는 159만 원부터예요."]
    assert (tmp_path / "report.md").read_text() == report
    assert (tmp_path / "prices.csv").read_bytes() == b"model,price\n13-inch,1590000\n"  # from code execution
    # Deleted from Anthropic's Files API once the job is over: its conversation names the file until then.
    assert client.files.deleted == ["file_1"]
    assert all((tmp_path / name).exists() for name in ("prices.svg", "prices.png", "prices-dark.png"))

    opening, second, third, last = client.messages.sent
    assert (opening["model"], opening["max_tokens"], opening["thinking"], opening["output_config"]) == (
        "claude-sonnet-5-5", 32000, {"type": "adaptive"}, {"effort": "high"})
    assert opening["cache_control"] == {"type": "ephemeral"} and opening["system"] == claude.SYSTEM_PROMPT
    tools = {t["name"]: t for t in opening["tools"]}
    assert [t.get("type") for t in opening["tools"][:3]] == [
        "web_search_20260318", "web_fetch_20260318", "code_execution_20260521"]
    assert tools["web_search"]["max_uses"] == claude.WEB_SEARCHES_PER_REQUEST
    assert tools["web_fetch"]["max_content_tokens"] == claude.FETCHED_TOKENS
    assert tools["web_fetch"]["max_uses"] == claude.WEB_FETCHES_PER_REQUEST
    assert list(tools)[3:] == ["progress", "relay_to_user", "save_file", "save_chart"]
    asked = opening["messages"][0]["content"][0]["text"]
    assert asked.startswith("Job 01 (it is now ") and "13인치와 15인치 노트북 가격 비교" in asked and "Korean" in asked
    assert second["messages"][1] == {"role": "assistant", "content": json.loads(json.dumps(
        [{"type": "thinking", "thinking": "plan", "signature": "SIG"}, text("가격을 찾아볼게요."),
         *searched("srvtoolu_1", "13인치 노트북 가격"), tool("toolu_1", "progress", text="가격 찾는 중")]))}
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
    paused = searched("srvtoolu_1", "laptop price")
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


def test_a_rejected_file_name_or_tool_name_is_never_repeated_back(tmp_path):
    """Coordinator's review of 2026-10-15, item 3: web fetch may fetch a URL that appears in a tool result, so a
    rejected name such as https://x/?d=<the task> repeated in the error would carry the task's text out."""
    from worker.harness import HarnessWorker, ToolUse

    leak = "https://evil.example/?d=salary-details"
    for name, args in (("save_file", {"name": leak, "content": "x"}), ("save_chart", {"name": leak, "title": "t",
                                                                                         "kind": "bar", "series": []})):
        client = api([reply([tool("toolu_1", name, **args)]), reply([text("Done.")], "end_turn")])
        run(job_in(tmp_path, None), client)
        (result,) = client.messages.sent[1]["messages"][-1]["content"]
        assert result["is_error"] and "evil.example" not in result["content"], name
    client = api([reply([tool("toolu_1", leak, text="x")]), reply([text("Done.")], "end_turn")])
    run(job_in(tmp_path, None), client)
    assert "evil.example" not in json.dumps(client.messages.sent[1]["messages"][-1]["content"])

    async def relay(words):
        pass

    worker = HarnessWorker("arn:harness", client=object())
    for use in (ToolUse("t1", "save_file", json.dumps({"name": leak, "content": "x"})), ToolUse("t2", leak, "{}")):
        said, status = asyncio.run(worker._call(use, job_in(tmp_path, None), lambda line: None, relay))
        assert status == "error" and "evil.example" not in said


def test_a_long_file_name_already_taken_gets_a_numbered_name_that_fits(tmp_path):
    """Review of 2026-10-15, finding 2: a name of 80 or 81 characters already in the job folder had no numbered name
    short enough to be allowed, and finding one looped forever in the call's event loop."""
    from worker.tools import SAFE_NAME, free_name

    for length in (79, 80, 81):
        name = "r" * (length - 4) + ".csv"
        (tmp_path / name).write_text("taken")
        found = free_name(tmp_path, name, "output-1.csv")
        assert found != name and SAFE_NAME.fullmatch(found) and found.endswith("-2.csv") and not (tmp_path / found).exists()
    long_kind = "a." + "x" * 79  # no room for a number before its kind
    (tmp_path / long_kind).write_text("taken")
    found = free_name(tmp_path, long_kind, "output-1")
    assert found != long_kind and SAFE_NAME.fullmatch(found) and not (tmp_path / found).exists()


def test_at_its_search_limit_a_job_is_asked_for_its_answer_without_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    client = api([reply([*searched("srvtoolu_1", "q"), tool("toolu_1", "progress", text="Searching")], searches=3),
                  reply([text("The 13-inch starts at 1.59 million won.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "The 13-inch starts at 1.59 million won."
    final = client.messages.sent[1]
    assert final["tool_choice"] == {"type": "none"} and "tool_choice" not in client.messages.sent[0]
    note = final["messages"][-1]["content"]
    assert note[0]["type"] == "tool_result" and "web searches" in note[-1]["text"]


@pytest.mark.parametrize("limit, value, used, what", [
    ("JOB_WEB_FETCHES", 2, {"fetches": 2}, "page reads"),
    ("JOB_USD", 0.5, {"tokens": (300_000, 0, 0, 0)}, "budget"),  # a large PDF read whole costs this way
])
def test_a_job_s_page_reads_and_spending_are_limits_too(tmp_path, monkeypatch, limit, value, used, what):
    """Coordinator's review of 2026-10-15 (queued): web fetch had no cap, and max_content_tokens does not cap a PDF.
    Page reads are capped per request and a job's page reads and spending are limits like its searches."""
    monkeypatch.setattr(claude, limit, value)
    client = api([reply([tool("toolu_1", "progress", text="Reading")], **used),
                  reply([text("What I have.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    final = client.messages.sent[-1]
    assert summary == "What I have." and final["tool_choice"] == {"type": "none"}
    assert what in final["messages"][-1]["content"][-1]["text"] and usage_of(job_in(tmp_path, None))["limit"] == what


def test_at_a_limit_beside_a_server_tool_still_waiting_only_the_tool_results_go_back(tmp_path, monkeypatch):
    """Coordinator's review of 2026-10-15, item 1: when the turn that reaches a limit also called a server tool that
    has not run yet (web_search beside progress), the next message may hold only the tool results (a note after them
    would end the turn and the request would be refused). The limit applies one request later."""
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    waiting = {"type": "server_tool_use", "id": "srvtoolu_7", "name": "web_search", "input": {"query": "q7"}}
    client = api([reply([waiting, tool("toolu_1", "progress", text="Searching")], searches=3),
                  reply([{"type": "web_search_tool_result", "tool_use_id": "srvtoolu_7", "content": []},
                         tool("toolu_2", "progress", text="Writing")], searches=1),
                  reply([text("The 13-inch.")], "end_turn")])
    summary, progress, _ = run(job_in(tmp_path, None), client)
    assert summary == "The 13-inch." and progress == ["Searching", "Writing"]
    second, third = client.messages.sent[1:]
    assert [b["type"] for b in second["messages"][-1]["content"]] == ["tool_result"] and "tool_choice" not in second
    assert third["tool_choice"] == {"type": "none"} and "web searches" in third["messages"][-1]["content"][-1]["text"]


def test_a_limit_put_off_once_cuts_a_turn_whose_server_tool_still_waits(tmp_path, monkeypatch):
    """Seen in the real-API limit probe of 2026-10-15: with dynamic filtering Claude's searches run in code execution,
    and nearly every turn leaves a code execution call waiting beside our tools, so putting the limit off kept putting
    it off. After one such request the turn is cut where its server tools finished (our tool calls stay, with their
    results) and the answer is asked for."""
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    code = {"type": "server_tool_use", "id": "srvtoolu_5", "name": "code_execution", "input": {"code": "search()"}}
    again = {"type": "server_tool_use", "id": "srvtoolu_6", "name": "code_execution", "input": {"code": "more()"}}
    client = api([reply([code, tool("toolu_1", "progress", text="Searching")], searches=3),
                  reply([{"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_5", "content": {
                      "type": "code_execution_result", "stdout": "", "stderr": "", "return_code": 0, "content": []}},
                         again, tool("toolu_2", "progress", text="Writing")], searches=2),
                  reply([text("The 13-inch.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "The 13-inch."
    final = client.messages.sent[-1]
    assert final["tool_choice"] == {"type": "none"}
    cut = final["messages"][-2]
    assert cut["role"] == "assistant" and "srvtoolu_6" not in json.dumps(cut) and "toolu_2" in json.dumps(cut)
    assert [b["type"] for b in final["messages"][-1]["content"]] == ["tool_result", "text"]
    assert usage_of(job_in(tmp_path, None))["limit"] == "web searches"


def test_a_paused_turn_at_either_limit_still_ends_with_the_answer(tmp_path, monkeypatch):
    """Review of 2026-10-15, finding 3: a turn that paused (pause_turn) at the request or search limit skipped the
    request for the answer, so the job failed and its work was lost. Now the answer is asked for at either limit,
    whatever the stop: a paused turn is cut where its server tools finished (one still running goes, with what it
    started), and the last request has no tools."""
    monkeypatch.setattr(claude, "MAX_REQUESTS", 3)
    client = api([reply([tool("toolu_1", "progress", text="Searching")]),
                  reply(searched("srvtoolu_1", "q"), "pause_turn", searches=1),
                  reply([tool("toolu_2", "progress", text="Comparing")]),
                  reply([text("The 13-inch starts at 1.59 million won.")], "end_turn")])
    summary, progress, _ = run(job_in(tmp_path, None), client)
    assert summary == "The 13-inch starts at 1.59 million won." and progress == ["Searching", "Comparing"]
    last = client.messages.sent[-1]
    assert last["tool_choice"] == {"type": "none"} and "requests" in last["messages"][-1]["content"][-1]["text"]

    monkeypatch.setattr(claude, "MAX_REQUESTS", 80)
    monkeypatch.setattr(claude, "JOB_WEB_SEARCHES", 3)
    # Code execution still running when the turn paused, and a search it started (with its result).
    running = [{"type": "server_tool_use", "id": "srvtoolu_9", "name": "code_execution", "input": {"code": "..."}},
               {"type": "server_tool_use", "id": "srvtoolu_8", "name": "web_search", "input": {"query": "q8"},
                "caller": {"type": "code_execution_20260120", "tool_id": "srvtoolu_9"}},
               {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_8", "caller": {
                   "type": "code_execution_20260120", "tool_id": "srvtoolu_9"}, "content": []}]
    client = api([reply([*searched("srvtoolu_1", "q"), *running], "pause_turn", searches=4),
                  reply([text("From what I found: the 13-inch.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "From what I found: the 13-inch."
    final = client.messages.sent[-1]
    assert final["tool_choice"] == {"type": "none"}
    assert final["messages"][1] == {"role": "assistant", "content": json.loads(json.dumps(searched("srvtoolu_1", "q")))}
    assert final["messages"][2]["role"] == "user" and "web searches" in final["messages"][2]["content"][-1]["text"]

    client = api([reply(running[:1], "pause_turn", searches=4), reply([text("Short answer.")], "end_turn")])
    summary, _, _ = run(job_in(tmp_path, None), client)
    asked = client.messages.sent[-1]["messages"]
    assert summary == "Short answer." and len(asked) == 1 and "web searches" in asked[0]["content"][-1]["text"]


def test_a_job_that_stops_for_another_reason_fails(tmp_path):
    stopped, _, _ = run(job_in(tmp_path, None), api([reply([text("Half an answer")], "max_tokens")]))
    assert isinstance(stopped, RuntimeError) and "max_tokens" in str(stopped)
    assert usage_of(job_in(tmp_path, None))["status"] == "failed"


def test_a_job_cancelled_while_a_request_runs_counts_an_estimate(tmp_path):
    """The call ended: the request is cut off, and what it sent is counted (marked estimated) with what came before."""
    hold = asyncio.Event()  # never set: the request is still running when the call ends
    client = api([reply([text("Never sent.")], "end_turn")], hold=hold)
    cancelled, _, _ = run(job_in(tmp_path), client, cancel_after=0.05)
    assert isinstance(cancelled, asyncio.CancelledError)
    used = usage_of(job_in(tmp_path))
    assert used["status"] == "cancelled" and used["estimated"] and used["input_tokens"] > 0


def test_a_job_cut_off_by_the_end_of_its_call_still_clears_its_files_from_the_files_api(tmp_path):
    """Coordinator's review of 2026-10-15 (queued): a job cancelled when its call ended left the files it had saved
    from code execution in Anthropic's Files API. They go then too, within a few seconds at most."""
    hold = asyncio.Event()

    class HeldSecond(Messages):
        def stream(self, **params):
            stream = super().stream(**params)
            stream.hold = hold if len(self.sent) == 2 else None
            return stream

    files = Files({"file_1": ("prices.csv", b"a,b\n")})
    client = SimpleNamespace(messages=HeldSecond([reply([*ran_code("srvtoolu_1", "file_1"),
                                                         tool("toolu_1", "progress", text="Saving")]),
                                                  reply([text("Never.")], "end_turn")], files=files), files=files)
    cancelled, _, _ = run(job_in(tmp_path, None), client, cancel_after=0.2)
    assert isinstance(cancelled, asyncio.CancelledError) and (tmp_path / "prices.csv").exists()
    assert files.deleted == ["file_1"]


def test_a_request_that_runs_past_its_deadline_is_cut_and_tried_again(tmp_path, monkeypatch):
    """A real request in the limit probe of 2026-10-15 stayed open for about 50 minutes on a live connection (the
    read timeout never fired, so bytes kept coming). Each request now has a deadline: one past it is cut, counted as
    an estimate, and tried again like a dropped connection."""
    monkeypatch.setattr(claude, "RETRY_WAITS_S", (0, 0))
    monkeypatch.setattr(claude, "REQUEST_DEADLINE_S", 0.2)
    hold = asyncio.Event()  # never set: the first request never ends

    class OnceHeld(Messages):
        def stream(self, **params):
            stream = super().stream(**params)
            stream.hold = hold if len(self.sent) == 1 else None
            return stream

    client = SimpleNamespace(messages=OnceHeld([reply([text("Never.")], "end_turn"), reply([text("Done.")], "end_turn")]),
                             files=Files({}))
    summary, _, _ = run(job_in(tmp_path, None), client)
    used = usage_of(job_in(tmp_path, None))
    assert summary == "Done." and len(client.messages.sent) == 2 and used["requests"] == 2 and used["estimated"]


def test_a_request_that_fails_midway_is_tried_again(tmp_path, monkeypatch):
    """Through the real SDK: a connection that cannot be made, and an error event in the middle of the stream (which
    the SDK raises with the stream's own status, 200), are tried again; a refused request (400) is not."""
    import httpx2

    monkeypatch.setattr(claude, "RETRY_WAITS_S", (0, 0))
    overloaded = sse(streamed("x")[:1]) + b'event: error\ndata: {"type": "error", "error": {"type": ' \
                                          b'"overloaded_error", "message": "Overloaded"}}\n\n'
    client, posts = sdk_client([httpx2.ConnectError("no route"), overloaded, sse(streamed("Done after all."))])
    summary, _, _ = run(job_in(tmp_path, None), client)
    assert summary == "Done after all." and len(posts) == 3
    assert usage_of(job_in(tmp_path, None))["requests"] == 3  # each try counted
    refused = httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})
    client, posts = sdk_client([refused])
    failed, _, _ = run(job_in(tmp_path, None), client)
    import anthropic

    assert isinstance(failed, anthropic.BadRequestError) and len(posts) == 1  # not tried again


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
    monkeypatch.setitem(model_catalog.ROLES, "worker", Role("worker", person=True, choices=(SONNET, glm), standby=glm))
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

    picked = job_worker("sonnet", "harness").first  # with GLM 5 standing by
    assert isinstance(picked, ClaudeWorker) and picked.choice.offering.model == "claude-sonnet-5-5"
    assert isinstance(job_worker("glm", "harness"), both_workers.HarnessWorker)
    assert isinstance(job_worker("gpt-9", "harness").first, ClaudeWorker)  # an id the catalog no longer has: default
    assert isinstance(job_worker(None, "harness").first, ClaudeWorker)  # never picked: the role's default
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


# ---------- through the real SDK, on a stand-in transport ----------

def sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def streamed(text_out: str, stop: str = "end_turn") -> list[dict]:
    """The stream of one answer of plain text, as the Messages API sends it."""
    return [{"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text_out}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": 20}},
            {"type": "message_stop"}]


def sdk_client(answers: list) -> tuple[object, list]:
    """Anthropic's own async client, answering each request from `answers` over a stand-in transport: bytes are a
    stream sent whole; (bytes, an exception) is a stream that breaks off with it after those bytes; an exception is a
    connection that fails; a response is sent as it is."""
    import anthropic
    import httpx2

    posts = []

    class Broken(httpx2.AsyncByteStream):
        def __init__(self, first: bytes, exc: Exception) -> None:
            self.first, self.exc = first, exc

        async def __aiter__(self):
            yield self.first
            raise self.exc

    def handler(request):
        posts.append(json.loads(request.content or b"{}"))
        answer = answers.pop(0)
        headers = {"content-type": "text/event-stream"}
        if isinstance(answer, Exception):  # the connection itself fails
            raise answer
        if isinstance(answer, httpx2.Response):
            return answer
        if isinstance(answer, tuple):
            return httpx2.Response(200, headers=headers, stream=Broken(*answer))
        return httpx2.Response(200, headers=headers, content=answer)

    client = anthropic.AsyncAnthropic(api_key="not-a-key", max_retries=0,
                                      http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return client, posts


def test_a_connection_that_drops_mid_stream_is_tried_again(tmp_path, monkeypatch):
    """Review of 2026-10-15, finding 1: the SDK reads the stream with httpx2, and a connection that drops while the
    answer streams raises httpx2's own errors, which were not taken for a dropped connection: the job failed at once."""
    import httpx2

    monkeypatch.setattr(claude, "RETRY_WAITS_S", (0, 0))
    first = sse(streamed("Half")[:3])
    for drop in (httpx2.RemoteProtocolError("peer closed connection without sending complete message body"),
                 httpx2.ReadError("connection reset"), httpx2.ReadTimeout("timed out")):
        client, posts = sdk_client([(first, drop), sse(streamed("Done after the drop."))])
        summary, _, _ = run(job_in(tmp_path, None), client)
        assert summary == "Done after the drop." and len(posts) == 2, drop


# ---------- GLM 5 standing by for the worker (coordinator's review of 2026-10-15, item 2) ----------

class Failing:
    """A worker that spends something, then fails before or after a result."""

    def __init__(self, error: BaseException, relay_first: bool = False) -> None:
        self.error, self.relay_first = error, relay_first

    async def run(self, job, progress, relay):
        from worker.tools import write_usage

        write_usage(job, {"provider": "anthropic", "model": "claude-sonnet-5-5", "input_tokens": 1_000_000,
                          "output_tokens": 0, "server_tools": {"web_search": 10}})
        if self.relay_first:
            await relay("Half an answer.")
        raise self.error


class Standby:
    def __init__(self) -> None:
        self.ran = 0

    async def run(self, job, progress, relay):
        from worker.tools import write_usage

        self.ran += 1
        write_usage(job, {"provider": "agentcore", "model": "zai.glm-5", "input_tokens": 1_000_000,
                          "output_tokens": 0})
        await relay("GLM 5 found it.")
        (job.dir / "report.md").write_text("# Report\n")
        return "GLM 5 found it."


def run_standing_by(tmp_path, first, standby):
    from worker.runner import JobRunner, StandingBy

    tmp_path.mkdir(parents=True, exist_ok=True)
    events, news = [], []

    async def notify(n):
        news.append(n)

    async def main():
        backend = StandingBy(first, lambda: standby, "Claude Sonnet 5.5", "GLM 5")
        runner = JobRunner(tmp_path, lambda name, **f: events.append((name, f)), backend, notify,
                           clock=lambda: datetime.now(SEOUL))
        job = runner.start("Compare the trails")
        await asyncio.gather(*runner.tasks.values(), return_exceptions=True)
        return job

    return asyncio.run(main()), events, news


def test_a_job_claude_cannot_run_runs_again_on_glm_5_and_each_attempt_counts_once(tmp_path):
    """When the Claude worker fails before it produced anything (here a refused key: 401), the job runs again on GLM
    5, the worker role's standby; the call's record and the job's own log say so, the call AI hears it, and the day's
    budget counts what each attempt used, once."""
    import anthropic
    import httpx

    from voice.costs import spent_today

    call = tmp_path / datetime.now(SEOUL).strftime("%Y-%m-%d") / "0100"
    call.mkdir(parents=True)
    refused = anthropic.AuthenticationError("invalid x-api-key", body=None, response=httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
    standby = Standby()
    job, events, news = run_standing_by(call, Failing(refused), standby)
    assert job.status == "completed" and standby.ran == 1
    (moved,) = [f for name, f in events if name == "job_standby"]
    assert (moved["instead_of"], moved["worker"]) == ("Claude Sonnet 5.5", "GLM 5") and "invalid x-api-key" in moved["reason"]
    logged = [json.loads(line) for line in (job.dir / "events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in logged if e["type"] == "standby"] == ["standby"]
    assert all("GLM 5 ran it instead" in n.note for n in news) and news[0].summary == "GLM 5 found it."
    assert sorted(p.name for p in job.dir.glob("usage*.json")) == ["usage.1.json", "usage.json"]
    claude_attempt = 2.00 + 10 * 0.01  # 1M Sonnet input tokens and 10 searches
    glm_attempt = 1.00  # 1M GLM 5 input tokens
    assert spent_today(tmp_path, datetime.now(SEOUL)) == pytest.approx(claude_attempt + glm_attempt)
    from voice.coordinator import job_notification

    assert "<note>Claude Sonnet 5.5 could not run this job" in job_notification(news[0])


def test_a_job_that_produced_something_or_was_declined_or_cut_off_does_not_move(tmp_path):
    from worker.runner import JobDeclined

    for first in (Failing(RuntimeError("overloaded"), relay_first=True), Failing(JobDeclined("declined (refusal)"))):
        standby = Standby()
        job, events, _ = run_standing_by(tmp_path / str(id(first)), first, standby)
        assert job.status == "failed" and standby.ran == 0 and "job_standby" not in [name for name, _ in events]
    standby = Standby()
    job, _, _ = run_standing_by(tmp_path / "cut", Failing(asyncio.CancelledError()), standby)
    assert job.status == "cancelled" and standby.ran == 0


def test_the_worker_role_s_standby_is_glm_5_on_the_harness_and_jobs_get_it(both_workers):
    from voice.agent import job_worker
    from worker.runner import StandingBy

    standby = model_catalog.role("worker").standby
    assert (standby.id, standby.offering.provider, standby.offering.model) == ("glm", "agentcore", "zai.glm-5")
    picked = job_worker("sonnet", "harness")
    assert isinstance(picked, StandingBy) and isinstance(picked.first, ClaudeWorker)
    assert isinstance(picked.standby(), both_workers.HarnessWorker)
    assert isinstance(job_worker("glm", "harness"), both_workers.HarnessWorker)  # GLM 5 itself: nothing to stand by


def test_a_claude_refusal_is_a_decline_the_standby_never_takes(tmp_path):
    """A refusal is a safety decision, not an outage (as for the call AI): it never goes to GLM 5."""
    from worker.runner import JobDeclined

    declined, _, _ = run(job_in(tmp_path, None), api([reply([text("I can't help with that.")], "refusal")]))
    assert isinstance(declined, JobDeclined)


def test_a_call_that_ends_while_a_failed_job_clears_its_files_does_not_hand_the_job_over(tmp_path, monkeypatch):
    """Review of the last stretch, finding 6: the end of the call was swallowed while a failed job deleted its code
    execution files, so the failure went on to the standby, which ran after the call was over and held its end. A
    deletion that never finishes holds the call's end FORGET_ON_CANCEL_S at most (shortened here)."""
    import anthropic
    import httpx

    from worker.runner import JobRunner, StandingBy

    monkeypatch.setattr(claude, "FORGET_ON_CANCEL_S", 0.2)
    held = asyncio.Event()

    class SlowFiles(Files):
        async def delete(self, file_id):
            await held.wait()  # never set: the deletion is still running when the call ends

    files = SlowFiles({"f1": ("huge.bin", b"0" * (claude.MAX_FILE_BYTES + 1))})
    refused = anthropic.BadRequestError("bad", body=None, response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
    client = SimpleNamespace(messages=Messages([reply([*ran_code("srvtoolu_1", "f1"), tool("toolu_1", "progress",
                                                                                            text="x")]), refused]),
                             files=files)
    standby = Standby()

    async def main():
        backend = StandingBy(ClaudeWorker(SONNET, client=client), lambda: standby, "Claude Sonnet 5.5", "GLM 5")

        async def notify(news):
            pass

        runner = JobRunner(tmp_path, lambda name, **f: None, backend, notify, clock=lambda: datetime.now(SEOUL))
        job = runner.start("Compare the trails")
        await asyncio.sleep(0.3)  # the job failed and is deleting its files
        await asyncio.wait_for(runner.cancel_all(), 2)  # the call ends
        return job

    job = asyncio.run(main())
    assert job.status == "cancelled" and standby.ran == 0


@pytest.mark.parametrize("ending", ["finished", "failed"])
def test_the_end_of_the_call_while_a_job_clears_its_files_lets_the_deletions_finish(tmp_path, ending):
    """Review of the final fixes, finding 3: since a8efc5b the end of the call while a job deleted its code execution
    files from the Files API ended the job as cancelled even when it had finished (its summary lost, its files not
    listed in the app), and in every case the deletions still to come were dropped. A finished job's result stands;
    a failed one still ends as cancelled, never handed to the standby; and the deletions go on for
    FORGET_ON_CANCEL_S after the call's end."""
    import anthropic
    import httpx

    from worker.runner import JobRunner, StandingBy

    release = asyncio.Event()  # the deletion finishes a moment after the call ends

    class SlowFiles(Files):
        async def delete(self, file_id):
            await release.wait()
            self.deleted.append(file_id)

    if ending == "finished":
        files = SlowFiles({"f1": ("prices.csv", b"a,b\n")})  # saved into the job folder
        last = reply([text("Done.")], "end_turn")
    else:
        files = SlowFiles({"f1": ("huge.bin", b"0" * (claude.MAX_FILE_BYTES + 1))})  # too large: nothing produced
        last = anthropic.BadRequestError("bad", body=None, response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))
    client = SimpleNamespace(messages=Messages([reply([*ran_code("srvtoolu_1", "f1"),
                                                       tool("toolu_1", "progress", text="x")]), last], files=files),
                             files=files)
    standby = Standby()
    events: list = []

    async def main():
        backend = StandingBy(ClaudeWorker(SONNET, client=client), lambda: standby, "Claude Sonnet 5.5", "GLM 5")

        async def notify(news):
            pass

        runner = JobRunner(tmp_path, lambda name, **f: events.append((name, f)), backend, notify,
                           clock=lambda: datetime.now(SEOUL))
        job = runner.start("Compare the trails")
        await asyncio.sleep(0.3)  # the job's conversation is over and it is deleting its files
        asyncio.get_running_loop().call_later(0.2, release.set)
        await asyncio.wait_for(runner.cancel_all(), 2)  # the call ends
        return job

    job = asyncio.run(main())
    assert files.deleted == ["f1"]
    if ending == "finished":
        (done,) = [f for name, f in events if name == "job_done"]
        assert job.status == "completed" and done["summary"] == "Done."
        assert [Path(f).name for f in done["files"]] == ["prices.csv", "report.md", "usage.json"]
    else:
        assert job.status == "cancelled" and standby.ran == 0
