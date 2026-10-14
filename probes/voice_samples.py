"""Voice samples for picking the AI's voice: call lines with warmth and feeling in them, read by Amazon Polly and ElevenLabs voices
through the same LiveKit TTS the voice server uses (voice/voices.py), on one page to listen to side by side.

  uv run python -m probes.voice_samples --list      # the ElevenLabs voices in My Voices (needs Voices read)
  uv run python -m probes.voice_samples [--voices "Name,voice_id"] [--models m1,m2] [--no-polly] [--out DIR]

Polly: the voices the calls use now (Ruth, Seoyeon) and the other English generative voices in Sydney.
ElevenLabs: the voices named with --voices, or else every Voice Library voice in My Voices (never premade or
cloned ones: a clone may be someone's real voice). A voice reads the lines of its language (its language label
in the Voice Library), or both languages when it has none, with each model it lists as high quality.
Each sample records how long its first sound took, measured here over this Mac's connection, through the same
streaming path as a call (Polly one sentence at a time, ElevenLabs over its WebSocket).

Cost: ElevenLabs bills characters; the script prints the total before it starts (about 640 per English voice
and model, 330 per Korean one). Polly generative costs about 3 US cents per 1,000 characters.
Writes WAV files and index.html under workspace/probes/voices/<time>/ unless --out says otherwise;
--combine DIR... makes one page from earlier runs.
"""

import argparse
import asyncio
import html
import json
import os
import shutil
import statistics
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import tts as lk_tts

from voice.language import CallLanguage, call_language
from voice.voices import ELEVENLABS_MODEL, VoiceChoice, elevenlabs_key, make_tts

LINES = {  # warmth and feeling are what make a voice sound like a person, so the lines lean on them
    "en": [
        "Hey Minseung, it's Bee Call. Is now still a good time? I've got the laptop comparison you asked for.",
        "Oh no, that sounds really frustrating. You put so much into that presentation, and it's completely fair "
        "to feel let down. Do you want to talk it through, or should I just take a few things off your plate?",
        "Wait, you got the offer? That's amazing, congratulations! Honestly, you earned this. All that late-night "
        "prep finally paid off. Let's celebrate a little before we get into the details.",
        "Okay, I have to ask... is this the third time we're comparing MacBooks? I'm kidding. This time I've got "
        "the real numbers, so let's settle it for good.",
    ],
    "ko": [
        "아, 그러셨구나. 오늘 면접 생각보다 많이 긴장되셨죠? 그래도 끝까지 차분하게 대답하신 거, 저는 정말 잘하셨다고 "
        "생각해요. 결과가 어떻게 나오든, 준비하신 시간은 절대 헛되지 않아요.",
        "와, 정말요? 합격하셨어요? 진짜 축하드려요! 그동안 늦게까지 준비하시던 거 제가 다 알잖아요. 오늘은 맛있는 거 "
        "드시면서 푹 쉬세요.",
        "음, 목소리가 좀 지쳐 보이시는데 괜찮으세요? 오늘 일정이 꽤 빡빡했잖아요. 급한 건 제가 정리해 둘 테니까, 지금은 "
        "잠깐이라도 숨 좀 돌리셔도 돼요.",
        "하하, 또 노트북 고민이세요? 이번엔 진짜 결정하실 거죠? 농담이에요. 이번에는 제가 확실하게 비교해서, 고민 끝내 "
        "드릴게요.",
    ],
}
POLLY = {  # (voice, engine), the first of each language is what the calls use now
    "en": [("Ruth", "generative"), ("Tiffany", "generative"), ("Danielle", "generative"),
           ("Stephen", "generative"), ("Matthew", "generative")],
    "ko": [("Seoyeon", "neural")],
}
# Flash is what calls use; Turbo is a little slower and fuller; v4 is the most expressive (text-to-dialogue).
# A voice reads only the models it lists as its high-quality ones, when it lists any.
DEFAULT_MODELS = [ELEVENLABS_MODEL, "eleven_turbo_v2_5", "eleven_v4"]
API = "https://api.elevenlabs.io/v1"
LANGUAGE_LABELS = {"en": "en", "english": "en", "ko": "ko", "korean": "ko"}


@dataclass
class Row:
    label: str
    note: str
    language: str
    samples: list[dict] = field(default_factory=list)  # {"file", "first_ms"} or {"error"}


