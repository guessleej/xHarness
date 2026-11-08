"""chart: draw a bar, horizontal-bar or line chart from numbers the model already
computed (with python_run), as an SVG written by this file alone -- no plotting
library. A .png path is converted with rsvg-convert, or cairosvg if importable.

The look follows the company palette and the charting rules the reports use:
one axis only, thin marks with 4 px rounded ends, a 2 px gap between bars,
recessive grid, text in ink colours (never the series colour), a legend for two
or more series plus selective direct labels, at most five series (a sixth is
folded into "其他" by the caller), and every text at 15 px or more. Static
images for documents, so no hover layer: the tool returns the data as a table
next to the file instead.

Palette order (validated for colour-blind separation on white): CIS red, cyan,
amber, purple, green. Fixed order; never cycled.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess  # nosec B404 - rsvg-convert with a fixed argv
from html import escape
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

PALETTE = ("#bf181f", "#0891b2", "#d97706", "#6b4fa3", "#2e7d32")
INK, INK_2, MUTED, GRID, SURFACE = "#16202a", "#3d4b56", "#6b7a85", "#e3e8ec", "#ffffff"
FONT = "'Microsoft JhengHei','PingFang TC','Noto Sans CJK TC','Noto Sans TC',sans-serif"
WIDTH, HEIGHT = 960, 540
MAX_SERIES = len(PALETTE)
MAX_POINTS = 60
KINDS = ("bar", "hbar", "line")


def text_width(text: str, size: float) -> float:
    return sum(size if ord(ch) > 0x2E80 else size * 0.58 for ch in text)


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round tick values (1, 2, 2.5, 5 x 10^n) covering low..high, zero included."""
    low, high = min(low, 0.0), max(high, 0.0)
    if high == low:
        high = low + 1
    raw = (high - low) / count
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    start = math.floor(low / step) * step
    ticks, value = [], start
    while value < high + step * 0.999:
        ticks.append(round(value, 10))
        value += step
    return ticks


def fmt(value: float) -> str:
    if abs(value) >= 1000 or float(value).is_integer():
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _text(x: float, y: float, s: str, size: int = 15, fill: str = INK_2, anchor: str = "start", weight: str = "400") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(s)}</text>')


def _bar_path(x: float, y0: float, w: float, y1: float, r: float = 4) -> str:
    """A bar from baseline y0 to data end y1, rounded only at the data end."""
    r = min(r, w / 2, abs(y1 - y0))
    if y1 <= y0:   # grows up
        return (f"M{x:.1f},{y0:.1f}V{y1 + r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}H{x + w - r:.1f}"
                f"Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 + r:.1f}V{y0:.1f}Z")
    return (f"M{x:.1f},{y0:.1f}V{y1 - r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 - r:.1f}V{y0:.1f}Z")


def _hbar_path(x0: float, y: float, h: float, x1: float, r: float = 4) -> str:
    r = min(r, h / 2, abs(x1 - x0))
    if x1 >= x0:
        return (f"M{x0:.1f},{y:.1f}H{x1 - r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}V{y + h - r:.1f}"
                f"Q{x1:.1f},{y + h:.1f} {x1 - r:.1f},{y + h:.1f}H{x0:.1f}Z")
    return (f"M{x0:.1f},{y:.1f}H{x1 + r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}V{y + h - r:.1f}"
            f"Q{x1:.1f},{y + h:.1f} {x1 + r:.1f},{y + h:.1f}H{x0:.1f}Z")


