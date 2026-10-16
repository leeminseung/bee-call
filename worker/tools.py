"""The job's own tools, which every background worker gives its model and this Mac runs:

  progress(text)                       a line on the job card
  relay_to_user(text)                  one or two sentences for the voice to tell the user
  save_file(name, content)             a text result file in the job folder (report.md, a CSV table)
  save_chart(name, title, kind, ...)   a chart from numbers; this Mac draws it as SVG, and the same drawing as two
                                       PNGs for the app, light and dark (worker/chart.py)

The job folder stays on this Mac with one writer per file: a worker's model writes into it only through these tools
(and the Claude worker's code execution files, which this Mac saves under a free, safe name). Also here: the job's
first message (job_request) and its usage.json (write_usage), the same for every worker.
"""

import asyncio
import itertools
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from beelink.clock import now_text
from voice.language import LANGUAGES
from worker.chart import chart_of_dark, pictures, render_svg, svg_to_png
from worker.runner import USAGE, Job

log = logging.getLogger("bee-call.worker")

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
LONGEST_NAME = 81  # what SAFE_NAME allows
# A rejected name is never repeated back: Anthropic's web fetch may fetch a URL that appears in a tool result, so a
# "name" such as https://x/?d=<the task> would carry the task's text out (coordinator's review, 2026-10-15).
NOT_A_NAME = ("Not saved: that is not an allowed file name. Use letters, digits, dots, dashes and underscores, at "
              "most 81, with no folders, such as report.md or prices.csv.")
NO_SUCH_TOOL = "There is no tool by that name. Use one of: {tools}."
DARK_NAME = ("Not saved: a chart's name cannot end in -dark, which names each chart's picture for dark mode. Use "
             "another name, such as prices_by_size.")
RESERVED = {"request.md", "events.jsonl", "usage.json"}  # written by this Mac, never by a worker's model
MAX_FILE_BYTES = 5_000_000


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# Each tool's description and its input as a JSON Schema object.
JOB_TOOLS: dict[str, dict[str, Any]] = {
    "progress": {
        "description": "Show a short progress line on the job card the user sees.",
        "parameters": _schema({"text": {"type": "string", "description": "Under ten words."}}, ["text"]),
    },
    "relay_to_user": {
        "description": "Give the coordinator one or two short sentences to say to the user out loud.",
        "parameters": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "save_file": {
        "description": "Save a text result file (Markdown or CSV) into the job folder the user can open in the app.",
        "parameters": _schema({
            "name": {"type": "string", "description": "File name such as report.md or prices.csv; no folders."},
            "content": {"type": "string"},
        }, ["name", "content"]),
    },
    "save_chart": {
        "description": "Draw a line or bar chart from numbers and save it into the job folder as an image.",
        "parameters": _schema({
            "name": {"type": "string", "description": "File name without extension, such as rain_by_hour."},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["line", "bar"]},
            "x_label": {"type": "string"},
            "y_label": {"type": "string"},
            "series": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"},
                "points": {"type": "array", "description": "[[x, y], ...]; x is a number or a category name.",
                           "items": {"type": "array"}}}, "required": ["label", "points"]}},
        }, ["name", "title", "kind", "series"]),
    },
}


def allowed_name(name: str) -> bool:
    return bool(SAFE_NAME.fullmatch(name)) and name.casefold() not in RESERVED and not USAGE.fullmatch(name.casefold())


def free_name(folder: Path, name: str, fallback: str) -> str:
    """A safe name for a file a worker made by other means than save_file (the Claude worker's code execution):
    `name` without any folder, or `fallback` when that is not an allowed name, numbered when the job folder has a
    file of that name already."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not allowed_name(name):
        name = fallback
    if not (folder / name).exists():
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    for n in itertools.count(2):
        # The number goes before the kind (prices-2.csv), the stem cut to make room (a long name has no numbered
        # form otherwise); a kind too long to leave room goes with the rest of the name.
        tail = f"-{n}.{suffix}" if suffix else f"-{n}"
        found = stem[:LONGEST_NAME - len(tail)] + tail if len(tail) < LONGEST_NAME else \
            name[:LONGEST_NAME - len(f"-{n}")] + f"-{n}"
        if allowed_name(found) and not (folder / found).exists():
            return found
    raise AssertionError("unreachable")


def job_request(job: Job) -> str:
    """The job's first message to the worker's model: the task with today's date (the model has no clock, and the
    task may say "this Saturday"), and the language to write in for the user."""
    request = (f"Job {job.job_id} (it is now {now_text(job.now)}; read today, tomorrow and weekdays against this): "
               f"{job.task}")
    if job.language in LANGUAGES:  # the user reads the job card, the summary and the charts in the call's language
        name = LANGUAGES[job.language].name
        request += (f"\n\nThe user speaks {name}. Write in {name} every progress line, what you relay, chart "
                    "titles and labels, the report and your final summary.")
    return request


async def use_tool(name: str, args: dict[str, Any], job: Job, progress: Callable[[str], None],
                   relay: Callable[[str], Awaitable[None]]) -> tuple[str, bool]:
    """Runs one of JOB_TOOLS for a job. Returns (text for the model, whether it worked)."""
    if name == "progress":
        progress(str(args.get("text", ""))[:200])
        return "Shown on the job card.", True
    if name == "relay_to_user":
        await relay(str(args.get("text", "")))
        return "The coordinator will tell the user.", True
    if name == "save_file":
        file_name = str(args.get("name", ""))
        if not allowed_name(file_name):
            return NOT_A_NAME, False
        data = str(args.get("content", "")).encode()
        if len(data) > MAX_FILE_BYTES:
            return f"Not saved: {len(data)} bytes is over the {MAX_FILE_BYTES} byte limit.", False
        (job.dir / file_name).write_bytes(data)
        return f"Saved {file_name} ({len(data)} bytes).", True
    if name == "save_chart":
        file_name = f"{str(args.get('name', 'chart')).removesuffix('.svg')}.svg"
        if not allowed_name(file_name):
            return NOT_A_NAME, False
        light, dark = pictures(file_name)
        if chart_of_dark(light.casefold()):  # its PNG would be another chart's dark one (macOS ignores case)
            return DARK_NAME, False
        svg = render_svg(str(args.get("title", "")), str(args.get("kind", "line")), args.get("series") or [],
                         str(args.get("x_label", "")), str(args.get("y_label", "")))
        (job.dir / file_name).write_text(svg)
        for picture, in_dark in ((light, False), (dark, True)):
            try:  # the pictures the app shows (contract 2.4); without the light one the chart is a plain file
                # Drawn in a worker thread: a job runs in the call's process, and drawing takes 40-170 ms (on an
                # M1), near LiveKit's 200 ms of outgoing audio.
                png = await asyncio.to_thread(svg_to_png, svg, dark=in_dark)
                (job.dir / picture).write_bytes(png)
            except Exception:
                log.exception("could not draw %s", picture)
        return f"Saved {file_name}. Mention it in the report.", True
    raise KeyError(name)


def write_usage(job: Job, usage: dict[str, Any]) -> None:
    """The job's usage.json, replaced whole, so a reader never sees half of it."""
    tmp = job.dir / ".usage.json.tmp"
    tmp.write_text(json.dumps(usage))
    tmp.replace(job.dir / "usage.json")
