"""chart: valid SVG in the company palette, the charting rules, and what is refused."""
import re
import shutil
import xml.etree.ElementTree as ET

import pytest

from xharness.tools import ToolContext
from xharness.tools.chart import PALETTE, chart_tool, fmt, nice_ticks, render, text_width

SALES = {"kind": "hbar", "title": "十月業績（依業務員）", "unit": "萬元", "labels": ["王大明", "李小華", "陳美玲"],
         "series": [{"name": "業績", "values": [1280, 1105.5, 980]}], "path": "out/sales.svg"}


def ctx(tmp_path, approve=True, asked=None):
    return ToolContext(cwd=str(tmp_path), approve=lambda s: (asked.append(s) if asked is not None else None) or approve)


def test_writes_an_svg_and_returns_the_data_as_a_table(tmp_path):
    result = chart_tool.execute(SALES, ctx(tmp_path))
    assert not result.is_error and result.output.startswith("wrote out/sales.svg")
    assert "| 李小華 | 1,105.5 |" in result.output
    svg = (tmp_path / "out" / "sales.svg").read_text(encoding="utf-8")
    ET.fromstring(svg)                                       # well-formed
    assert "十月業績（依業務員）" in svg and "單位：萬元" in svg and "Microsoft JhengHei" in svg


def test_the_rules_are_kept():
    series = [{"name": n, "values": [1, 2, 3]} for n in ("甲", "乙", "丙")]
    svg = render("line", "趨勢", ["一", "二", "三"], series)
    fills = re.findall(r'fill="none" stroke="(#[0-9a-f]{6})"', svg)
    assert fills == list(PALETTE[:3])                         # fixed order, never cycled
    sizes = {int(x) for x in re.findall(r'font-size="(\d+)"', svg)}
    assert min(sizes) >= 15                                   # no text under 15 px
    texts = re.findall(r'<text[^>]*fill="(#[0-9a-f]{6})"', svg)
    assert not set(texts) & set(PALETTE)                      # text never wears a series colour
    assert svg.count('rx="3" fill=') == 3                     # a legend for two or more series
    single = render("bar", "單一", ["a", "b"], [{"name": "x", "values": [1, 2]}])
    assert 'rx="3" fill=' not in single                       # none for one series


def test_line_end_labels_never_overlap():
    series = [{"name": f"系列{i}", "values": [10, 20, 30 + i * 0.1]} for i in range(4)]
    svg = render("line", "收斂", ["a", "b", "c"], series)
    ys = sorted(float(y) for y in re.findall(r'<text x="[\d.]+" y="([\d.]+)" font-size="15" fill="#16202a" text-anchor="start" font-weight="400">系列', svg))
    assert len(ys) == 4 and all(b - a >= 20 for a, b in zip(ys, ys[1:]))


def test_refusals(tmp_path):
    c = ctx(tmp_path)
    bad = [
        ({**SALES, "kind": "pie"}, "no pie"),
        ({**SALES, "series": [{"name": str(i), "values": [1, 2, 3]} for i in range(6)]}, "其他"),
        ({**SALES, "series": [{"name": "x", "values": [1, 2]}]}, "needs 3"),
        ({**SALES, "series": [{"name": "x", "values": [1, "a", 3]}]}, "numbers"),
        ({**SALES, "series": [{"name": "x", "values": [1, float("nan"), 3]}]}, "finite"),
        ({**SALES, "title": ""}, "title"),
        ({**SALES, "path": "../escape.svg"}, "inside the working directory"),
        ({**SALES, "path": "x.pdf"}, ".svg or .png"),
    ]
    for args, words in bad:
        result = chart_tool.execute(args, c)
        assert result.is_error and words in result.output, (words, result.output)
    assert not list(tmp_path.rglob("*.svg"))


def test_approval_names_the_file_and_a_no_writes_nothing(tmp_path):
    asked = []
    assert chart_tool.execute(SALES, ctx(tmp_path, False, asked)).is_error
    assert asked == ["chart: write out/sales.svg (hbar, 1 series, 3 labels)"] and not (tmp_path / "out").exists()


@pytest.mark.skipif(not shutil.which("rsvg-convert"), reason="needs rsvg-convert")
def test_png(tmp_path):
    result = chart_tool.execute({**SALES, "path": "sales.png"}, ctx(tmp_path))
    assert not result.is_error and (tmp_path / "sales.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_helpers():
    assert nice_ticks(0, 4010) == [0, 1000, 2000, 3000, 4000, 5000]
    assert nice_ticks(-30, 70)[0] < 0 < nice_ticks(-30, 70)[-1]
    assert fmt(1105.5) == "1,105.5" and fmt(0.125) == "0.12" and fmt(3.0) == "3"
    assert text_width("王a", 15) == pytest.approx(15 + 15 * 0.58)
