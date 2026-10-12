"""Draws a simple line or bar chart as SVG, from data the worker sends.

The worker's model cannot practically type out image bytes, so it sends the numbers
(save_chart) and this Mac draws the picture. No dependencies; the session screen
shows the SVG like any other image.
"""

from html import escape
from typing import Any

WIDTH, HEIGHT = 640, 360
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 40, 56
COLORS = ["#2f6fdf", "#e0702a", "#2a9d6f", "#a347c2", "#c43d4b", "#6b7a8f"]


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
    pw, ph = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

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
           f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
           f'<text x="{LEFT}" y="22" font-size="15" font-weight="bold" fill="#222">{escape(title)}</text>']
    for i in range(6):
        value = y_min + (y_max - y_min) * i / 5
        y = py(value)
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" stroke="#e5e5e5"/>')
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
        out.append(f'<rect x="{WIDTH - RIGHT - 150}" y="{TOP + 4 + n * 16}" width="10" height="10" fill="{color}"/>')
        out.append(f'<text x="{WIDTH - RIGHT - 136}" y="{TOP + 13 + n * 16}" fill="#333">{escape(label[:20])}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
