"""Charts the worker draws (worker/chart.py): an SVG, and two PNGs drawn from it for the app, which cannot show SVG
without a web view (contract 2.4, decision 4), one for light mode and one for dark mode; the chart's title kept for
the call view."""

import io
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageChops, ImageFont

from worker import chart
from worker.chart import FONT, render_svg, svg_to_png, svg_title

SERIES = [{"label": "MacBook Air", "points": [["8GB/256GB", 1590000], ["16GB/512GB", 2190000]]},
          {"label": "MacBook Pro", "points": [["8GB/256GB", 2390000], ["16GB/512GB", 2990000]]}]
SIX = [{"label": f"구성 {n + 1}", "points": [["기본", 10 + n], ["최상", 20 + n]]} for n in range(6)]
TITLE = "한국 가격(원), 에어 & 프로"
# A chart as the worker drew it before charts had PNGs or kept their title (the renderer up to 35275c1).
BEFORE_PNGS = Path(__file__).parent / "fixtures" / "chart-before-pngs.svg"
# A chart of 2026-10-15, two series, drawn before charts had a dark PNG.
BEFORE_DARK = Path(__file__).parent / "fixtures" / "sessions" / "2026-10-15" / "0123" / "jobs" / "01" / "price-chart.svg"
# What a dark PNG sits on in the app (the dark mode design): the screen, a card, the chart plate, the plate in a sheet.
DARK_PLATES = ("#000000", "#1C1C1E", "#2C2C2E", "#3A3A3C")


def rgb(colour: str) -> tuple[int, int, int]:
    digits = colour.lstrip("#")
    digits = "".join(d * 2 for d in digits) if len(digits) == 3 else digits
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def contrast(a: str, b: str) -> float:
    """WCAG 2's contrast ratio of two colours."""
    def luminance(colour: str) -> float:
        r, g, b = (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in (c / 255 for c in rgb(colour)))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def opaque_colours(image: Image.Image) -> set[tuple[int, int, int]]:
    return {c[:3] for _, c in image.getcolors(maxcolors=1 << 21) if c[3] == 255}


def test_a_chart_s_two_pictures_are_named_by_one_rule():
    """price.svg's PNGs are price.png (light) and price-dark.png (dark), beside it (contract 2.4)."""
    assert chart.pictures("price-chart.svg") == ("price-chart.png", "price-chart-dark.png")
    assert chart.chart_of_dark("price-chart-dark.png") == "price-chart.svg"
    assert [chart.chart_of_dark(n) for n in ("price-chart.png", "price-chart-dark.svg", "-dark.png")] == [None] * 3


def test_the_dark_png_is_the_same_drawing_in_the_dark_palette_on_a_transparent_background():
    """The dark mode design (2026-10-15): no background, so the app's own plate shows through; text #ABABB0, grid
    #48484A, the series yellow, grey, orange, purple, green, red (no blue, the person's own colour); every mark where
    the light PNG has it."""
    svg = render_svg(TITLE, "bar", SIX, "구성", "원")
    light = Image.open(io.BytesIO(svg_to_png(svg)))
    dark = Image.open(io.BytesIO(svg_to_png(svg, dark=True)))
    assert (light.mode, dark.mode, dark.size) == ("RGB", "RGBA", light.size)
    assert dark.getpixel((0, 0)) == (0, 0, 0, 0)
    drawn = opaque_colours(dark)
    assert {rgb(c) for c in [*chart.DARK_COLORS, chart.DARK_TEXT, chart.DARK_GRID]} <= drawn
    assert not drawn & {rgb(c) for c in [*chart.COLORS, chart.TEXT, chart.GRID, chart.BACKGROUND]}
    # A letter's soft edge is the letter's colour, less opaque: no dark or white fringe, whatever plate is under it.
    assert {c[:3] for _, c in dark.getcolors(maxcolors=1 << 21) if 0 < c[3] < 255} == {rgb(chart.DARK_TEXT)}
    off_white = ImageChops.difference(light, Image.new("RGB", light.size, chart.BACKGROUND)).split()
    drawn_light = ImageChops.lighter(ImageChops.lighter(*off_white[:2]), off_white[2]).point(lambda v: 255 if v else 0)
    drawn_dark = dark.getchannel("A").point(lambda a: 255 if a else 0)
    assert drawn_dark.tobytes() == drawn_light.tobytes()


