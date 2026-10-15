"""Tests for the AgentCore Harness worker with a scripted harness (no network, no cost)."""

import asyncio
import json
from datetime import datetime

import pytest

from voice.costs import SpendLog
from worker.harness import INLINE_FUNCTIONS, HarnessWorker, harness_definition, read_round
from worker.runner import Job
from worker.web_search import SearchResult
from tests.helpers import SEOUL


def assistant(*blocks, stop="tool_use"):
    """Stream events for one assistant message. Blocks: ("text", s) or ("tool", id, name, input_json_parts)."""
    events = [{"messageStart": {"role": "assistant"}}]
    for i, block in enumerate(blocks):
        if block[0] == "text":
            events += [{"contentBlockStart": {"contentBlockIndex": i, "start": {}}},
                       {"contentBlockDelta": {"contentBlockIndex": i, "delta": {"text": block[1]}}}]
        else:
            _, tool_id, name, parts = block
            events.append({"contentBlockStart": {"contentBlockIndex": i, "start": {"toolUse": {"toolUseId": tool_id, "name": name}}}})
            events += [{"contentBlockDelta": {"contentBlockIndex": i, "delta": {"toolUse": {"input": p}}}} for p in parts]
        events.append({"contentBlockStop": {"contentBlockIndex": i}})
    events.append({"messageStop": {"stopReason": stop}})
    events.append({"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}}})
    return events


def server_tool_result(tool_id):
    return [{"messageStart": {"role": "user"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolResult": {"toolUseId": tool_id, "status": "success"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"text": "42"}]}}},
            {"contentBlockStop": {"contentBlockIndex": 0}}, {"messageStop": {"stopReason": "tool_result"}}]


class FakeHarness:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []

    def invoke_harness(self, **params):
        self.requests.append(json.loads(json.dumps(params)))
        return {"stream": iter(self.rounds.pop(0))}


async def fake_search(question):
    return SearchResult(question=question, answer="Gwanaksan is gentler.[1]",
                        sources=[{"url": "https://trails.example/g", "domain": "trails.example"}])


def test_definition_has_the_inline_functions_and_the_code_interpreter():
    d = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "global.amazon.nova-2-lite-v1:0")
    names = [t["name"] for t in d["tools"]]
    assert names == list(INLINE_FUNCTIONS) + ["code_interpreter"]
    assert d["memory"] == {"disabled": {}} and d["model"]["bedrockModelConfig"]["modelId"] == "global.amazon.nova-2-lite-v1:0"
    assert all(t["config"]["inlineFunction"]["inputSchema"]["type"] == "object" for t in d["tools"] if t["type"] == "inline_function")


def test_models_that_fail_with_the_code_interpreter_get_no_code_interpreter():
    d = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "global.moonshotai.kimi-k3")
    assert [t["name"] for t in d["tools"]] == list(INLINE_FUNCTIONS)
    prompt = d["systemPrompt"][0]["text"]
    assert "code interpreter" not in prompt and "Work out calculations step by step" in prompt
    for model in ("global.amazon.nova-2-lite-v1:0", "zai.glm-5"):
        other = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", model)
        assert "Use the code interpreter" in other["systemPrompt"][0]["text"], model
        assert other["tools"][-1]["name"] == "code_interpreter", model


def test_the_worker_has_room_for_long_research_and_reports():
    """The user (2026-10-15): background jobs need not be quick; only the call AI is held to short waits. A report
    fits in one model call, a job gets twice the steps and time, and GLM 5 reasons before it acts."""
    from worker.harness import (HARNESS_TIMEOUT_S, INVOKE_READ_TIMEOUT_S, KEEP_WARM_S, MAX_ROUNDS,
                                WEB_SEARCH_LONGEST_S)

    d = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "zai.glm-5")
    model = d["model"]["bedrockModelConfig"]
    # GLM 5 on Bedrock takes a prompt and maxTokens of 202,752 tokens together (probed 2026-10-15), so a long
    # job's conversation keeps about 170K tokens of room next to a 32K-token answer.
    assert model["maxTokens"] == 32768
    # Probed: GLM 5 reasons only when asked; the harness puts additionalParams into its Converse request as they are.
    assert model["additionalParams"] == {"additionalModelRequestFields": {"reasoning_effort": "high"}}
    assert (d["maxIterations"], d["timeoutSeconds"], MAX_ROUNDS) == (120, 1800, 80) == (120, HARNESS_TIMEOUT_S, 80)
    lifecycle = d["environment"]["agentCoreRuntimeEnvironment"]["lifecycleConfiguration"]
    # A session outlives the longest wait between a job's rounds: this Mac runs one round's calls one after another,
    # so two web searches that each take their longest (with their one retry) still fit; the warm-up keeps it from
    # idling out between jobs.
    assert lifecycle["idleRuntimeSessionTimeout"] > 2 * WEB_SEARCH_LONGEST_S
    assert KEEP_WARM_S < lifecycle["idleRuntimeSessionTimeout"]
    # A session is warmed when the call starts and serves its jobs until it ends: AgentCore's own limit, 8 hours,
    # so a job late in a long call is never cut short.
    assert lifecycle["maxLifetime"] == 28800
    assert INVOKE_READ_TIMEOUT_S > HARNESS_TIMEOUT_S  # this Mac never gives up on a round the harness still runs
    other = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "global.amazon.nova-2-lite-v1:0")
    assert "additionalParams" not in other["model"]["bedrockModelConfig"]  # the switch is GLM 5's own


