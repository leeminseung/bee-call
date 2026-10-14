"""Tests for the web search worker, with canned Bedrock responses (no network, no cost)."""

import asyncio

from tests.helpers import seoul_now

from worker.runner import Job
from worker.search import SearchWorker
from worker.web_search import SearchResult, parse_response

NOVA1_SHAPE = {"output": {"message": {"content": [
    {"text": "Rain is unlikely on Saturday morning in Seoul. "},
    {"citationsContent": {"citations": [{"location": {"web": {"url": "https://weather.example/seoul", "domain": "weather.example"}}}]}},
    {"text": "Showers may start after 2 pm. Highs near 19 degrees."},
    {"citationsContent": {"citations": [{"location": {"web": {"url": "https://news.example/a", "domain": "news.example"}}},
                                        {"location": {"web": {"url": "https://weather.example/seoul", "domain": "weather.example"}}}]}},
]}}, "usage": {"inputTokens": 1200, "outputTokens": 300}}

NOVA2_SHAPE = {"output": {"message": {"content": [
    {"text": "Gwanaksan has the gentler route.", "citationsContent": [
        {"location": {"web": {"url": "https://trails.example/gwanak", "domain": "trails.example"}}}]},
]}}, "usage": {"inputTokens": 900, "outputTokens": 100}}


def test_parse_numbers_sources_once_and_keeps_them_with_the_answer():
    result = parse_response("Seoul weather Saturday", NOVA1_SHAPE)
    assert [s["url"] for s in result.sources] == ["https://weather.example/seoul", "https://news.example/a"]
    assert result.answer == ("Rain is unlikely on Saturday morning in Seoul. [1]"
                             "Showers may start after 2 pm. Highs near 19 degrees.[2][1]")
    assert result.summary == "Rain is unlikely on Saturday morning in Seoul. Showers may start after 2 pm."
    assert "1. [weather.example](https://weather.example/seoul)" in result.markdown()
    assert round(result.cost_usd, 4) == round(0.03 + 1200 * 0.33e-6 + 300 * 2.75e-6, 4)


def test_summary_has_no_markdown_for_speaking():
    result = SearchResult(question="q", answer="There is a **low chance of rain** in Seoul[1]. Highs near **19**. More text.")
    assert result.summary == "There is a low chance of rain in Seoul. Highs near 19."


def test_parse_reads_citations_attached_to_the_text_block():
    result = parse_response("trail", NOVA2_SHAPE)
    assert result.answer == "Gwanaksan has the gentler route.[1]" and result.sources[0]["domain"] == "trails.example"


def test_search_worker_writes_the_report_and_relays_the_summary(tmp_path):
    async def fake_search(question):
        return parse_response(question, NOVA1_SHAPE)

    async def main():
        job = Job(job_id="01", dir=tmp_path, now=seoul_now(), task="Will it rain in Seoul on Saturday?")
        progress, relayed = [], []

        async def relay(text):
            relayed.append(text)

        summary = await SearchWorker(fake_search).run(job, progress.append, relay)
        report = (tmp_path / "report.md").read_text()
        assert "Task: Will it rain in Seoul on Saturday?" in report and "Sources:" in report
        assert relayed == ["Rain is unlikely on Saturday morning in Seoul. Showers may start after 2 pm."]
        assert progress == ["Searching the web", "Read 2 sources"] and "2 sources" in summary

    asyncio.run(main())
