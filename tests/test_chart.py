"""Charts the worker draws (worker/chart.py): an SVG, and a PNG drawn from it for the app, which cannot show SVG
without a web view (contract 2.4, decision 4); the chart's title kept for the call view."""

import io
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageFont

from worker import chart
from worker.chart import FONT, render_svg, svg_to_png, svg_title

SERIES = [{"label": "MacBook Air", "points": [["8GB/256GB", 1590000], ["16GB/512GB", 2190000]]},
          {"label": "MacBook Pro", "points": [["8GB/256GB", 2390000], ["16GB/512GB", 2990000]]}]
TITLE = "한국 가격(원), 에어 & 프로"
# A chart as the worker drew it before charts had PNGs or kept their title (the renderer up to 35275c1).
BEFORE_PNGS = Path(__file__).parent / "fixtures" / "chart-before-pngs.svg"


def test_the_svg_keeps_the_chart_s_title():
    svg = render_svg(TITLE, "bar", SERIES, "구성", "원")
    assert svg.startswith("<svg") and "<title>한국 가격(원), 에어 &amp; 프로</title>" in svg
    assert svg_title(svg) == TITLE
    assert svg_title("<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>") is None


def test_a_chart_drawn_before_titles_were_kept_shows_its_drawn_title_and_gets_its_png():
    """Charts from before 2026-10-15 have no <title>: their title is the bold line the renderer drew at the top, and
    their PNG is drawn like any other. An SVG this code did not draw has no title even with bold text."""
    svg = BEFORE_PNGS.read_text()
    assert "<title>" not in svg
    assert svg_title(svg) == "Rain chance & wind (%)"
    image = Image.open(io.BytesIO(svg_to_png(svg)))
    assert image.size == (2 * chart.WIDTH, 2 * chart.HEIGHT)
    colors = {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 20)}
    assert (47, 111, 223) in colors
    foreign = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'>"
               "<text x='5' y='20' font-weight='bold'>Logo</text></svg>")
    assert svg_title(foreign) is None


def test_the_png_is_the_svg_s_drawing_at_twice_its_size():
    for kind, series in (("bar", SERIES), ("line", [{"label": "북한산", "points": [[6, 10], [12, 20], [16, 70]]}])):
        png = svg_to_png(render_svg(TITLE, kind, series, "시", "%"))
        image = Image.open(io.BytesIO(png))
        assert (image.format, image.size) == ("PNG", (2 * chart.WIDTH, 2 * chart.HEIGHT))
        colors = {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 20)}
        assert (47, 111, 223) in colors  # the first series' colour, #2f6fdf
        title_band = image.convert("L").crop((2 * chart.LEFT, 0, 2 * chart.WIDTH, 2 * chart.TOP))
        assert title_band.getextrema()[0] < 100  # the title is drawn


def test_korean_text_is_drawn_with_its_own_glyphs():
    """The bundled font (Noto Sans KR, SIL Open Font License) has Hangul: a syllable is not the missing-glyph box."""
    font = ImageFont.truetype(str(FONT), 24)
    assert bytes(font.getmask("한")) != bytes(font.getmask("\U0010FFFD"))
    assert (FONT.parent / "LICENSE-NotoSansKR.txt").read_text().startswith(
        "This Font Software is licensed under the SIL Open Font License")


def test_older_charts_get_their_png_from_the_one_off_command(tmp_path, capsys):
    """`python -m worker.chart data/people/<id>` draws the PNG of each chart the worker drew before PNGs existed;
    one already drawn, and an SVG this code did not make, are left alone."""
    jobs = tmp_path / "2026-10-14" / "1226" / "jobs" / "01"
    jobs.mkdir(parents=True)
    (jobs / "rain.svg").write_text(render_svg("비 확률", "line", [{"label": "서울", "points": [[8, 10], [12, 40]]}]))
    (jobs / "wind.svg").write_text(BEFORE_PNGS.read_text())
    (jobs / "done.svg").write_text(render_svg("done", "bar", SERIES))
    (jobs / "done.png").write_bytes(b"kept")
    (jobs / "foreign.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><circle r='4'/></svg>")
    (jobs / "broken.svg").write_text("<svg")
    chart.main([str(tmp_path)])
    assert Image.open(jobs / "rain.png").size == (2 * chart.WIDTH, 2 * chart.HEIGHT)
    assert Image.open(jobs / "wind.png").size == (2 * chart.WIDTH, 2 * chart.HEIGHT)
    assert (jobs / "done.png").read_bytes() == b"kept"
    assert not (jobs / "foreign.png").exists() and not (jobs / "broken.png").exists()
    assert "rain.png" in capsys.readouterr().out


def test_a_chart_the_worker_saves_shows_in_the_call_view_with_its_title_and_picture(tmp_path):
    """save_chart writes the SVG and its PNG; the call view lists the chart with its title and the PNG to show."""
    import asyncio

    from tests.test_harness import FakeHarness, assistant, fake_search
    from voice import call_view
    from worker.harness import HarnessWorker
    from worker.runner import JobRunner

    seoul = ZoneInfo("Asia/Seoul")
    folder = tmp_path / "2026-10-15" / "2000"
    folder.mkdir(parents=True)
    events = folder / "events.jsonl"
    events.write_text(json.dumps({"t": 0, "type": "call_started", "room": "call-matt-1",
                                  "started_at": "2026-10-15T20:00:00+09:00"}) + "\n")
    saved = json.dumps({"name": "prices", "title": TITLE, "kind": "bar", "series": SERIES})
    harness = FakeHarness([assistant(("tool", "c1", "save_chart", [saved])),
                           assistant(("text", "가격을 비교했어요."), stop="end_turn")])

    def emit(name, **fields):
        with events.open("a") as f:
            f.write(json.dumps({"t": 5.0, **fields, "type": name}, ensure_ascii=False) + "\n")

    async def main():
        runner = JobRunner(folder, emit, HarnessWorker("arn:harness", client=harness, search=fake_search),
                           lambda news: asyncio.sleep(0), clock=lambda: datetime.now(seoul))
        runner.start("Compare prices", title="가격 비교", language="ko")
        await asyncio.gather(*runner.tasks.values())

    asyncio.run(main())
    with events.open("a") as f:
        f.write(json.dumps({"t": 60, "type": "session_closed", "reason": "user_initiated"}) + "\n")
    assert Image.open(folder / "jobs" / "01" / "prices.png").format == "PNG"
    view = call_view.call_view(tmp_path, "call-matt-1", zone=seoul, language="ko",
                               now=datetime(2026, 10, 15, 21, 0, tzinfo=seoul))
    (job,) = [i for i in view["items"] if i["type"] == "job"]
    assert job["files"][0] == {"name": "prices.svg", "kind": "chart", "title": TITLE,
                               "url": "/calls/call-matt-1/files/jobs/01/prices.svg",
                               "image_url": "/calls/call-matt-1/files/jobs/01/prices.png"}