def test_the_worker_s_reasoning_is_counted_in_its_usage(tmp_path):
    """How the reasoning switch shows in a job: the harness streams GLM 5's reasoning, and the job's usage.json
    counts its characters."""
    reasoning = [{"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
                 {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "The user "}}}},
                 {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "wants trails."}}}},
                 {"contentBlockStop": {"contentBlockIndex": 0}}]
    finished = assistant(("text", "Gwanaksan is gentler."), stop="end_turn")
    events = finished[:1] + reasoning + finished[1:]
    assert read_round(events).reasoning_chars == len("The user wants trails.")
    assert read_round(events).text == "Gwanaksan is gentler."

    async def main():
        job = Job(job_id="01", dir=tmp_path, now=datetime.now(SEOUL), task="Compare the trails")

        async def relay(text):
            pass

        await HarnessWorker("arn:harness", client=FakeHarness([events]), search=fake_search).run(job, lambda t: None, relay)

    asyncio.run(main())
    assert json.loads((tmp_path / "usage.json").read_text())["reasoning_chars"] == len("The user wants trails.")


def test_read_round_keeps_only_inline_calls_still_waiting():
    events = (assistant(("tool", "t_ci", "code_interpreter", ['{"code": "6*7"}'])) + server_tool_result("t_ci")
              + assistant(("text", "Now searching. "), ("tool", "t_ws", "web_search", ['{"quest', 'ion": "trails"}'])))
    found = read_round(events)
    assert [t.name for t in found.pending] == ["web_search"] and found.pending[0].input == {"question": "trails"}
    assert found.text == "Now searching. " and found.stop_reason == "tool_use"