def my_voices(key: str) -> list[dict]:
    request = urllib.request.Request(f"{API}/voices", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["voices"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"ElevenLabs refused the voice list ({e.code}): {detail}\n"
                         "Give the API key the Voices read permission (Developers, API Keys).") from None


def voice_languages(voice: dict) -> list[str]:
    labels = voice.get("labels") or {}
    found = {LANGUAGE_LABELS.get(str(labels.get("language", "")).lower())}
    for verified in voice.get("verified_languages") or []:
        found.add(LANGUAGE_LABELS.get(str(verified.get("language", "")).lower()))
    found.discard(None)
    return sorted(found) or ["en", "ko"]


def models_for(voice: dict, models: list[str]) -> list[str]:
    good = voice.get("high_quality_base_model_ids") or []
    return [m for m in models if not good or m in good]


def describe(voice: dict) -> str:
    labels = voice.get("labels") or {}
    parts = [labels.get(k) for k in ("gender", "age", "accent", "descriptive", "use_case") if labels.get(k)]
    return ", ".join(parts)


async def speak(engine: lk_tts.TTS, text: str) -> tuple[rtc.AudioFrame, float]:
    """The audio for one line and the seconds to its first sound, on the path a call uses."""
    if not engine.capabilities.streaming:
        engine = lk_tts.StreamAdapter(tts=engine)  # what AgentSession does for Polly: sentence by sentence
    stream = engine.stream()
    stream.push_text(text)
    stream.end_input()
    start, first, frames = time.perf_counter(), None, []
    async for event in stream:
        if first is None:
            first = time.perf_counter() - start
        frames.append(event.frame)
    await stream.aclose()
    return rtc.combine_audio_frames(frames), first or 0.0


def save_wav(frame: rtc.AudioFrame, path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(frame.num_channels)
        out.setsampwidth(2)
        out.setframerate(frame.sample_rate)
        out.writeframes(frame.data.tobytes())


async def read_row(row: Row, engine: lk_tts.TTS, out: Path, stem: str) -> None:
    for i, text in enumerate(LINES[row.language], 1):
        try:
            frame, first = await speak(engine, text)
        except Exception as e:  # one voice or model failing should not stop the page
            row.samples.append({"error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        name = f"{stem}-{row.language}{i}.wav"
        save_wav(frame, out / name)
        row.samples.append({"file": name, "first_ms": round(first * 1000)})
    print(f"  {row.label} ({row.note}): " + ", ".join(
        f"{s['first_ms']} ms" if "file" in s else "error" for s in row.samples))


def page(rows: list[Row], made: str) -> str:
    def cell(sample: dict) -> str:
        if "error" in sample:
            return f'<td class="error">{html.escape(sample["error"])}</td>'
        return (f'<td><audio controls preload="none" src="{html.escape(sample["file"])}"></audio>'
                f'<span class="ms">첫 소리 {sample["first_ms"]} ms</span></td>')

    sections = []
    for language, title in (("en", "영어"), ("ko", "한국어")):
        mine = [r for r in rows if r.language == language]
        if not mine:
            continue
        head = "".join(f"<th>{i}. {html.escape(t)}</th>" for i, t in enumerate(LINES[language], 1))
        body = []
        for r in mine:
            firsts = [s["first_ms"] for s in r.samples if "first_ms" in s]
            median = f"{round(statistics.median(firsts))} ms" if firsts else "없음"
            body.append(f'<tr><th class="who">{html.escape(r.label)}<small>{html.escape(r.note)}</small>'
                        f'<small>첫 소리 중간값 {median}</small></th>'
                        + "".join(cell(s) for s in r.samples) + "</tr>")
        sections.append(f"<h2>{title}</h2><div class=scroll><table><thead><tr><th></th>{head}</tr></thead>"
                        f"<tbody>{''.join(body)}</tbody></table></div>")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>목소리 비교</title>
<style>
:root {{ color-scheme: light dark; --bg: #fff; --text: #16181d; --muted: #5b6270; --line: #d9dde4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #111317; --text: #eceef2; --muted: #a2a9b5; --line: #2c3038; }} }}
body {{ margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, "Apple SD Gothic Neo", system-ui, sans-serif; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} p {{ color: var(--muted); margin: 0 0 20px; max-width: 70ch; }}
h2 {{ font-size: 18px; margin: 28px 0 8px; }}
.scroll {{ overflow-x: auto; }} table {{ border-collapse: collapse; min-width: 900px; }}
th, td {{ border-top: 1px solid var(--line); padding: 10px 12px; vertical-align: top; text-align: left; }}
thead th {{ font-weight: 500; color: var(--muted); font-size: 13px; max-width: 280px; }}
th.who {{ min-width: 190px; }} th.who small {{ display: block; font-weight: 400; color: var(--muted); }}
audio {{ width: 230px; display: block; }} .ms {{ font-size: 12px; color: var(--muted); }}
td.error {{ color: #c0392b; font-size: 13px; max-width: 230px; }}
</style></head><body>
<h1>목소리 비교</h1>
<p>실제 통화에서 AI가 한 말을 같은 경로(통화 때 쓰는 LiveKit 음성 합성)로 읽힌 것입니다. 첫 소리는 이 Mac에서 잰 값입니다.
만든 때 {html.escape(made)}.</p>
{''.join(sections)}
</body></html>"""


def combine(runs: list[Path], out: Path) -> None:
    """One page from earlier runs, Polly rows first, so new voices can be added without paying again."""
    out.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    for run in runs:
        for data in json.loads((run / "samples.json").read_text()):
            row = Row(**data)
            for sample in row.samples:
                if "file" in sample and run.resolve() != out.resolve():
                    shutil.copyfile(run / sample["file"], out / sample["file"])
            rows = [r for r in rows if (r.label, r.note, r.language) != (row.label, row.note, row.language)]
            rows.append(row)
    order = [ELEVENLABS_MODEL, "eleven_turbo_v2_5", "eleven_v4_turbo", "eleven_v4", "eleven_v3_conversational"]
    model = lambda r: r.note.split(",")[0]  # noqa: E731
    rows.sort(key=lambda r: (not r.label.startswith("Polly"), r.label if not r.label.startswith("Polly") else "",
                             order.index(model(r)) if model(r) in order else len(order)))
    (out / "index.html").write_text(page(rows, datetime.now().strftime("%Y-%m-%d %H:%M")))
    (out / "samples.json").write_text(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=1))
    print(f"wrote {out / 'index.html'}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list the ElevenLabs voices in My Voices and stop")
    parser.add_argument("--voices", default="", help="comma-separated ElevenLabs voice ids, or names before ' - '")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated ElevenLabs models")
    parser.add_argument("--no-polly", action="store_true", help="skip the Polly voices")
    parser.add_argument("--all-polly", action="store_true",
                        help="every English generative Polly voice, not only the ones calls use now")
    parser.add_argument("--no-elevenlabs", action="store_true", help="only the Polly voices")
    parser.add_argument("--any-model", action="store_true",
                        help="read every model, also ones a voice does not list as high quality")
    parser.add_argument("--combine", type=Path, nargs="+", metavar="DIR",
                        help="make one page from earlier runs (their samples.json) and stop")
    parser.add_argument("--out", type=Path, help="where to write the samples and index.html")
    args = parser.parse_args()
    load_dotenv(".env.local")
    if args.combine:
        combine(args.combine, args.out or Path("workspace/probes/voices") / "combined")
        return
    key = elevenlabs_key()

    voices: list[dict] = []
    if args.list or not args.no_elevenlabs:
        if not key:
            raise SystemExit("ELEVENLABS_API_KEY is not set (python3 scripts/set_secret.py ELEVENLABS_API_KEY)")
        voices = my_voices(key)
    if args.list:
        for v in voices:
            print(f"{v['voice_id']}  {v['name']}  [{v.get('category', '')}]  languages {voice_languages(v)}  "
                  f"{describe(v)}")
        return

    picked: list[dict] = []
    if not args.no_elevenlabs:
        wanted = [w.strip() for w in args.voices.split(",") if w.strip()]
        if wanted:
            for w in wanted:
                match = [v for v in voices if w == v["voice_id"]
                         or v["name"].split(" - ")[0].strip().lower() == w.lower()]  # "Kanna - Calm & ..."
                if not match:
                    raise SystemExit(f"No voice named {w!r} in My Voices (see --list)")
                picked.append(match[0])
        else:
            picked = [v for v in voices if v.get("category") not in ("premade", "cloned")]
            if not picked:
                raise SystemExit("My Voices has no voice from the Voice Library yet; add some, or name premade "
                                 "ones with --voices")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    characters = sum(len(t) for v in picked for lang in voice_languages(v) for t in LINES[lang]
                     for _ in (models if args.any_model else models_for(v, models)))
    if picked:
        print(f"ElevenLabs: {len(picked)} voices x {len(models)} models, about {characters} characters")

    out = args.out or Path("workspace/probes/voices") / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    rows: list[Row] = []
    async with aiohttp.ClientSession() as http:
        if not args.no_polly:
            for language, choices in POLLY.items():
                lang = call_language(language)
                for i, (name, engine) in enumerate(choices if args.all_polly else choices[:1]):
                    row = Row(f"Polly {name}", f"{engine}" + (", 지금 통화 목소리" if i == 0 else ""), language)
                    await read_row(row, make_tts(VoiceChoice("polly", name, engine), lang, region), out,
                                   f"polly-{name.lower()}")
                    rows.append(row)
        for v in picked:
            for language in voice_languages(v):
                lang: CallLanguage = call_language(language)
                for model in (models if args.any_model else models_for(v, models)):
                    choice = VoiceChoice("elevenlabs", v["voice_id"], model)
                    note = model + (f", {describe(v)}" if describe(v) else "")
                    row = Row(f"ElevenLabs {v['name']}", note, language)
                    await read_row(row, make_tts(choice, lang, region, http_session=http), out,
                                   f"eleven-{v['voice_id'][:8]}-{model}")
                    rows.append(row)
    made = datetime.now().strftime("%Y-%m-%d %H:%M")
    (out / "index.html").write_text(page(rows, made))
    (out / "samples.json").write_text(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=1))
    print(f"wrote {out / 'index.html'}")


if __name__ == "__main__":
    asyncio.run(main())