def render(kind: str, title: str, labels: list[str], series: list[dict[str, Any]], unit: str = "", note: str = "") -> str:
    parts: list[str] = []
    top = 40
    parts.append(_text(32, top, title, 24, INK, weight="700"))
    sub = " · ".join(s for s in (f"單位：{unit}" if unit else "", note) if s)
    if sub:
        top += 28
        parts.append(_text(32, top, sub, 15, INK_2))
    top += 18
    if len(series) > 1:                                  # legend row, text in ink
        x = 32
        for i, s in enumerate(series):
            parts.append(f'<rect x="{x}" y="{top + 2}" width="14" height="14" rx="3" fill="{PALETTE[i]}"/>')
            parts.append(_text(x + 20, top + 15, s["name"], 15, INK_2))
            x += 20 + text_width(s["name"], 15) + 24
        top += 44
    values = [v for s in series for v in s["values"]]
    ticks = nice_ticks(min(values), max(values))
    lo, hi = ticks[0], ticks[-1]

    if kind == "hbar":
        label_w = max(text_width(lb, 15) for lb in labels) + 16
        left, right, bottom = 32 + label_w, WIDTH - 90, HEIGHT - 48
        sx = lambda v: left + (v - lo) / (hi - lo) * (right - left)  # noqa: E731
        for t in ticks:
            parts.append(f'<line x1="{sx(t):.1f}" y1="{top}" x2="{sx(t):.1f}" y2="{bottom}" stroke="{GRID}" stroke-width="1"/>')
            parts.append(_text(sx(t), bottom + 24, fmt(t), 15, MUTED, "middle"))
        band = (bottom - top) / len(labels)
        bar_h = min(28.0, (band - 8) / len(series) - 2)
        for j, lb in enumerate(labels):
            y = top + j * band + (band - (bar_h + 2) * len(series) + 2) / 2
            parts.append(_text(left - 12, y + (bar_h + 2) * len(series) / 2 + 4, lb, 15, INK_2, "end"))
            for i, s in enumerate(series):
                v = s["values"][j]
                yy = y + i * (bar_h + 2)
                parts.append(f'<path d="{_hbar_path(sx(0), yy, bar_h, sx(v))}" fill="{PALETTE[i]}"/>')
                if len(series) == 1:
                    anchor_x = sx(v) + (8 if v >= 0 else -8)
                    parts.append(_text(anchor_x, yy + bar_h / 2 + 5, fmt(v), 15, INK, "start" if v >= 0 else "end"))
        parts.append(f'<line x1="{sx(0):.1f}" y1="{top}" x2="{sx(0):.1f}" y2="{bottom}" stroke="{MUTED}" stroke-width="1"/>')
    else:
        tick_w = max(text_width(fmt(t), 15) for t in ticks)
        left, right, bottom = 32 + tick_w + 12, WIDTH - (110 if kind == "line" and len(series) > 1 else 32), HEIGHT - 56
        sy = lambda v: bottom - (v - lo) / (hi - lo) * (bottom - top)  # noqa: E731
        for t in ticks:
            parts.append(f'<line x1="{left}" y1="{sy(t):.1f}" x2="{right}" y2="{sy(t):.1f}" stroke="{GRID}" stroke-width="1"/>')
            parts.append(_text(left - 10, sy(t) + 5, fmt(t), 15, MUTED, "end"))
        band = (right - left) / len(labels)
        every = max(1, math.ceil(max(text_width(lb, 15) for lb in labels) / (band - 6)))
        for j, lb in enumerate(labels):
            if j % every == 0:
                parts.append(_text(left + band * (j + 0.5), bottom + 26, lb, 15, INK_2, "middle"))
        if kind == "bar":
            group = band * 0.72
            bar_w = (group - 2 * (len(series) - 1)) / len(series)
            direct = len(labels) * len(series) <= 12
            for j in range(len(labels)):
                x0 = left + band * j + (band - group) / 2
                for i, s in enumerate(series):
                    v = s["values"][j]
                    x = x0 + i * (bar_w + 2)
                    parts.append(f'<path d="{_bar_path(x, sy(0), bar_w, sy(v))}" fill="{PALETTE[i]}"/>')
                    if direct:
                        ty = sy(v) - 8 if v >= 0 else sy(v) + 20
                        parts.append(_text(x + bar_w / 2, ty, fmt(v), 15, INK, "middle"))
        else:
            markers = len(labels) <= 24
            ends: list[list[Any]] = []
            for i, s in enumerate(series):
                pts = [(left + band * (j + 0.5), sy(v)) for j, v in enumerate(s["values"])]
                d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                parts.append(f'<path d="{d}" fill="none" stroke="{PALETTE[i]}" stroke-width="2" stroke-linejoin="round"/>')
                if markers:
                    parts += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{PALETTE[i]}" stroke="{SURFACE}" stroke-width="2"/>'
                              for x, y in pts]
                last_x, last_y = pts[-1]
                ends.append([last_y, last_x, fmt(s["values"][-1]) if len(series) == 1 else s["name"]])
            if len(series) <= 4:                         # end labels, pushed apart so none overlap
                ends.sort()
                for k in range(1, len(ends)):
                    ends[k][0] = max(ends[k][0], ends[k - 1][0] + 20)
                parts += [_text(x + 10, y + 5, tag, 15, INK) for y, x, tag in ends]
        parts.append(f'<line x1="{left}" y1="{sy(0):.1f}" x2="{right}" y2="{sy(0):.1f}" stroke="{MUTED}" stroke-width="1"/>')

    body = "\n".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
            f'font-family="{FONT}">\n<rect width="{WIDTH}" height="{HEIGHT}" fill="{SURFACE}"/>\n{body}\n</svg>\n')


def table(labels: list[str], series: list[dict[str, Any]]) -> str:
    head = "| 項目 | " + " | ".join(s["name"] for s in series) + " |"
    rows = [f"| {lb} | " + " | ".join(fmt(s["values"][j]) for s in series) + " |" for j, lb in enumerate(labels)]
    return "\n".join([head, "|---|" + "---|" * len(series)] + rows)


