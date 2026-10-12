"""Probe Claude Sonnet 5.5 on Bedrock before building the coordinator.

Checks, on both the Converse API (boto3) and InvokeModel (Anthropic SDK):
thinking settings and effort pass through, thinking blocks come back,
tool calls survive with and without the blocks, edited history is
rejected or not, and how fast the first word arrives.

Run: uv run python probes/bedrock_thinking.py
Writes raw results to workspace/probes/ (git-ignored).
"""

import json
import os
import pathlib
import re
import time
from datetime import datetime

import boto3
from anthropic import AnthropicBedrock
from dotenv import load_dotenv

load_dotenv(".env.local")
PROFILE = os.environ.get("AWS_PROFILE", "bee-call").strip('"')
REGION = os.environ.get("BEDROCK_REGION", "ap-southeast-2").strip('"')
MODEL = "au.anthropic.claude-sonnet-5-5" if REGION == "ap-southeast-2" else "us.anthropic.claude-sonnet-5-5"

OUT_DIR = pathlib.Path("workspace/probes")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = OUT_DIR / f"bedrock-thinking-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

session = boto3.Session(profile_name=PROFILE)
runtime = session.client("bedrock-runtime", region_name=REGION)
claude = AnthropicBedrock(aws_profile=PROFILE, aws_region=REGION)