def test_worker_serves_inline_functions_until_the_agent_finishes(tmp_path):
    report = "# Trails\n\nGwanaksan is gentler. Source: https://trails.example/g\n"
    harness = FakeHarness([
        assistant(("tool", "t1", "progress", ['{"text": "Searching trails"}'])),
        assistant(("tool", "t2", "web_search", ['{"question": "easiest trail Gwanaksan or Bukhansan"}'])),
        assistant(("tool", "t3", "relay_to_user", [json.dumps({"text": "Gwanaksan is the gentler hike."})]),
                  ("tool", "t4", "save_file", [json.dumps({"name": "report.md", "content": report})])),
        assistant(("tool", "t5", "save_file", [json.dumps({"name": "../escape.md", "content": "x"})])),
        assistant(("text", "Compared the two trails; Gwanaksan is gentler."), stop="end_turn"),
    ])

    async def main():
        job = Job(job_id="01", dir=tmp_path, now=datetime.now(SEOUL), task="Compare the trails")
        progress, relayed = [], []

        async def relay(text):
            relayed.append(text)

        summary = await HarnessWorker("arn:harness", client=harness, search=fake_search).run(job, progress.append, relay)
        assert summary == "Compared the two trails; Gwanaksan is gentler."
        assert progress == ["Searching trails"] and relayed == ["Gwanaksan is the gentler hike."]
        assert (tmp_path / "report.md").read_text() == report and not (tmp_path.parent / "escape.md").exists()
        assert json.loads((tmp_path / "usage.json").read_text())["output_tokens"] == 50

    asyncio.run(main())
    first, second, third, fourth, fifth = harness.requests
    opening = first["messages"][0]["content"][0]["text"]
    assert opening.startswith("Job 01 (it is now ") and opening.endswith("): Compare the trails")
    assert len({r["runtimeSessionId"] for r in harness.requests}) == 1 and len(first["runtimeSessionId"]) >= 33
    assert second["messages"][0]["content"][0]["toolResult"]["toolUseId"] == "t1"
    assert "trails.example" in third["messages"][0]["content"][0]["toolResult"]["content"][0]["text"]
    assert [b["toolResult"]["toolUseId"] for b in fourth["messages"][0]["content"]] == ["t3", "t4"]
    assert fifth["messages"][0]["content"][0]["toolResult"]["status"] == "error"


def run_job(tmp_path, rounds, search=fake_search):
    """Runs one job through the real JobRunner with a scripted harness; returns (events, notifications)
    with each notification as (status, summary, error)."""
    from worker.runner import JobRunner

    events, news = [], []

    async def on_notify(n):
        news.append((n.status, n.summary, n.error))

    async def main():
        runner = JobRunner(tmp_path, lambda name, **f: events.append((name, f)),
                           HarnessWorker("arn:harness", client=FakeHarness(rounds), search=search), on_notify,
                           clock=lambda: datetime.now(SEOUL))
        runner.start("Compare the trails")
        await asyncio.gather(*runner.tasks.values())

    asyncio.run(main())
    return events, news


def test_a_worker_that_hits_a_limit_fails_and_the_user_hears_it(tmp_path):
    events, news = run_job(tmp_path, [assistant(("text", "Still going"), stop="max_iterations_exceeded")])
    assert [name for name, _ in events][-1] == "job_failed"
    assert news == [("failed", None, "the worker stopped before finishing (max_iterations_exceeded)")]


def test_an_error_inside_the_stream_fails_the_job(tmp_path):
    from botocore.exceptions import EventStreamError

    def broken():
        yield {"messageStart": {"role": "assistant"}}
        raise EventStreamError({"Error": {"Code": "internalServerException", "Message": "boom"}}, "InvokeHarness")

    events, news = run_job(tmp_path, [broken()])
    assert [name for name, _ in events][-1] == "job_failed" and news[0][0] == "failed" and "boom" in news[0][2]


def test_a_job_that_never_relayed_still_tells_the_user_its_summary(tmp_path):
    events, news = run_job(tmp_path, [assistant(("text", "Gwanaksan is gentler."), stop="end_turn")])
    assert news == [("completed", "Gwanaksan is gentler.", None)] and ("job_done" in [name for name, _ in events])


def test_requests_match_the_api_model():
    import botocore.session
    from botocore.validate import ParamValidator
    from worker.harness import update_request

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    for model_id in ("global.amazon.nova-2-lite-v1:0", "zai.glm-5"):
        definition = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", model_id)
        for operation, params in (("CreateHarness", definition), ("UpdateHarness", update_request(definition, "h-123"))):
            report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
            assert not report.has_errors(), report.generate_report()