def to_png(svg_path: str, png_path: str) -> str | None:
    """None when done, else what is missing."""
    tool = shutil.which("rsvg-convert")
    if tool:
        done = subprocess.run([tool, "-z", "2", "-o", png_path, svg_path],  # nosec B603 - fixed argv
                              capture_output=True, timeout=60)
        return None if done.returncode == 0 else done.stderr.decode("utf-8", "replace")[:300]
    try:
        import cairosvg  # type: ignore[import-not-found]
    except ImportError:
        return "PNG needs rsvg-convert (librsvg) or the cairosvg package; save as .svg, or install one of them"
    cairosvg.svg2png(url=svg_path, write_to=png_path, scale=2)
    return None


def _validate(args: dict[str, Any]) -> tuple[str, str, list[str], list[dict[str, Any]]] | str:
    kind = str(args.get("kind") or "bar")
    if kind not in KINDS:
        return f"kind must be one of {', '.join(KINDS)} (no pie, no second y-axis)"
    labels = [str(x) for x in (args.get("labels") or [])]
    raw = args.get("series") or []
    if not labels or not isinstance(raw, list) or not raw:
        return "labels and series are required"
    if len(labels) > MAX_POINTS:
        return f"at most {MAX_POINTS} labels; aggregate first (by month, by group)"
    if len(raw) > MAX_SERIES:
        return (f"at most {MAX_SERIES} series: fold the smallest into one named 其他, "
                "or draw several charts")
    series = []
    for s in raw:
        if not isinstance(s, dict):
            return 'each series is {"name": ..., "values": [...]}'
        try:
            values = [float(v) for v in s.get("values") or []]
        except (TypeError, ValueError):
            return f"series {s.get('name')!r}: values must be numbers"
        if len(values) != len(labels) or not all(math.isfinite(v) for v in values):
            return f"series {s.get('name')!r}: needs {len(labels)} finite numbers, one per label"
        series.append({"name": str(s.get("name") or f"系列 {len(series) + 1}"), "values": values})
    title = str(args.get("title") or "").strip()
    if not title:
        return "title is required: say what the chart shows"
    return kind, title, labels, series


def _run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    checked = _validate(args)
    if isinstance(checked, str):
        return ToolResult(checked, is_error=True)
    kind, title, labels, series = checked
    path = str(args.get("path") or "").strip()
    if not path.lower().endswith((".svg", ".png")):
        return ToolResult("path must end in .svg or .png", is_error=True)
    full = os.path.realpath(os.path.join(ctx.cwd, path))
    root = os.path.realpath(ctx.cwd)
    if os.path.commonpath([full, root]) != root:
        return ToolResult("path must be inside the working directory", is_error=True)
    if not ctx.approve(f"chart: write {path} ({kind}, {len(series)} series, {len(labels)} labels)"):
        return ToolResult("denied by approval policy", is_error=True)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    svg = render(kind, title, labels, series, str(args.get("unit") or ""), str(args.get("note") or ""))
    svg_path = full if full.lower().endswith(".svg") else full[:-4] + ".svg"
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(svg)
    written = [os.path.relpath(svg_path, root)]
    if full.lower().endswith(".png"):
        problem = to_png(svg_path, full)
        if problem:
            return ToolResult(f"wrote {written[0]} but not the PNG: {problem}", is_error=True)
        written.append(os.path.relpath(full, root))
    return ToolResult(f"wrote {', '.join(written)}\n\n{table(labels, series)}")


chart_tool = Tool(
    name="chart",
    description=(
        "Draw a chart from numbers you already computed (compute them with python_run first) and save it as "
        ".svg or .png in the working directory, in the company palette. kind: bar (compare categories, "
        "grouped for up to 5 series), hbar (rankings, long names), line (change over time). One axis only; "
        "at most 5 series (fold the rest into 其他). Returns the data as a table too."
    ),
    mutating=True,
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "bar, hbar or line"},
            "title": {"type": "string", "description": "What the chart shows, e.g. 十月業績（依業務員）"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Categories or time points"},
            "series": {"type": "array", "description": 'Up to 5 of {"name": "...", "values": [numbers, one per label]}',
                       "items": {"type": "object", "properties": {
                           "name": {"type": "string"}, "values": {"type": "array", "items": {"type": "number"}}}}},
            "unit": {"type": "string", "description": "e.g. 萬元, %, 件"},
            "note": {"type": "string", "description": "Source or caveat, e.g. 資料：十月業績表.xlsx"},
            "path": {"type": "string", "description": "Output file, .svg or .png, relative to the working directory"},
        },
        "required": ["kind", "title", "labels", "series", "path"],
    },
    execute=_run,
)


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [chart_tool])


chart_tool_plugin = Plugin("tool-chart", _apply)
