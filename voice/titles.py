"""A call's one-line title, written after the call (contract section 1, decision 7).

The voice server runs it in a call's shutdown, beside the Bee retry, once the record is closed: one Claude Haiku 5.5
request on Anthropic's API (ANTHROPIC_API_KEY) over what was said, in the call's language, only when the person
spoke, with a hard limit of TITLE_LIMIT_S. The title is recorded as an after-call call_titled event in the call's
events.jsonl, and the request's tokens go to the spend log (voice/costs.py). On any failure (no key, the limit, an
error, a refusal, an empty answer) nothing is written, and the call view's title falls back to the call's topic or
the person's first sentence (voice/call_view.py).

Older calls: uv run python -m voice.titles data/people/<id>   titles each ended call of that person without one
"""

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from voice.call_folders import EVENTS, call_folders, session_id
from voice.call_view import items_of, read_call, read_record
from voice.costs import SpendLog
from voice.language import CallLanguage, call_language

log = logging.getLogger("bee-call.titles")

TITLE_MODEL = "claude-haiku-5-5"
TITLE_LIMIT_S = 5.0  # the whole request, the answer included (decision 7)
TITLE_CHARS = 60  # as long as a title made from the person's first sentence
SAID_CHARS = 8000  # what was said, as the model reads it: a long call keeps its start and its end
SYSTEM = ("You write the title of one phone call between a person and Bee Call, their voice assistant, for the "
          "person's list of calls. Write one line in {language}, at most {chars} characters: what the call was "
          "about, in a few words, as the person would name it, such as a question they asked or what they sorted "
          "out. No quotation marks, no full stop, no speaker names, and never words like \"call\" or \"conversation "
          "about\". Answer with the title only.")
QUOTES = "\"'“”‘’「」『』"


def anthropic_client() -> Any:
    """Anthropic's API, or None without a key (tests put a stand-in here)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    import anthropic

    return anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=anthropic.Timeout(TITLE_LIMIT_S, connect=2.0))


def what_was_said(folder: Path) -> tuple[str, bool]:
    """The call's speech as "Person: ..." and "AI: ..." lines, and whether the person spoke."""
    from zoneinfo import ZoneInfo

    call = read_call(read_record(folder), ZoneInfo("UTC"))
    items, _ = items_of("call", call, "ended", ZoneInfo("UTC"))
    lines = [f"{'Person' if i['speaker'] == 'person' else 'AI'}: {i['text']}" for i in items if i["type"] == "speech"]
    said = "\n".join(lines)
    if len(said) > SAID_CHARS:
        said = said[:SAID_CHARS // 4] + "\n(...)\n" + said[-(SAID_CHARS * 3 // 4):]
    return said, any(i["type"] == "speech" and i["speaker"] == "person" for i in items)


def clean_title(text: str) -> str:
    """The answer's first line without quotation marks or a closing full stop, at most TITLE_CHARS."""
    line = next((l for l in text.strip().splitlines() if l.strip()), "").strip().strip(QUOTES).strip()
    line = line.rstrip(".。").strip()
    return line[:TITLE_CHARS].rstrip() if len(line) > TITLE_CHARS else line


async def title_call(folder: Path, language: CallLanguage, *, workspace: Path, client: Any = "default") -> str | None:
    """Writes the call's title (call_titled) and returns it; None, with nothing written, when the person never spoke
    or anything failed. `client`: Anthropic's API by default, None for none."""
    said, person_spoke = what_was_said(folder)
    if not person_spoke:
        return None
    client = anthropic_client() if client == "default" else client
    if client is None:
        return None
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(client.messages.create(
            model=TITLE_MODEL, max_tokens=80,
            system=SYSTEM.format(language=language.name, chars=TITLE_CHARS),
            messages=[{"role": "user", "content": said}]), TITLE_LIMIT_S)
    except Exception as exc:  # the limit, a network error, an overloaded model: no title this time
        log.warning("no title for %s: %s", folder, f"{type(exc).__name__}: {exc}"[:200])
        return None
    usage = getattr(answer, "usage", None)
    tokens = {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
              "output_tokens": int(getattr(usage, "output_tokens", 0) or 0)}
    SpendLog(workspace).add("call_title", model=TITLE_MODEL, call=session_id(folder), **tokens)
    if getattr(answer, "stop_reason", None) == "refusal":
        return None
    text = "".join(getattr(block, "text", "") for block in getattr(answer, "content", []) or []
                   if getattr(block, "type", None) == "text")
    title = clean_title(text)
    if not title:
        return None
    append_event(folder, "call_titled", title=title, model=TITLE_MODEL, seconds=round(time.monotonic() - started, 2),
                 **tokens)
    return title


def append_event(folder: Path, event_type: str, /, **data: Any) -> None:
    """An after-call line at the record's last time (call_view leaves call_titled out of how long the call was)."""
    record = read_record(folder)
    last = max((e.get("t") for _, e in record.lines if isinstance(e.get("t"), (int, float))), default=0.0)
    with (folder / EVENTS).open("a") as f:
        f.write(json.dumps({"t": last, **data, "type": event_type}, ensure_ascii=False) + "\n")


def needs_title(folder: Path) -> bool:
    """An ended call without a title. A call without an end (a stopped voice server) is left alone: writing to its
    record would make it look alive."""
    kinds = {e.get("type") for _, e in read_record(folder).lines}
    return "call_ended" in kinds and "call_titled" not in kinds


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m voice.titles", description="Titles a person's ended calls that "
                                     "have no title yet (one Claude Haiku 5.5 request each).")
    parser.add_argument("workspace", type=Path, help="the person's folder, data/people/<id>")
    args = parser.parse_args(argv)
    client = anthropic_client()
    if client is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    from voice.people import Settings

    language = Settings.from_file(args.workspace / "settings.json").language
    for folder in call_folders(args.workspace):
        if not needs_title(folder):
            continue
        record = read_record(folder)
        spoken = next((e.get("language") for _, e in record.lines if e.get("type") == "coordinator"
                       and isinstance(e.get("language"), str)), language)
        title = asyncio.run(title_call(folder, call_language(spoken), workspace=args.workspace, client=client))
        print(f"{session_id(folder)}: {title or '(no title)'}")


if __name__ == "__main__":
    main()