def test_save_chart_draws_svg_and_file_names_are_checked(tmp_path):
    chart = json.dumps({"name": "rain_by_hour", "title": "Rain chance", "kind": "line",
                        "series": [{"label": "Seoul", "points": [[8, 10], [12, 15], [16, 40]]}]})
    harness = FakeHarness([
        assistant(("tool", "c1", "save_chart", [chart])),
        assistant(("tool", "c2", "save_file", [json.dumps({"name": "REQUEST.md", "content": "x"})]),
                  ("tool", "c3", "save_file", [json.dumps({"name": "report.md\n", "content": "x"})])),
        assistant(("text", "Done."), stop="end_turn"),
    ])

    async def main():
        job = Job(job_id="01", dir=tmp_path, now=datetime.now(SEOUL), task="chart")
        (tmp_path / "request.md").write_text("original")

        async def relay(text):
            pass

        await HarnessWorker("arn:harness", client=harness, search=fake_search).run(job, lambda t: None, relay)

    asyncio.run(main())
    svg = (tmp_path / "rain_by_hour.svg").read_text()
    assert svg.startswith("<svg") and "Rain chance" in svg and "polyline" in svg
    assert (tmp_path / "request.md").read_text() == "original"
    results = [b["toolResult"]["status"] for b in harness.requests[2]["messages"][0]["content"]]
    assert results == ["error", "error"]


def test_a_call_to_a_tool_that_does_not_exist_gets_an_error_and_the_job_goes_on(tmp_path):
    harness = FakeHarness([
        assistant(("tool", "u1", "get_current_date", ["{}"])),
        assistant(("text", "Finished without the date tool."), stop="end_turn"),
    ])

    async def main():
        async def relay(text):
            pass

        job = Job(job_id="01", dir=tmp_path, now=datetime.now(SEOUL), task="What day is Saturday?")
        return await HarnessWorker("arn:harness", client=harness, search=fake_search).run(job, lambda t: None, relay)

    assert asyncio.run(main()) == "Finished without the date tool."
    answer = harness.requests[1]["messages"][0]["content"][0]["toolResult"]
    assert answer["toolUseId"] == "u1" and answer["status"] == "error"
    assert "There is no tool named get_current_date" in answer["content"][0]["text"]


def test_a_warm_session_is_ready_before_the_first_job_and_the_job_uses_it(tmp_path):
    harness = FakeHarness([
        assistant(("text", "ready"), stop="end_turn"),  # the warm-up at call start
        assistant(("text", "Done."), stop="end_turn"),  # the job
    ])

    async def main():
        worker = HarnessWorker("arn:harness", client=harness, search=fake_search)
        warm = asyncio.create_task(worker.keep_warm(SpendLog(tmp_path)))
        while not harness.requests:  # the warm-up went out
            await asyncio.sleep(0.01)
        async def relay(text):
            pass
        summary = await worker.run(Job(job_id="01", dir=tmp_path, now=datetime.now(SEOUL), task="Anything"), lambda t: None, relay)
        warm.cancel()
        await warm
        return summary

    assert asyncio.run(main()) == "Done."
    warm_up, job = harness.requests
    assert warm_up["messages"][0]["content"][0]["text"].startswith("Warm-up before a phone call")
    assert warm_up["runtimeSessionId"] == job["runtimeSessionId"]
    (spent,) = [json.loads(line) for line in (tmp_path / "state" / "spend.jsonl").read_text().splitlines()]
    assert (spent["what"], spent["input_tokens"], spent["output_tokens"]) == ("worker_warm_up", 100, 10)


