"""Draws a simple line or bar chart from data the worker sends: an SVG, and two PNGs of the same drawing, one for
light mode and one for dark mode.

The worker's model cannot practically type out image bytes, so it sends the numbers (save_chart) and this machine
draws the picture as SVG. The PNGs are drawn from that SVG (svg_to_png), so they always show the same; the app shows
a PNG, since iOS cannot show SVG without a web view (contract section 2.4, decision 4), and the session screen shows
either. The dark PNG (price-dark.png beside price.png, the chart's image_url_dark) is the same drawing with each
colour swapped for its dark one (DARK) and no background, so the app's own dark plate shows through; it is drawn from
the SVG alone, so a chart drawn before dark PNGs existed gets one too. The chart's title is kept in the SVG's <title>,
where the call view reads it (svg_title); a chart drawn before titles were kept (before 2026-10-15) has it only as the
bold line at its top, which is read instead. Text is drawn with Noto Sans KR (worker/fonts/, SIL Open Font License
1.1), which has Korean and Latin letters.

Charts drawn before PNGs or dark PNGs existed: uv run python -m worker.chart data/people/<id>   draws each missing PNG
"""

import argparse
import functools
import io
import math
import re
import xml.etree.ElementTree as ET
from html import escape, unescape
from pathlib import Path
from typing import Any

WIDTH, HEIGHT = 640, 360
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 40, 56
LEGEND = 140  # the legend's own column, right of the plot, so no bar or line hides it and it hides none
# Colours, by what they draw: the light ones in the SVG and its PNG, the dark ones in the dark PNG. They are the app's
# chart tokens from the dark mode design of 2026-10-15 (chart1-6, chartText, chartGrid), whose light series colours
# are the ones charts were already drawn in.
BACKGROUND = "#ffffff"  # left out of the dark PNG, which the app lays on its own plate
TEXT = "#555555"  # every text: the title, axis names, the axes' values, the legend
GRID = "#e5e5e5"
COLORS = ["#2f6fdf", "#e0702a", "#2a9d6f", "#a347c2", "#c43d4b", "#6b7a8f"]  # the series, in this order
DARK_TEXT, DARK_GRID = "#ABABB0", "#48484A"
# Yellow, grey, orange, purple, green, red: no blue, which is the person's own colour in the app.
DARK_COLORS = ["#FFD900", "#AEAEB2", "#FF9F0A", "#BF5AF2", "#30D158", "#FF6961"]
EARLIER_TEXT = ("#222", "#333", "#555")  # before 2026-10-16: a chart's title, its axis names and legend, the rest
# Each colour a chart was ever drawn in, and what the dark PNG has in its place (None: nothing).
DARK = {light.lower(): dark for light, dark in [(BACKGROUND, None), (TEXT, DARK_TEXT), (GRID, DARK_GRID),
                                                 *[(text, DARK_TEXT) for text in EARLIER_TEXT],
                                                 *zip(COLORS, DARK_COLORS)]}
DARK_PNG = "-dark.png"  # the end of a chart's dark PNG's name, in place of the SVG's .svg
FONT = Path(__file__).with_name("fonts") / "NotoSansKR-Regular.otf"
PNG_SCALE = 2  # the PNG has twice the SVG's size, sharp on a phone's screen
SVG = "{http://www.w3.org/2000/svg}"
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
# How a chart drawn before titles were kept begins: the renderer's white background, then the title in bold.
DRAWN_TITLE = re.compile(r'\s*<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360"[^>]*>\s*'
                         r'<rect width="640" height="360" fill="#ffffff"/>\s*'
                         r'<text [^>]*font-weight="bold"[^>]*>(.*?)</text>', re.S)
ANCHORS = {"start": "ls", "middle": "ms", "end": "rs"}  # SVG's text-anchor, on the baseline, as Pillow names it
ROTATED = re.compile(r"rotate\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\)")
SPACES = re.compile(r"[ \t\r\n]+")  # XML's white space, which SVG text shows as one space


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 100 or value == int(value) else f"{value:.2g}"


