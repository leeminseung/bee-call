"""Draws a simple line or bar chart from data the worker sends: an SVG, and a PNG of the same drawing.

The worker's model cannot practically type out image bytes, so it sends the numbers (save_chart) and this machine
draws the picture as SVG. The PNG is drawn from that SVG (svg_to_png), so the two always show the same; the app shows
the PNG, since iOS cannot show SVG without a web view (contract section 2.4, decision 4), and the session screen shows
either. The chart's title is kept in the SVG's <title>, where the call view reads it (svg_title). Text is drawn with
Noto Sans KR (worker/fonts/, SIL Open Font License 1.1), which has Korean and Latin letters.

Charts drawn before PNGs existed: uv run python -m worker.chart data/people/<id>   draws each missing PNG
"""

import argparse
import functools
import io
import re
import xml.etree.ElementTree as ET
from html import escape, unescape
from pathlib import Path
from typing import Any

WIDTH, HEIGHT = 640, 360
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 40, 56
LEGEND = 140  # the legend's own column, right of the plot, so no bar or line hides it and it hides none
COLORS = ["#2f6fdf", "#e0702a", "#2a9d6f", "#a347c2", "#c43d4b", "#6b7a8f"]
FONT = Path(__file__).with_name("fonts") / "NotoSansKR-Regular.otf"
PNG_SCALE = 2  # the PNG has twice the SVG's size, sharp on a phone's screen
SVG = "{http://www.w3.org/2000/svg}"
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
ANCHORS = {"start": "ls", "middle": "ms", "end": "rs"}  # SVG's text-anchor, on the baseline, as Pillow names it
ROTATED = re.compile(r"rotate\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\)")


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
           f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
           f'<text x="{LEFT}" y="22" font-size="15" font-weight="bold" fill="#222">{escape(title)}</text>']
    for i in range(6):
        value = y_min + (y_max - y_min) * i / 5
        y = py(value)
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{LEFT + pw}" y2="{y:.1f}" stroke="#e5e5e5"/>')
        out.append(f'<text x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#555">{escape(_fmt(value))}</text>')
    labels = categories if not numeric_x else sorted(set(_num(x) for x in xs))
    every = max(1, len(labels) // 10)
    for i, label in enumerate(labels):
        if i % every:
            continue
        x = px(label)
        text = label if isinstance(label, str) else _fmt(label)
        out.append(f'<text x="{x:.1f}" y="{TOP + ph + 18}" text-anchor="middle" fill="#555">{escape(str(text)[:14])}</text>')
    if x_label:
        out.append(f'<text x="{LEFT + pw / 2:.1f}" y="{HEIGHT - 12}" text-anchor="middle" fill="#333">{escape(x_label)}</text>')
    if y_label:
        out.append(f'<text x="14" y="{TOP + ph / 2:.1f}" text-anchor="middle" fill="#333" '
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
        out.append(f'<text x="{LEFT + pw + 28}" y="{TOP + 13 + n * 16}" fill="#333">{escape(label[:16])}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def svg_title(svg: str) -> str | None:
    """The chart's title, as render_svg keeps it; None for an SVG without one."""
    found = TITLE.search(svg[:4096])
    return (unescape(found.group(1)).strip() or None) if found else None


@functools.lru_cache(maxsize=16)
def _font(size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONT), size)


def _f(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = _num(element.get(name))
    return default if value is None else value


def svg_to_png(svg: str, scale: int = PNG_SCALE) -> bytes:
    """The PNG of an SVG render_svg drew: its rectangles, lines, polylines and text, at `scale` times its size.
    ValueError for anything that is not such an SVG."""
    from PIL import Image, ImageDraw

    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"not an SVG: {exc}") from None
    view = (root.get("viewBox") or "").split()
    if root.tag != f"{SVG}svg" or len(view) != 4 or root.find(f"{SVG}title") is None:
        raise ValueError("not a chart this code drew")
    width, height = float(view[2]), float(view[3])
    image = Image.new("RGB", (round(width * scale), round(height * scale)), "#ffffff")
    draw = ImageDraw.Draw(image)
    base = _f(root, "font-size", 12.0)
    for element in root:
        tag = element.tag.removeprefix(SVG)
        if tag == "rect":
            x, y = _f(element, "x"), _f(element, "y")
            w, h = _f(element, "width", width), _f(element, "height", height)
            draw.rectangle([x * scale, y * scale, (x + w) * scale, (y + h) * scale], fill=element.get("fill") or "#000")
        elif tag == "line":
            draw.line([_f(element, "x1") * scale, _f(element, "y1") * scale, _f(element, "x2") * scale,
                       _f(element, "y2") * scale], fill=element.get("stroke") or "#000",
                      width=max(1, round(_f(element, "stroke-width", 1.0) * scale)))
        elif tag == "polyline":
            points = [tuple(float(v) * scale for v in pair.split(","))
                      for pair in (element.get("points") or "").split() if "," in pair]
            if len(points) > 1:
                draw.line(points, fill=element.get("stroke") or "#000",
                          width=max(1, round(_f(element, "stroke-width", 1.0) * scale)), joint="curve")
        elif tag == "text" and element.text:
            _text(image, draw, element, element.text, base, scale)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def _text(image, draw, element: ET.Element, text: str, base: float, scale: int) -> None:
    from PIL import Image, ImageDraw

    size = round(_f(element, "font-size", base) * scale)
    font = _font(size)
    bold = element.get("font-weight") == "bold"
    fill = element.get("fill") or "#000"
    anchor = ANCHORS.get(element.get("text-anchor") or "start", "ls")
    x, y = _f(element, "x") * scale, _f(element, "y") * scale
    turned = ROTATED.fullmatch((element.get("transform") or "").strip())
    if not turned:
        draw.text((x, y), text, font=font, fill=fill, anchor=anchor, stroke_width=1 if bold else 0, stroke_fill=fill)
        return
    # A turned label (the y axis): drawn upright on its own, turned, and placed around its own anchor point.
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor=anchor)
    label = Image.new("RGBA", (right - left + 4, bottom - top + 4), (255, 255, 255, 0))
    ImageDraw.Draw(label).text((-left + 2, -top + 2), text, font=font, fill=fill, anchor=anchor)
    label = label.rotate(-float(turned.group(1)), expand=True)  # SVG turns clockwise, Pillow the other way
    image.paste(label, (round(x - label.width / 2), round(y - label.height / 2)), label)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m worker.chart", description="Draws the PNG of each chart a "
                                     "person's background jobs drew before PNGs were drawn with them.")
    parser.add_argument("workspace", type=Path, help="the person's folder, data/people/<id>")
    args = parser.parse_args(argv)
    for svg in sorted(args.workspace.glob("*/*/jobs/*/*.svg")):
        png = svg.with_suffix(".png")
        if png.exists():
            continue
        try:
            png.write_bytes(svg_to_png(svg.read_text(errors="replace")))
        except ValueError as exc:
            print(f"{svg}: left alone ({exc})")
            continue
        print(f"{png}: drawn")


if __name__ == "__main__":
    main()
