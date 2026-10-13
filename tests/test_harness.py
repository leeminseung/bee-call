"""Tests for the AgentCore Harness worker with a scripted harness (no network, no cost)."""

import asyncio
import json

from worker.harness import INLINE_FUNCTIONS, HarnessWorker, harness_definition, read_round
from worker.runner import Job
from worker.web_search import SearchResult


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
    nova = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "global.amazon.nova-2-lite-v1:0")
    assert "Use the code interpreter" in nova["systemPrompt"][0]["text"]


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
        job = Job(job_id="01", dir=tmp_path, task="Compare the trails")
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
    """Runs one job through the real JobRunner with a scripted harness; returns (events, relays)."""
    from worker.runner import JobRunner

    events, relays = [], []

    async def on_relay(job_id, text, failed=False):
        relays.append((text, failed))

    async def main():
        runner = JobRunner(tmp_path, lambda name, **f: events.append((name, f)),
                           HarnessWorker("arn:harness", client=FakeHarness(rounds), search=search), on_relay)
        runner.start("Compare the trails")
        await asyncio.gather(*runner.tasks.values())

    asyncio.run(main())
    return events, relays


def test_a_worker_that_hits_a_limit_fails_and_the_user_hears_it(tmp_path):
    events, relays = run_job(tmp_path, [assistant(("text", "Still going"), stop="max_iterations_exceeded")])
    assert [name for name, _ in events][-1] == "job_failed"
    assert relays == [("the worker stopped before finishing (max_iterations_exceeded)", True)]


def test_an_error_inside_the_stream_fails_the_job(tmp_path):
    from botocore.exceptions import EventStreamError

    def broken():
        yield {"messageStart": {"role": "assistant"}}
        raise EventStreamError({"Error": {"Code": "internalServerException", "Message": "boom"}}, "InvokeHarness")

    events, relays = run_job(tmp_path, [broken()])
    assert [name for name, _ in events][-1] == "job_failed" and relays[0][1] is True and "boom" in relays[0][0]


def test_a_job_that_never_relayed_still_tells_the_user_its_summary(tmp_path):
    events, relays = run_job(tmp_path, [assistant(("text", "Gwanaksan is gentler."), stop="end_turn")])
    assert relays == [("Gwanaksan is gentler.", False)] and ("job_done" in [name for name, _ in events])


def test_requests_match_the_api_model():
    import botocore.session
    from botocore.validate import ParamValidator
    from worker.harness import update_request

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    definition = harness_definition("arn:aws:iam::123456789012:role/bee-call-worker", "global.amazon.nova-2-lite-v1:0")
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
        job = Job(job_id="01", dir=tmp_path, task="chart")
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

        job = Job(job_id="01", dir=tmp_path, task="What day is Saturday?")
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
        warm = asyncio.create_task(worker.keep_warm())
        while not harness.requests:  # the warm-up went out
            await asyncio.sleep(0.01)
        async def relay(text):
            pass
        summary = await worker.run(Job(job_id="01", dir=tmp_path, task="Anything"), lambda t: None, relay)
        warm.cancel()
        await warm
        return summary

    assert asyncio.run(main()) == "Done."
    warm_up, job = harness.requests
    assert warm_up["messages"][0]["content"][0]["text"].startswith("Warm-up before a phone call")
    assert warm_up["runtimeSessionId"] == job["runtimeSessionId"]


def test_a_warm_up_that_calls_a_tool_is_told_not_to(tmp_path):
    harness = FakeHarness([
        assistant(("tool", "w1", "progress", ['{"text": "warming"}'])),
        assistant(("text", "ready"), stop="end_turn"),
    ])

    async def main():
        worker = HarnessWorker("arn:harness", client=harness, search=fake_search)
        await worker._ping("beecall-warm-test-0123456789abcdef0123456789")

    asyncio.run(main())
    answer = harness.requests[1]["messages"][0]["content"][0]["toolResult"]
    assert answer["toolUseId"] == "w1" and answer["status"] == "error"