def test_a_line_chart_with_a_turned_axis_name_draws_dark_too():
    """A line, and the y axis's name turned on its side: the same letters, in the dark text colour."""
    svg = render_svg("토요일 강수 확률(%)", "line", [{"label": "북한산", "points": [[6, 10], [12, 20], [16, 70]]}],
                     "시", "강수 확률(%)")
    dark = Image.open(io.BytesIO(svg_to_png(svg, dark=True)))
    assert rgb(chart.DARK_COLORS[0]) in opaque_colours(dark)
    turned = dark.crop((0, 2 * chart.TOP, 2 * 24, 2 * (chart.HEIGHT - chart.BOTTOM)))
    assert turned.getchannel("A").getextrema()[1] == 255  # the turned name is drawn
    assert {c[:3] for _, c in turned.getcolors(maxcolors=1 << 21) if c[3]} == {rgb(chart.DARK_TEXT)}


def test_a_chart_drawn_before_dark_pngs_gets_its_dark_png_slot_for_slot():
    """Charts drawn before 2026-10-16 (blue first) get the dark colours by series: a price chart with two series and
    a rain chart from before PNGs with one."""
    for svg, series in ((BEFORE_DARK.read_text(), 2), (BEFORE_PNGS.read_text(), 1)):
        drawn = opaque_colours(Image.open(io.BytesIO(svg_to_png(svg, dark=True))))
        assert {rgb(c) for c in chart.DARK_COLORS[:series]} <= drawn
        assert not drawn & {rgb(c) for c in chart.DARK_COLORS[series:]}
        assert not drawn & {(47, 111, 223), (224, 112, 42), (255, 255, 255)}  # their light colours and background


def test_an_svg_in_colours_this_code_never_drew_gets_no_dark_png():
    """Its dark picture would have to guess: the light PNG is drawn as before, the dark one refused."""
    svg = render_svg(TITLE, "bar", SERIES).replace(chart.COLORS[0], "#123456")
    assert Image.open(io.BytesIO(svg_to_png(svg))).format == "PNG"
    with pytest.raises(ValueError):
        svg_to_png(svg, dark=True)


def test_the_palettes_are_the_design_s_and_readable_where_the_app_shows_them():
    """The token table of the dark mode design (ui-mockups/dark-mode.html: chart1-6, chartText, chartGrid; the light
    series are the colours charts were drawn in before it). Text keeps 4.5:1 and marks 3:1 on what they sit on: white
    for the light PNG, any dark plate the app lays the dark one on. Every colour a chart was ever drawn in has its
    dark colour, slot for slot."""
    assert chart.DARK_COLORS == ["#FFD900", "#AEAEB2", "#FF9F0A", "#BF5AF2", "#30D158", "#FF6961"]
    assert (chart.TEXT, chart.GRID, chart.DARK_TEXT, chart.DARK_GRID) == ("#555555", "#e5e5e5", "#ABABB0", "#48484A")
    assert contrast(chart.TEXT, chart.BACKGROUND) >= 4.5
    assert min(contrast(c, chart.BACKGROUND) for c in chart.COLORS) >= 3
    for plate in DARK_PLATES:
        assert contrast(chart.DARK_TEXT, plate) >= 4.5, plate
        assert min(contrast(c, plate) for c in chart.DARK_COLORS) >= 3, plate
    assert [chart.DARK[c.lower()] for c in chart.COLORS] == chart.DARK_COLORS
    assert {chart.DARK[c] for c in (chart.TEXT, *chart.EARLIER_TEXT)} == {chart.DARK_TEXT}


