"""A call's one-line title, written after the call (contract section 1, decision 7).

The voice server runs it in a call's shutdown, beside the Bee retry, once the record is closed: one request to the
model catalog's title role (voice/model_catalog.py: Claude Haiku 5.5, no thinking) over what was said, in the call's
language, only when the person spoke, with a hard limit of TITLE_LIMIT_S. The title is recorded as an after-call
call_titled event in the call's events.jsonl, and the request's tokens go to the spend log (voice/costs.py). On any
failure (the provider not set up, the limit, an error, a refusal, an empty answer) nothing is written, and the call
view's title falls back to the call's topic or the person's first sentence (voice/call_view.py).

Older calls: uv run python -m voice.titles data/people/<id>   titles each ended call of that person without one
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from voice import model_catalog, providers
from voice.call_folders import EVENTS, call_folders, session_id
from voice.call_models import coordinator_llm
from voice.call_view import items_of, read_call, read_record
from voice.costs import SpendLog
from voice.language import CallLanguage, call_language
from voice.providers import Turn, Usage

log = logging.getLogger("bee-call.titles")

TITLE_LIMIT_S = 5.0  # the whole request, the answer included (decision 7)
TITLE_CHARS = 60  # as long as a title made from the person's first sentence
SAID_CHARS = 8000  # what was said, as the model reads it: a long call keeps its start and its end
SYSTEM = ("You write the title of one phone call between a person and Bee Call, their voice assistant, for the "
          "person's list of calls. Write one line in {language}, at most {chars} characters: what the call was "
          "about, in a few words, as the person would name it, such as a question they asked or what they sorted "
          "out. No quotation marks, no full stop, no speaker names, and never words like \"call\" or \"conversation "
          "about\". Answer with the title only.")
QUOTES = "\"'“”‘’「」『』"


def title_choice() -> model_catalog.Choice:
    """The title role's model as this COORDINATOR_LLM mode runs it."""
    return model_catalog.mode_role("title", coordinator_llm()).choices[0]


def ready(choice: model_catalog.Choice) -> bool:
    """Whether the choice's provider is set up here (Anthropic's needs its key); one that is not is never asked."""
    found = providers.adapter(choice.offering.provider)
    return bool(getattr(found, "ready", lambda: True)())


def what_was_said(folder: Path) -> tuple[str, bool]:
    """The call's speech as "Person: ..." and "AI: ..." lines, and whether the person spoke."""
    from zoneinfo import ZoneInfo

    call = read_call(read_record(folder), ZoneInfo("UTC"))
    items, _ = items_of("call", call, "ended", ZoneInfo("UTC"), time.time())
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


async def title_call(folder: Path, language: CallLanguage, *, workspace: Path) -> str | None:
    """Writes the call's title (call_titled) and returns it; None, with nothing written, when the person never spoke
    or anything failed."""
    said, person_spoke = what_was_said(folder)
    choice = title_choice()
    if not person_spoke or not ready(choice):
        return None
    started, usage = time.monotonic(), Usage()
    try:
        answer = await asyncio.wait_for(providers.reply(
            choice, SYSTEM.format(language=language.name, chars=TITLE_CHARS), [Turn("user", text=said)],
            usage=usage), TITLE_LIMIT_S)
    except Exception as exc:  # the limit, a network error, an overloaded model: no title this time
        log.warning("no title for %s: %s", folder, f"{type(exc).__name__}: {exc}"[:200])
        answer = None
    finally:
        if usage.input_tokens or usage.output_tokens:
            SpendLog(workspace).add("call_title", model=choice.offering.model, input_tokens=usage.input_tokens,
                                    output_tokens=usage.output_tokens, estimated=usage.estimated,
                                    call=session_id(folder))
    if answer is None or answer.stop == "refusal":
        return None
    title = clean_title(answer.turn.text)
    if not title:
        return None
    append_event(folder, "call_titled", title=title, model=answer.model, seconds=round(time.monotonic() - started, 2),
                 input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
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
    if not ready(title_choice()):
        raise SystemExit(f"{title_choice().offering.provider} is not set up here (for Anthropic: ANTHROPIC_API_KEY)")
    from voice.people import Settings

    language = Settings.from_file(args.workspace / "settings.json").language
    for folder in call_folders(args.workspace):
        if not needs_title(folder):
            continue
        record = read_record(folder)
        spoken = next((e.get("language") for _, e in record.lines if e.get("type") == "coordinator"
                       and isinstance(e.get("language"), str)), language)
        title = asyncio.run(title_call(folder, call_language(spoken), workspace=args.workspace))
        print(f"{session_id(folder)}: {title or '(no title)'}")


if __name__ == "__main__":
    main()