def render_svg(title: str, kind: str, series: list[dict[str, Any]], x_label: str = "", y_label: str = "") -> str:
    """series: [{"label": str, "points": [[x, y], ...]}]; x may be numbers or category names."""
    clean = []
    for s in series[:6]:
        points = [(p[0], _num(p[1])) for p in s.get("points", []) if isinstance(p, (list, tuple)) and len(p) == 2]
        points = [(x, y) for x, y in points if y is not None]
        if points:
            clean.append((str(s.get("label", "")), points[:200]))
    if not clean:
        raise ValueError("no data points to draw")
    xs = [x for _, pts in clean for x, _ in pts]
    numeric_x = all(_num(x) is not None for x in xs) and kind == "line"
    categories = [] if numeric_x else list(dict.fromkeys(str(x) for x in xs))
    ys = [y for _, pts in clean for _, y in pts]
    y_min, y_max = min(0.0, min(ys)), max(ys)
    if y_max == y_min:
        y_max = y_min + 1
    pw, ph = WIDTH - LEFT - RIGHT - LEGEND, HEIGHT - TOP - BOTTOM

    def py(y: float) -> float:
        return TOP + ph - (y - y_min) / (y_max - y_min) * ph

    if numeric_x:
        x_vals = [_num(x) for x in xs]
        x_lo, x_hi = min(x_vals), max(x_vals)
        x_hi = x_hi if x_hi != x_lo else x_lo + 1

        def px(x: Any) -> float:
            return LEFT + (_num(x) - x_lo) / (x_hi - x_lo) * pw
    else:
        step = pw / max(len(categories), 1)

        def px(x: Any) -> float:
            return LEFT + step * (categories.index(str(x)) + 0.5)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" font-family="sans-serif" font-size="12">',
           f'<title>{escape(title)}</title>',
           f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
           f'<text x="{LEFT}" y="22" font-size="15" font-weight="bold" fill="{TEXT}">{escape(title)}</text>']
    for i in range(6):
        value = y_min + (y_max - y_min) * i / 5
        y = py(value)
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{LEFT + pw}" y2="{y:.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end" fill="{TEXT}">{escape(_fmt(value))}</text>')
    labels = categories if not numeric_x else sorted(set(_num(x) for x in xs))
    every = max(1, len(labels) // 10)
    for i, label in enumerate(labels):
        if i % every:
            continue
        x = px(label)
        text = label if isinstance(label, str) else _fmt(label)
        out.append(f'<text x="{x:.1f}" y="{TOP + ph + 18}" text-anchor="middle" fill="{TEXT}">{escape(str(text)[:14])}</text>')
    if x_label:
        out.append(f'<text x="{LEFT + pw / 2:.1f}" y="{HEIGHT - 12}" text-anchor="middle" fill="{TEXT}">{escape(x_label)}</text>')
    if y_label:
        out.append(f'<text x="14" y="{TOP + ph / 2:.1f}" text-anchor="middle" fill="{TEXT}" '
                   f'transform="rotate(-90 14 {TOP + ph / 2:.1f})">{escape(y_label)}</text>')
    for n, (label, points) in enumerate(clean):
        color = COLORS[n % len(COLORS)]
        if kind == "bar":
            width = pw / max(len(categories), 1) * 0.8 / len(clean)
            for x, y in points:
                left = px(x) - width * len(clean) / 2 + width * n
                top = py(max(y, 0.0))
                out.append(f'<rect x="{left:.1f}" y="{top:.1f}" width="{width:.1f}" height="{abs(py(y) - py(0.0)):.1f}" fill="{color}"/>')
        else:
            ordered = sorted(points, key=lambda p: _num(p[0])) if numeric_x else points
            path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in ordered)
            out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
    for n, (label, _) in enumerate(clean):
        color = COLORS[n % len(COLORS)]
        out.append(f'<rect x="{LEFT + pw + 14}" y="{TOP + 4 + n * 16}" width="10" height="10" fill="{color}"/>')
        out.append(f'<text x="{LEFT + pw + 28}" y="{TOP + 13 + n * 16}" fill="{TEXT}">{escape(label[:16])}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def svg_title(svg: str) -> str | None:
    """The chart's title, as render_svg keeps it or, for a chart drawn before titles were kept, as it drew it; None
    for an SVG without one."""
    found = TITLE.search(svg[:4096]) or DRAWN_TITLE.match(svg[:4096])
    return (unescape(found.group(1)).strip() or None) if found else None


@functools.lru_cache(maxsize=16)
def _font(size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONT), size)


def _f(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = _num(element.get(name))
    return default if value is None else value


def pictures(svg_name: str) -> tuple[str, str]:
    """A chart's two PNGs, light and dark, beside its SVG: price.svg has price.png and price-dark.png."""
    stem = svg_name.removesuffix(".svg")
    return f"{stem}.png", f"{stem}{DARK_PNG}"


def chart_of_dark(name: str) -> str | None:
    """The SVG whose dark PNG a file of this name is (price-dark.png: price.svg); None for any other name."""
    stem = name.removesuffix(DARK_PNG)
    return f"{stem}.svg" if stem and stem != name else None


def svg_to_png(svg: str, scale: int = PNG_SCALE, dark: bool = False) -> bytes:
    """The PNG of an SVG render_svg drew: its rectangles, lines, polylines and text, at `scale` times its size; with
    `dark`, each colour swapped for its dark one (DARK) and the background left out. ValueError for anything that is
    not such an SVG, and with `dark` for one in a colour this code never drew (its dark picture would be a guess)."""
    from PIL import Image, ImageDraw

    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"not an SVG: {exc}") from None
    view = (root.get("viewBox") or "").split()
    if root.tag != f"{SVG}svg" or len(view) != 4 or (root.find(f"{SVG}title") is None and not DRAWN_TITLE.match(svg)):
        raise ValueError("not a chart this code drew")
    width, height = float(view[2]), float(view[3])

    def ink(element: ET.Element, name: str) -> str | None:
        colour = element.get(name) or "#000"
        if not dark:
            return colour
        if colour.lower() not in DARK:
            raise ValueError(f"a colour this code never drew: {colour[:20]}")
        return DARK[colour.lower()]

    image = Image.new("RGBA", (round(width * scale), round(height * scale)), (0, 0, 0, 0) if dark else BACKGROUND)
    draw = ImageDraw.Draw(image)
    base = _f(root, "font-size", 12.0)
    for element in root:
        tag = element.tag.removeprefix(SVG)
        if tag == "rect" and (fill := ink(element, "fill")):  # no fill: the background, left out of the dark PNG
            x, y = _f(element, "x"), _f(element, "y")
            w, h = _f(element, "width", width), _f(element, "height", height)
            draw.rectangle([x * scale, y * scale, (x + w) * scale, (y + h) * scale], fill=fill)
        elif tag == "line" and (stroke := ink(element, "stroke")):
            draw.line([_f(element, "x1") * scale, _f(element, "y1") * scale, _f(element, "x2") * scale,
                       _f(element, "y2") * scale], fill=stroke,
                      width=max(1, round(_f(element, "stroke-width", 1.0) * scale)))
        elif tag == "polyline" and (stroke := ink(element, "stroke")):
            points = [tuple(float(v) * scale for v in pair.split(","))
                      for pair in (element.get("points") or "").split() if "," in pair]
            if len(points) > 1:
                draw.line(points, fill=stroke, width=max(1, round(_f(element, "stroke-width", 1.0) * scale)),
                          joint="curve")
        elif tag == "text" and (words := SPACES.sub(" ", element.text or "").strip()):
            if fill := ink(element, "fill"):
                _text(image, element, words, base, scale, fill)  # on one line, as a browser shows the SVG
    out = io.BytesIO()
    (image if dark else image.convert("RGB")).save(out, "PNG", optimize=True)
    return out.getvalue()


def _text(image, element: ET.Element, text: str, base: float, scale: int, fill: str) -> None:
    """One text, laid on the picture in its colour through its letters' own coverage, so their soft edges blend with
    what is under them, a transparent background too (drawn straight onto one, they would come out darker)."""
    from PIL import Image, ImageDraw

    font = _font(round(_f(element, "font-size", base) * scale))
    bold = 1 if element.get("font-weight") == "bold" else 0  # Noto Sans KR Regular, thickened by its own colour
    anchor = ANCHORS.get(element.get("text-anchor") or "start", "ls")
    x, y = _f(element, "x") * scale, _f(element, "y") * scale
    box = font.getbbox(text, anchor=anchor, stroke_width=bold)
    left, top = math.floor(box[0]) - 2, math.floor(box[1]) - 2
    mask = Image.new("L", (math.ceil(box[2]) + 3 - left, math.ceil(box[3]) + 3 - top), 0)
    dx, dy = x - math.floor(x), y - math.floor(y)  # the letters keep their place within a pixel
    ImageDraw.Draw(mask).text((dx - left, dy - top), text, font=font, fill=255, anchor=anchor, stroke_width=bold,
                              stroke_fill=255)
    turned = ROTATED.fullmatch((element.get("transform") or "").strip())
    if turned:  # a turned label (the y axis): turned on its own and placed around its own anchor point
        mask = mask.rotate(-float(turned.group(1)), expand=True)  # SVG turns clockwise, Pillow the other way
        place = (round(x - mask.width / 2), round(y - mask.height / 2))
    else:
        place = (math.floor(x) + left, math.floor(y) + top)
    layer = Image.new("RGBA", mask.size, fill)
    layer.putalpha(mask)
    image.alpha_composite(layer, dest=place)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m worker.chart", description="Draws each PNG, light or dark, "
                                     "that a chart a person's background jobs drew is missing: both for charts "
                                     "drawn before PNGs, the dark one for charts drawn before dark PNGs.")
    parser.add_argument("workspace", type=Path, help="the person's folder, data/people/<id>")
    args = parser.parse_args(argv)
    for svg in sorted(args.workspace.glob("*/*/jobs/*/*.svg")):
        for name, dark in zip(pictures(svg.name), (False, True)):
            png = svg.with_name(name)
            if png.exists():
                continue
            try:
                png.write_bytes(svg_to_png(svg.read_text(errors="replace"), dark=dark))
            except ValueError as exc:
                print(f"{png}: left alone ({exc})")
                continue
            print(f"{png}: drawn")


if __name__ == "__main__":
    main()