def test_every_text_of_the_light_chart_is_the_chart_text_colour():
    """chartText #555555 for the title too, as the design draws a chart's title, axes and legend alike; charts drawn
    before had their title in #222 and their axis names and legend in #333."""
    image = Image.open(io.BytesIO(svg_to_png(render_svg(TITLE, "bar", SERIES, "구성", "원"))))
    title_band = image.crop((2 * chart.LEFT, 0, 2 * chart.WIDTH, 2 * chart.TOP))
    greys = {c for _, c in title_band.getcolors(maxcolors=1 << 21)}  # the title's letters blended into white
    assert all(r == g == b for r, g, b in greys) and min(greys) == rgb(chart.TEXT) and max(greys) == (255, 255, 255)
    assert not {c for _, c in image.getcolors(maxcolors=1 << 21)} & {rgb("#222"), rgb("#333")}


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


def test_a_chart_a_job_saves_is_drawn_off_the_call_s_event_loop(tmp_path, monkeypatch):
    """A job runs in the call's process, beside the voice: drawing a PNG takes 40-170 ms (an M1; more on the
    server), against LiveKit's 200 ms of outgoing audio, so it is drawn in a worker thread (review, 2026-10-15)."""
    import asyncio
    import threading
    from datetime import datetime

    from tests.helpers import SEOUL
    from worker import tools
    from worker.runner import Job

    drawn_on = []

    def png(svg: str) -> bytes:
        drawn_on.append(threading.get_ident())
        return b"PNG"

    monkeypatch.setattr(tools, "svg_to_png", png)
    job = Job(job_id="01", dir=tmp_path, task="t", now=datetime.now(SEOUL))

    async def relay(words):
        pass

    async def main():
        loop_thread = threading.get_ident()
        said, ok = await tools.use_tool("save_chart", {"name": "rain", "title": "비", "kind": "line", "series": [
            {"label": "서울", "points": [[8, 10], [12, 40]]}]}, job, lambda line: None, relay)
        return loop_thread, ok

    loop_thread, ok = asyncio.run(main())
    assert ok and drawn_on and drawn_on[0] != loop_thread
    assert (tmp_path / "rain.png").read_bytes() == b"PNG" and (tmp_path / "rain.svg").exists()


def test_older_charts_get_their_pngs_from_the_one_off_command(tmp_path, capsys):
    """`python -m worker.chart data/people/<id>` draws each picture a chart the worker drew is missing: both for a
    chart from before PNGs, the dark one for a chart from before dark PNGs. A picture already there is left alone, an
    SVG this code did not make gets none, and one in colours this code never drew gets only its light PNG."""
    jobs = tmp_path / "2026-10-14" / "1226" / "jobs" / "01"
    jobs.mkdir(parents=True)
    (jobs / "rain.svg").write_text(render_svg("비 확률", "line", [{"label": "서울", "points": [[8, 10], [12, 40]]}]))
    (jobs / "wind.svg").write_text(BEFORE_PNGS.read_text())
    (jobs / "done.svg").write_text(render_svg("done", "bar", SERIES))
    (jobs / "done.png").write_bytes(b"kept")
    (jobs / "both.svg").write_text(render_svg("both", "bar", SERIES))
    (jobs / "both.png").write_bytes(b"kept")
    (jobs / "both-dark.png").write_bytes(b"kept too")
    (jobs / "odd.svg").write_text(render_svg("odd", "bar", SERIES).replace(chart.COLORS[0], "#123456"))
    (jobs / "foreign.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><circle r='4'/></svg>")
    (jobs / "broken.svg").write_text("<svg")
    chart.main([str(tmp_path)])
    for name in ("rain.png", "rain-dark.png", "wind.png", "wind-dark.png", "done-dark.png", "odd.png"):
        assert Image.open(jobs / name).size == (2 * chart.WIDTH, 2 * chart.HEIGHT), name
    assert [Image.open(jobs / n).mode for n in ("wind.png", "wind-dark.png")] == ["RGB", "RGBA"]
    assert [(jobs / n).read_bytes() for n in ("done.png", "both.png", "both-dark.png")] == [b"kept", b"kept",
                                                                                          b"kept too"]
    assert not [p.name for p in jobs.iterdir() if p.name.startswith(("odd-", "foreign", "broken")) and
                p.suffix == ".png"]
    out = capsys.readouterr().out
    assert "rain.png: drawn" in out and "done-dark.png: drawn" in out and "odd-dark.png: left alone" in out
    assert "both.png" not in out and "both-dark.png" not in out


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
