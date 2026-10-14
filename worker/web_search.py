"""Web search for the worker: Amazon Nova Web Grounding on Bedrock.

Nova 2 Lite with the nova_grounding system tool searches the web, reads what it
finds and answers with citations. It runs in us-east-1 because Web Grounding is
offered only through US cross-region inference profiles; Bedrock calls to
us-east-1 are allowed for this account, whose other services are pinned to Sydney.

Cost (AWS price list, us-east-1, checked 2026-10-12): 0.03 USD per grounded
request, plus Nova 2 Lite tokens at 0.33 USD per million in and 2.75 USD per
million out. AWS requires the citations to be kept and shown wherever the answer
is shown, so every result carries its sources.

Run: uv run python -m worker.web_search "What is the weather in Seoul this Saturday?"
"""

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from botocore.config import Config
from dotenv import load_dotenv

from voice.aws_session import aws_session

load_dotenv(".env.local")
MODEL = "us.amazon.nova-2-lite-v1:0"
REGION = "us-east-1"
PRICE_PER_REQUEST = 0.03
PRICE_IN_PER_TOKEN = 0.33 / 1_000_000
PRICE_OUT_PER_TOKEN = 2.75 / 1_000_000

INSTRUCTIONS = (
    "Search the web to answer the request below. Start with a summary of at most two short sentences "
    "that someone could say out loud on a phone call, with numbers rounded. Then give the details. "
    "Only state what the sources support.\n\nRequest: "
)


@dataclass
class SearchResult:
    question: str
    answer: str  # the model's text, with [n] where a source backs a passage
    sources: list[dict[str, str]] = field(default_factory=list)  # [{"url": ..., "domain": ...}], numbered from 1
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def summary(self) -> str:
        """The first two sentences, for speaking. Source markers are left out."""
        plain = " ".join(self.answer.replace("\n", " ").split())
        sentences, rest = [], plain
        while rest and len(sentences) < 2:
            cut = min((i for i in (rest.find(". "), rest.find("? "), rest.find("! ")) if i >= 0), default=len(rest) - 1)
            sentences.append(rest[:cut + 1].strip())
            rest = rest[cut + 1:].strip()
        return strip_markers(" ".join(sentences))

    @property
    def cost_usd(self) -> float:
        return PRICE_PER_REQUEST + self.input_tokens * PRICE_IN_PER_TOKEN + self.output_tokens * PRICE_OUT_PER_TOKEN

    def markdown(self) -> str:
        lines = [self.answer.strip(), "", "Sources:"]
        lines += [f"{n}. [{s['domain'] or s['url']}]({s['url']})" for n, s in enumerate(self.sources, 1)]
        return "\n".join(lines) + "\n"


def strip_markers(text: str) -> str:
    """Plain words for speaking: no [n] source markers and no Markdown emphasis or headings."""
    text = text.replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    out, depth = [], 0
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return " ".join("".join(out).split())


def _citations(block: dict[str, Any]) -> list[dict[str, Any]]:
    # Two shapes appear in the docs: {"citationsContent": {"citations": [...]}} as its own block,
    # and {"text": ..., "citationsContent": [...]} on the text block itself.
    found = block.get("citationsContent")
    if isinstance(found, dict):
        found = found.get("citations", [])
    return found if isinstance(found, list) else []


def parse_response(question: str, response: dict[str, Any]) -> SearchResult:
    result = SearchResult(question=question, answer="")
    numbers: dict[str, int] = {}
    parts: list[str] = []
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            parts.append(block["text"])
        marks = []
        for citation in _citations(block):
            web = (citation.get("location") or {}).get("web") or {}
            url = web.get("url")
            if not url:
                continue
            if url not in numbers:
                result.sources.append({"url": url, "domain": web.get("domain", "")})
                numbers[url] = len(result.sources)
            marks.append(f"[{numbers[url]}]")
        if marks:
            parts.append("".join(sorted(set(marks), key=marks.index)))
    result.answer = "".join(parts).strip()
    usage = response.get("usage", {})
    result.input_tokens = usage.get("inputTokens", 0)
    result.output_tokens = usage.get("outputTokens", 0)
    return result


def make_client() -> Any:
    return aws_session().client("bedrock-runtime", region_name=REGION, config=Config(read_timeout=300))


async def web_search(question: str, client: Any | None = None, max_tokens: int = 2048) -> SearchResult:
    client = client or make_client()
    response = await asyncio.to_thread(
        client.converse,
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": INSTRUCTIONS + question}]}],
        toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
        inferenceConfig={"maxTokens": max_tokens},
    )
    return parse_response(question, response)


if __name__ == "__main__":
    found = asyncio.run(web_search(" ".join(sys.argv[1:]) or "What is the weather in Seoul this Saturday?"))
    print(found.markdown())
    print(f"summary to speak: {found.summary}")
    print(f"tokens in {found.input_tokens}, out {found.output_tokens}, about {found.cost_usd:.3f} USD")