def test_a_warm_up_that_calls_a_tool_is_told_not_to(tmp_path):
    harness = FakeHarness([
        assistant(("tool", "w1", "progress", ['{"text": "warming"}'])),
        assistant(("text", "ready"), stop="end_turn"),
    ])

    async def main():
        # Through the warm-up a call runs (keep_warm), not its private step (server review 16).
        worker = HarnessWorker("arn:harness", client=harness, search=fake_search)
        warming = asyncio.create_task(worker.keep_warm(SpendLog(tmp_path)))
        for _ in range(100):
            if len(harness.requests) == 2:
                break
            await asyncio.sleep(0.01)
        warming.cancel()
        await asyncio.gather(warming, return_exceptions=True)

    asyncio.run(main())
    answer = harness.requests[1]["messages"][0]["content"][0]["toolResult"]
    assert answer["toolUseId"] == "w1" and answer["status"] == "error"


# ---------- what a job costs reaches the daily budget (server review finding 4, repro s10) ----------

def searching_rounds(n: int, tokens: int = 1_000_000):
    """n rounds that each search the web once and report `tokens` input tokens."""
    rounds = [assistant(("tool", f"s{i}", "web_search", [json.dumps({"question": f"q{i}"})])) for i in range(n)]
    for r in rounds:
        r[-1] = {"metadata": {"usage": {"inputTokens": tokens, "outputTokens": 0}}}
    return rounds


def run_in_a_call(tmp_path, harness, cancel_after: float | None = None) -> str:
    """Runs one job in a call folder of today; returns how it ended."""
    from datetime import datetime
    from worker.runner import JobRunner

    call = tmp_path / datetime.now(SEOUL).strftime("%Y-%m-%d") / "0100"
    call.mkdir(parents=True)

    async def main():
        async def notify(news):
            pass

        runner = JobRunner(call, lambda *a, **k: None, HarnessWorker("arn:harness", client=harness, search=fake_search),
                           notify, clock=lambda: datetime.now(SEOUL))
        runner.start("research something")
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            await runner.cancel_all()  # the call ended
        await asyncio.gather(*runner.tasks.values(), return_exceptions=True)
        return runner.jobs["01"].status

    return asyncio.run(main())


def test_a_failed_job_still_counts_what_it_used(tmp_path):
    from datetime import datetime
    from voice.costs import WEB_SEARCH_USD, spent_today

    rounds = searching_rounds(5) + [assistant(("text", "Still going"), stop="max_iterations_exceeded")]
    assert run_in_a_call(tmp_path, FakeHarness(rounds)) == "failed"
    rounds_cost = 5 * 1.00 + 5 * WEB_SEARCH_USD + (100 / 1_000_000) * 1.00 + (10 / 1_000_000) * 3.20  # the last round
    assert spent_today(tmp_path, datetime.now(SEOUL)) == pytest.approx(rounds_cost)


class SlowHarness(FakeHarness):
    """Each round takes a while, as the real one does."""

    def invoke_harness(self, **params):
        import time
        time.sleep(0.2)
        return super().invoke_harness(**params)


def test_a_job_cancelled_when_the_call_ends_still_counts_its_rounds_so_far(tmp_path):
    from datetime import datetime
    from voice.costs import WEB_SEARCH_USD, spent_today

    assert run_in_a_call(tmp_path, SlowHarness(searching_rounds(10)), cancel_after=0.5) == "cancelled"
    spent = spent_today(tmp_path, datetime.now(SEOUL))
    assert spent >= 1.00 + WEB_SEARCH_USD  # at least the rounds that finished before the call ended


class HeldHarness(FakeHarness):
    """Round `held` (counting from 0) runs until the test lets it end, as a long round does while the call ends."""

    def __init__(self, rounds, held: int):
        import threading

        super().__init__(rounds)
        self.held, self.running, self.release = held, threading.Event(), threading.Event()

    def invoke_harness(self, **params):
        if len(self.requests) == self.held:
            self.running.set()
            self.release.wait(5)
        return super().invoke_harness(**params)


async def until(check, seconds: float = 3.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)


