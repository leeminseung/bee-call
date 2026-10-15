"""Calls the voice server with a synthetic caller, no human needed.

Polly speaks a few English lines in a different voice from the AI, ffmpeg
stitches them into one Opus file with pauses, and `lk room join --publish`
plays it into a LiveKit room. The voice server (running in dev mode) is
dispatched into that room, answers, and records the call. Afterwards this
prints the transcript and the timing metrics from the newest session folder.

By default the fake caller starts the call, the way the app does. With
--outbound INBOX the AI starts it instead: the planner decides on that saved
Bee change, voice/dispatch.py opens a test call (no phone rings, no offer is
used), and the fake caller answers.

Run (voice server already running: uv run python -m voice.agent dev):
    uv run python -m voice.fake_caller
    uv run python -m voice.fake_caller --line "Hello there" --line "How are you?"
    uv run python -m voice.fake_caller --outbound workspace/inbox/2026-10-12-153000
"""

import argparse
import asyncio
import os
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from beelink.planner import RulePlanner
from voice.call_folders import EVENTS, read_events
from voice.aws_session import aws_session
from voice.costs import SpendLog
from voice.dispatch import open_inbound_room, open_test_call
from voice.outbound import saved_trigger
from voice.people import People, dev_person
from beelink import cli as bee
from voice.prep import PREP_SECONDS, prepare_outbound

load_dotenv(".env.local")
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

DEFAULT_LINES = {
    "en": ["Hi, can you hear me okay?",
           "I got a job offer today and I'm not sure what to do.",
           "The new one pays more, but my team is great."],
    "ko": ["안녕하세요, 제 목소리 잘 들리세요?",
           "오늘 이직 제안을 받았는데 어떻게 할지 모르겠어요.",
           "새 회사가 연봉은 더 높은데, 지금 팀이 정말 좋거든요."],
}
# The caller's Polly voice and engine by language; it must not sound like the AI (voice/language.py).
CALLER_VOICES = {"en": ("Matthew", "generative"), "ko": ("Jihye", "neural")}


def synthesize(polly, text: str, path: Path, voice: str, engine: str = "generative") -> None:
    resp = polly.synthesize_speech(Engine=engine, VoiceId=voice, OutputFormat="mp3", Text=text)
    path.write_bytes(resp["AudioStream"].read())


def build_track(lines: list[str], voice: str, lead: float, gaps: list[float], tail: float, out: Path,
                engine: str = "generative") -> None:
    polly = aws_session().client("polly", region_name=REGION)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, text in enumerate(lines):
            mp3 = Path(tmp) / f"line{i}.mp3"
            synthesize(polly, text, mp3, voice, engine)
            parts.append(mp3)
        inputs, filters = [], []
        for i, part in enumerate(parts):
            inputs += ["-i", str(part)]
            pause = lead if i == 0 else gaps[min(i - 1, len(gaps) - 1)]
            filters.append(f"[{i}:a]aresample=48000,adelay={int(pause * 1000)}:all=1[a{i}]")
        joined = "".join(f"[a{i}]" for i in range(len(parts)))
        filters.append(f"{joined}concat=n={len(parts)}:v=0:a=1,apad=pad_dur={tail}[out]")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
             "-filter_complex", ";".join(filters), "-map", "[out]",
             "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k", str(out)],
            check=True,
        )


def newest_session(workspace: Path, after: float) -> Path | None:
    sessions = [p for p in workspace.glob("*/*") if p.is_dir() and p.stat().st_mtime >= after]
    return max(sessions, key=lambda p: p.stat().st_mtime) if sessions else None


def summarize(session: Path) -> None:
    print(f"\nsession folder: {session}")
    print((session / "transcript.md").read_text())
    for ev in read_events(session / EVENTS):
        if ev.get("type") != "metrics":
            continue
        data = ev["data"]
        keep = {k: data[k] for k in ("end_of_utterance_delay", "transcription_delay", "ttft", "ttfb", "duration")
                if k in data}
        if keep:
            print(f"  t={ev['t']:>6}s {ev['kind']}: {keep}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--line", action="append", help="caller line (repeatable)")
    parser.add_argument("--language", choices=sorted(DEFAULT_LINES), default="en",
                        help="the caller's language; start the voice server with the same CALL_LANGUAGE")
    parser.add_argument("--voice", help="Polly voice for the caller (default by language)")
    parser.add_argument("--lead", type=float, help="silence before the first line, seconds (default 7, plus the "
                        "longest ring before the AI picks up when the caller starts the call)")
    parser.add_argument("--gap", default="7", help="silence before each line after the first, seconds; a comma "
                        "list sets it line by line and its last value repeats (3,0.6,10: barge in, pause mid-thought, wait)")
    parser.add_argument("--tail", type=float, default=10.0, help="silence after the last line, seconds")
    parser.add_argument("--outbound", type=Path, metavar="INBOX",
                        help="let the AI call: plan and place a call for this saved Bee change, then answer it")
    parser.add_argument("--ring", type=float, default=3.0, help="seconds to let it ring before answering (--outbound)")
    args = parser.parse_args()
    person = dev_person()  # the call is recorded in this person's folder, like a call from their phone
    if not person:
        raise SystemExit("set BEE_CALL_DEV_PERSON to the person whose folder the test call goes into")
    people = People()
    workspace = people.workspace(person)
    bee.use_bee(people.bee_dir(person) if people.settings(person).bee else None)  # the call prep reads their Bee

    lines = args.line or DEFAULT_LINES[args.language]
    voice, engine = (args.voice, "generative") if args.voice else CALLER_VOICES[args.language]
    lead = args.lead if args.lead is not None else 7.0 + (0 if args.outbound else PREP_SECONDS)
    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        track = Path(tmp) / "caller.ogg"
        build_track(lines, voice, lead, [float(g) for g in args.gap.split(",")], args.tail, track, engine)
        if args.outbound:
            # A test: ignore the gap between calls and the age of the saved conversation.
            plan = RulePlanner(workspace, cooldown=timedelta(0), max_age=timedelta(days=3650),
                               now=people.person(person).now).decide(saved_trigger(args.outbound))
            print(f"plan: {'CALL' if plan.call else 'no call'} - {plan.reason}")
            if not plan.call:
                return
            prep = asyncio.run(prepare_outbound(plan, spend=SpendLog(workspace), bee=people.settings(person).bee,
                                                now=people.person(person).now()))
            print(f"prepared in {prep.seconds} s: " + (f"opening {prep.opening!r}" if prep.note else f"no note ({prep.error})"))
            room = asyncio.run(open_test_call(plan, people.person(person), prep=prep.to_meta()))
            print(f"the AI is calling in room {room}; answering in {args.ring:.0f}s")
            time.sleep(args.ring)
        else:
            room = f"fake-{time.strftime('%Y%m%d-%H%M%S')}"
            asyncio.run(open_inbound_room(room))
        print(f"joining room {room} as fake-caller with {len(lines)} lines")
        subprocess.run(
            ["lk", "room", "join", "--identity", "fake-caller", "--publish", str(track),
             "--auto-subscribe", "--exit-after-publish", room],
            check=True, stdout=subprocess.DEVNULL,
        )
    time.sleep(3)  # let the voice server flush the last turn
    session = newest_session(workspace, started)
    if session is None:
        print("no session folder was written; is the voice server running in dev mode?")
        return
    summarize(session)


if __name__ == "__main__":
    main()