REASONING_Q = (
    "Two job offers. A pays $120k plus a 10% yearly bonus. B pays $130k with no bonus. "
    "Both raise salary 4% a year. Over 3 years, which pays more in total? Answer in two sentences."
)
TOOL_SPEC = {
    "name": "get_usd_krw_rate",
    "description": "Returns today's US dollar to Korean won exchange rate.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def mask(text: str) -> str:
    return re.sub(r"\d{12}", "<acct>", text)


def record(name: str, **data) -> None:
    with OUT.open("a") as f:
        f.write(json.dumps({"test": name, **data}, default=str) + "\n")
    summary = {k: v for k, v in data.items() if k in ("ok", "error", "blocks", "seconds", "stop", "note")}
    print(f"{name}: {summary}")


def error_text(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error", {})
        return mask(f"{err.get('Code')}: {err.get('Message', '')}")[:300]
    return mask(f"{type(exc).__name__}: {exc}")[:300]


# ---------- Converse API (what the LiveKit AWS plugin uses) ----------

def converse(messages, fields, tools=None, max_tokens=2048):
    kwargs = {
        "modelId": MODEL,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens},
        "additionalModelRequestFields": fields,
    }
    if tools:
        kwargs["toolConfig"] = {"tools": [{"toolSpec": {
            "name": t["name"], "description": t["description"], "inputSchema": {"json": t["input_schema"]},
        }} for t in tools]}
    start = time.time()
    resp = runtime.converse(**kwargs)
    return resp, round(time.time() - start, 2)


def converse_blocks(content):
    kinds = []
    for block in content:
        if "reasoningContent" in block:
            rc = block["reasoningContent"]
            if "reasoningText" in rc:
                rt = rc["reasoningText"]
                kinds.append(f"reasoning(text={len(rt.get('text', ''))},sig={len(rt.get('signature', ''))})")
            else:
                kinds.append("reasoning(redacted)")
        else:
            kinds.append(next(iter(block)))
    return kinds


def run_converse_tests():
    adaptive_low = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    adaptive_high = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
    between_low = {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}
    hello = [{"role": "user", "content": [{"text": "Reply with one short friendly sentence: how are you?"}]}]

    for name, fields in (("converse adaptive+low", adaptive_low), ("converse between_tools+low", between_low)):
        try:
            resp, secs = converse(hello, fields)
            content = resp["output"]["message"]["content"]
            record(name, ok=True, blocks=converse_blocks(content), seconds=secs,
                   stop=resp["stopReason"], usage=resp.get("usage"), content=content)
        except Exception as exc:
            record(name, ok=False, error=error_text(exc))

    # Reasoning turn, then four ways to continue the history.
    first = [{"role": "user", "content": [{"text": REASONING_Q}]}]
    try:
        resp, secs = converse(first, adaptive_high)
        assistant = resp["output"]["message"]
        record("converse reasoning turn", ok=True, blocks=converse_blocks(assistant["content"]), seconds=secs,
               stop=resp["stopReason"], content=assistant["content"])
    except Exception as exc:
        record("converse reasoning turn", ok=False, error=error_text(exc))
        return

    follow = {"role": "user", "content": [{"text": "Thanks. Now say the answer in one word."}]}
    text_blocks = [b for b in assistant["content"] if "text" in b]
    reasoning = [b for b in assistant["content"] if "reasoningContent" in b]
    heard = (text_blocks[0]["text"][:40] if text_blocks else "")
    cut = {"role": "assistant", "content": reasoning + [{"text": heard}]}
    cut_no_reasoning = {"role": "assistant", "content": [{"text": heard}]}
    note = {"role": "user", "content": [{"text": f"(The user interrupted and only heard: \"{heard}\".) "
                                                 "Thanks. Now say the answer in one word."}]}
    variants = {
        "converse continue unchanged": first + [assistant, follow],
        "converse edit text, keep reasoning": first + [cut, follow],
        "converse edit text, drop reasoning": first + [cut_no_reasoning, follow],
        "converse keep turn, add heard-note": first + [assistant, note],
    }
    for name, msgs in variants.items():
        try:
            resp, secs = converse(msgs, adaptive_low, max_tokens=512)
            record(name, ok=True, blocks=converse_blocks(resp["output"]["message"]["content"]), seconds=secs,
                   stop=resp["stopReason"])
        except Exception as exc:
            record(name, ok=False, error=error_text(exc))

    # Tool loop: keep or drop the reasoning block before the tool call.
    ask = [{"role": "user", "content": [{"text": "Think it through, then use the tool: how many won is 250 dollars?"}]}]
    try:
        resp, secs = converse(ask, adaptive_high, tools=[TOOL_SPEC])
        call = resp["output"]["message"]
        record("converse tool call", ok=True, blocks=converse_blocks(call["content"]), seconds=secs,
               stop=resp["stopReason"])
    except Exception as exc:
        record("converse tool call", ok=False, error=error_text(exc))
        return
    uses = [b["toolUse"] for b in call["content"] if "toolUse" in b]
    if not uses:
        record("converse tool result", ok=False, note="model did not call the tool")
        return
    result = {"role": "user", "content": [{"toolResult": {
        "toolUseId": uses[0]["toolUseId"], "content": [{"json": {"usd_krw": 1385.2}}]}}]}
    stripped = {"role": "assistant", "content": [b for b in call["content"] if "reasoningContent" not in b]}
    for name, msgs in (("converse tool result, keep reasoning", ask + [call, result]),
                       ("converse tool result, drop reasoning", ask + [stripped, result])):
        try:
            resp, secs = converse(msgs, adaptive_high, tools=[TOOL_SPEC], max_tokens=1024)
            record(name, ok=True, blocks=converse_blocks(resp["output"]["message"]["content"]), seconds=secs,
                   stop=resp["stopReason"])
        except Exception as exc:
            record(name, ok=False, error=error_text(exc))


# ---------- InvokeModel via the Anthropic SDK (Messages API body) ----------

def sdk_blocks(content):
    kinds = []
    for block in content:
        if block.type == "thinking":
            kinds.append(f"thinking(text={len(block.thinking or '')},sig={len(block.signature or '')})")
        else:
            kinds.append(block.type)
    return kinds


def run_sdk_tests():
    hello = [{"role": "user", "content": "Reply with one short friendly sentence: how are you?"}]
    for name, thinking in (("sdk adaptive+low", {"type": "adaptive"}), ("sdk between_tools+low", {"type": "between_tools"})):
        try:
            start = time.time()
            resp = claude.messages.create(model=MODEL, max_tokens=2048, thinking=thinking,
                                          output_config={"effort": "low"}, messages=hello)
            record(name, ok=True, blocks=sdk_blocks(resp.content), seconds=round(time.time() - start, 2),
                   stop=resp.stop_reason, usage=resp.usage.model_dump())
        except Exception as exc:
            record(name, ok=False, error=error_text(exc))

    first = [{"role": "user", "content": REASONING_Q}]
    try:
        resp = claude.messages.create(model=MODEL, max_tokens=4096, thinking={"type": "adaptive"},
                                      output_config={"effort": "high"}, messages=first)
        assistant = [b.model_dump(exclude_none=True) for b in resp.content]
        record("sdk reasoning turn", ok=True, blocks=sdk_blocks(resp.content), stop=resp.stop_reason)
    except Exception as exc:
        record("sdk reasoning turn", ok=False, error=error_text(exc))
        return

    texts = [b for b in assistant if b["type"] == "text"]
    heard = texts[0]["text"][:40] if texts else ""
    cut = [b for b in assistant if b["type"] == "thinking"] + [{"type": "text", "text": heard}]
    variants = {
        "sdk edit text, keep thinking": first + [{"role": "assistant", "content": cut},
                                                 {"role": "user", "content": "Thanks. One word?"}],
        "sdk mid-conversation system message": first + [
            {"role": "assistant", "content": assistant},
            {"role": "system", "content": "The user is now looking at the app screen."},
            {"role": "user", "content": "Thanks. One word?"},
        ],
    }
    for name, msgs in variants.items():
        try:
            resp = claude.messages.create(model=MODEL, max_tokens=512, thinking={"type": "adaptive"},
                                          output_config={"effort": "low"}, messages=msgs)
            record(name, ok=True, blocks=sdk_blocks(resp.content), stop=resp.stop_reason)
        except Exception as exc:
            record(name, ok=False, error=error_text(exc))


# ---------- Time to first text token, three short turns each ----------

def first_token_seconds(path: str, thinking_type: str) -> float | None:
    prompt = "Say hi back in one short sentence."
    start = time.time()
    if path == "converse":
        resp = runtime.converse_stream(
            modelId=MODEL, messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 512},
            additionalModelRequestFields={"thinking": {"type": thinking_type}, "output_config": {"effort": "low"}})
        for event in resp["stream"]:
            delta = event.get("contentBlockDelta", {}).get("delta", {})
            if "text" in delta:
                return round(time.time() - start, 2)
    else:
        stream = claude.messages.create(model=MODEL, max_tokens=512, stream=True,
                                        thinking={"type": thinking_type}, output_config={"effort": "low"},
                                        messages=[{"role": "user", "content": prompt}])
        for event in stream:
            if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                return round(time.time() - start, 2)
    return None


def run_latency():
    for path in ("converse", "sdk"):
        for thinking_type in ("adaptive", "between_tools"):
            samples = []
            for _ in range(3):
                try:
                    samples.append(first_token_seconds(path, thinking_type))
                except Exception as exc:
                    samples.append(error_text(exc))
            ok = all(isinstance(s, float) for s in samples)
            record(f"first token {path} {thinking_type}+low", ok=ok, note=samples)


if __name__ == "__main__":
    print(f"model {MODEL} in {REGION}, profile {PROFILE}; raw results -> {OUT}")
    run_converse_tests()
    run_sdk_tests()
    run_latency()