def test_a_round_still_running_when_the_call_ends_is_counted(tmp_path):
    """Review of this round's fixes, finding 8: the harness runs a round it started to its end, and it is paid for,
    even when the job waiting for it is cancelled because the call ended. It counts at once as the round before it
    (each round reads the whole session again), and as what it really used once it ends."""
    from worker.runner import JobRunner

    rounds = searching_rounds(3)
    rounds[1][-1] = {"metadata": {"usage": {"inputTokens": 1_500_000, "outputTokens": 0}}}
    harness = HeldHarness(rounds, held=1)
    usage = tmp_path / "jobs" / "01" / "usage.json"

    async def main():
        async def notify(news):
            pass

        runner = JobRunner(tmp_path, lambda *a, **k: None, HarnessWorker("arn:harness", client=harness,
                                                                        search=fake_search),
                           notify, clock=lambda: datetime.now(SEOUL))
        runner.start("research something")
        await until(harness.running.is_set)
        await runner.cancel_all()  # the call ended during the second round
        at_once = json.loads(usage.read_text())
        harness.release.set()
        await until(lambda: json.loads(usage.read_text())["input_tokens"] == 2_500_000)
        return at_once, json.loads(usage.read_text())

    at_once, at_end = asyncio.run(main())
    assert (at_once["status"], at_once["input_tokens"], at_once.get("estimated")) == ("cancelled", 2_000_000, True)
    assert (at_end["status"], at_end["input_tokens"], at_end.get("estimated")) == ("cancelled", 2_500_000, None)


def test_a_round_that_ends_as_its_wait_is_cancelled_is_counted_after_the_guess(tmp_path):
    """Second review of this round's fixes: were the round's own count to land before the guess, the guess would
    stay marked as one ("estimated") though the count is right."""
    import threading
    import time

    release, order = threading.Event(), []

    class Harness(FakeHarness):
        def invoke_harness(self, **params):
            release.wait(5)
            return super().invoke_harness(**params)

    worker = HarnessWorker("arn:harness", client=Harness([assistant(("text", "done"), stop="end_turn")]),
                           search=fake_search)

    def cancelled():
        order.append("guess")
        release.set()  # the round ends just now, while the guess is being counted
        time.sleep(0.3)  # time for the round's thread to count itself, were it free to
        order.append("guess counted")

    async def main():
        waiting = asyncio.create_task(worker._paid_round("s", [], cancelled=cancelled,
                                                         late=lambda found: order.append("count")))
        await asyncio.sleep(0.1)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await until(lambda: "count" in order)

    asyncio.run(main())
    assert order == ["guess", "guess counted", "count"]


def test_a_warm_up_still_running_when_the_call_ends_is_counted(tmp_path):
    harness = HeldHarness([assistant(("text", "ready"), stop="end_turn")], held=0)
    spend = tmp_path / "state" / "spend.jsonl"

    async def main():
        worker = HarnessWorker("arn:harness", client=harness, search=fake_search)
        warm = asyncio.create_task(worker.keep_warm(SpendLog(tmp_path)))
        await until(harness.running.is_set)
        warm.cancel()  # the call ended during the warm-up
        await warm
        harness.release.set()
        await until(spend.exists)

    asyncio.run(main())
    (spent,) = [json.loads(line) for line in spend.read_text().splitlines()]
    assert (spent["what"], spent["input_tokens"], spent["output_tokens"]) == ("worker_warm_up", 100, 10)


def test_the_worker_smoke_test_builds_its_job(monkeypatch, capsys):
    """scripts/deploy_worker.py --smoke runs one job through the worker (a fake one here)."""
    from scripts import deploy_worker

    class Worker:
        def __init__(self, arn):
            pass

        async def run(self, job, progress, relay):
            (job.dir / "report.md").write_text("ok")
            (job.dir / "usage.json").write_text('{"output_tokens": 90, "reasoning_chars": 412}')
            return f"ran at {job.now.tzinfo}"

    monkeypatch.setattr(deploy_worker, "HarnessWorker", Worker)
    asyncio.run(deploy_worker.smoke("arn:harness"))
    out = capsys.readouterr().out
    assert "ran at Asia/Seoul" in out and "'reasoning_chars': 412" in out  # how a deploy shows the model reasoned
