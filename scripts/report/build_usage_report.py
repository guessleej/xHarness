#!/usr/bin/env python3
"""Turn the service usage report into a Word document management can read.

    xharness report --since 30d --json report.json
    python3 scripts/report/build_usage_report.py report.json -o 使用報告.docx

or straight from a running node:

    python3 scripts/report/build_usage_report.py --url http://node:3080 --token-file ~/.xharness/node-token

Design rules this file follows on purpose (they are house style, not taste):
- Microsoft JhengHei throughout, brand red #BF181F, no accent bars anywhere,
  tables without gridlines
- the cover reads eyebrow, then title, then one-line subtitle, then company
  and date -- the title is black, only the eyebrow is red
- the table of contents uses bookmarks and internal hyperlinks, not a TOC
  field, so it works the moment the file is opened
- every figure is computed from the data, never drawn by hand; a number
  nobody collected is written as such instead of being shown as zero

python-docx is required; matplotlib is optional (charts are skipped without
it). Neither is a dependency of xharness itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - only used for an explicit --pdf conversion
import shutil
import sys
import urllib.request
from datetime import datetime, timezone

BRAND = "BF181F"
INK = "16202A"
GREY = "6B7A85"
FONT = "Microsoft JhengHei"

CHAPTERS = [
    ("ch1", "一、這段期間誰在用"),
    ("ch2", "二、花了多少"),
    ("ch3", "三、自建與外購的分工"),
    ("ch4", "四、設備有沒有被用到"),
    ("ch5", "五、對外串接的安全狀態"),
    ("ch6", "六、系統健康與治理"),
]


def fetch(url: str, token: str | None) -> dict:
    request = urllib.request.Request(  # nosec B310 - scheme is validated below
        url.rstrip("/") + "/api/usage-report?since=30d",
        headers={"X-XHarness-Client": "report"},
    )
    if not url.startswith(("http://", "https://")):
        raise SystemExit("--url must be an http(s) address")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


# --- document helpers --------------------------------------------------


def _style(document):
    from docx.shared import Pt

    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(11)
    normal._element.rPr.rFonts.set(_qn("w:eastAsia"), FONT)


def _qn(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _run(paragraph, text, *, size=11, bold=False, color=INK, italic=False):
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(text)
    run.font.name = FONT
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    run.font.color.rgb = RGBColor.from_string(color)
    run._element.rPr.rFonts.set(_qn("w:eastAsia"), FONT)
    return run


def _bookmark(paragraph, name: str, index: int) -> None:
    from docx.oxml import OxmlElement

    start = OxmlElement("w:bookmarkStart")
    start.set(_qn("w:id"), str(index))
    start.set(_qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(_qn("w:id"), str(index))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _link(paragraph, anchor: str, text: str) -> None:
    """An internal hyperlink: clickable the moment the file opens, no F9 needed."""
    from docx.oxml import OxmlElement

    link = OxmlElement("w:hyperlink")
    link.set(_qn("w:anchor"), anchor)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(_qn("w:ascii"), FONT)
    fonts.set(_qn("w:eastAsia"), FONT)
    properties.append(fonts)
    run.append(properties)
    text_element = OxmlElement("w:t")
    text_element.text = text
    run.append(text_element)
    link.append(run)
    paragraph._p.append(link)


def _no_borders(table) -> None:
    """python-pptx and python-docx both default to hairlines; house style has none."""
    from docx.oxml import OxmlElement

    properties = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(_qn("w:val"), "none")
        element.set(_qn("w:sz"), "0")
        borders.append(element)
    properties.append(borders)


def _shade(cell, color: str) -> None:
    from docx.oxml import OxmlElement

    shading = OxmlElement("w:shd")
    shading.set(_qn("w:val"), "clear")
    shading.set(_qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shading)


def table(document, headers: list[str], rows: list[list[str]]):
    from docx.shared import Pt

    grid = document.add_table(rows=1, cols=len(headers))
    _no_borders(grid)
    for index, title in enumerate(headers):
        cell = grid.rows[0].cells[index]
        cell.text = ""
        _run(cell.paragraphs[0], title, size=10, bold=True, color="FFFFFF")
        _shade(cell, BRAND)
    for row in rows:
        cells = grid.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = ""
            _run(cells[index].paragraphs[0], str(value), size=10)
            if len(grid.rows) % 2 == 0:
                _shade(cells[index], "F4F7F9")
    document.add_paragraph().paragraph_format.space_after = Pt(6)
    return grid


def heading(document, anchor: str, text: str, index: int):
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(18)
    paragraph.paragraph_format.space_after = Pt(8)
    _run(paragraph, text, size=17, bold=True)
    _bookmark(paragraph, anchor, index)
    return paragraph


def body(document, text: str):
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(6)
    _run(paragraph, text)
    return paragraph


def chart(report: dict, out_dir: str) -> str | None:
    """One figure: tokens by user. Skipped entirely when matplotlib is absent."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return None
    people = report["people"]["top"][:8]
    if not people:
        return None
    for candidate in (FONT, "PingFang TC", "Noto Sans TC"):
        if any(candidate in font.name for font in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False
    figure, axes = plt.subplots(figsize=(7.2, max(2.2, 0.46 * len(people))), dpi=200)
    names = [row["user"] for row in people][::-1]
    values = [row["tokens"] for row in people][::-1]
    axes.barh(names, values, color="#bf181f", height=0.58)
    axes.set_xlabel("tokens")
    for spine in ("top", "right", "left"):
        axes.spines[spine].set_visible(False)
    axes.tick_params(left=False)
    axes.grid(axis="x", color="#e4eaee", linewidth=0.8)
    axes.set_axisbelow(True)
    figure.tight_layout()
    path = os.path.join(out_dir, "_usage_by_user.png")
    figure.savefig(path, transparent=True)
    plt.close(figure)
    return path


def build_docx(report: dict, output: str) -> str:
    """The whole document. Named so the delivery checklist can find it."""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt
    except ImportError:
        raise SystemExit(
            "this builder needs python-docx:  pip install python-docx  (optional matplotlib adds the charts)"
        )

    document = Document()
    _style(document)
    today = datetime.now(timezone.utc).astimezone()
    roc_date = f"{today.year - 1911} 年 {today.month} 月 {today.day} 日"

    # --- cover: eyebrow, title, subtitle, company and date --------------
    for _ in range(4):
        document.add_paragraph()
    eyebrow = document.add_paragraph()
    eyebrow.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(eyebrow, "服務使用報告", size=13, bold=True, color=BRAND)
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(title, "xHarness 服務使用與治理報告", size=30, bold=True)
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(subtitle, f"統計期間近 {report['since']}，數字全部由平台自身記錄計算", size=12, color=GREY)
    for _ in range(3):
        document.add_paragraph()
    footer = document.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(footer, f"云碩科技股份有限公司　{roc_date}", size=11, color=GREY)
    document.add_page_break()

    # --- contents --------------------------------------------------------
    contents = document.add_paragraph()
    _run(contents, "目錄", size=20, bold=True)
    for index, (anchor, text) in enumerate(CHAPTERS, start=1):
        line = document.add_paragraph()
        line.paragraph_format.space_after = Pt(4)
        _link(line, anchor, text)
    document.add_page_break()

    people, spend, hosting = report["people"], report["spend"], report["hosting"]
    health, machines, outbound = report["health"], report["machines"], report["outbound"]

    heading(document, "ch1", CHAPTERS[0][1], 1)
    body(document, (
        f"本期共有 {people['active']} 位使用者實際送出任務，平台開通 {people['accounts']} 個帳號"
        f"（其中 {people['disabled_accounts']} 個已停用）。"
        + ("使用者身分驗證已啟用，每一筆用量都歸得到人。"
           if people["identity"] == "on"
           else "使用者身分驗證尚未啟用，因此本期用量無法回答「是誰用掉的」，這一點列在第六章。")
    ))
    figure = chart(report, os.path.dirname(os.path.abspath(output)) or ".")
    if figure:
        document.add_picture(figure, width=Inches(6.4))
        os.remove(figure)
    table(document, ["使用者", "任務數", "tokens", "工具呼叫"],
          [[row["user"], row["tasks"], f"{row['tokens']:,}", row["tool_calls"]] for row in people["top"]]
          or [["本期無資料", "0", "0", "0"]])

    heading(document, "ch2", CHAPTERS[1][1], 2)
    body(document, (
        f"本期累計 {spend['tokens']:,} tokens，分佈在 {spend['tasks']:,} 個任務、"
        f"{spend['calls']:,} 次模型呼叫與 {spend['tool_calls']:,} 次工具呼叫。"
    ))
    body(document, spend["cost"]["assumption"]
         + (f"，以此換算本期約 {spend['cost']['estimate']:,}。" if spend["cost"]["estimate"] else "。"))
    table(document, ["模型", "使用過的工作階段數"],
          [[name, count] for name, count in spend["models"]] or [["本期無資料", "0"]])

    heading(document, "ch3", CHAPTERS[2][1], 3)
    total = hosting["self_hosted"]["tokens"] + hosting["external"]["tokens"]
    share = f"{hosting['self_hosted']['tokens'] / total * 100:.1f}%" if total else "本期未收集"
    body(document, (
        f"地端自建推論處理了 {hosting['self_hosted']['tokens']:,} tokens，外購 API 處理了 "
        f"{hosting['external']['tokens']:,} tokens，自建佔比 {share}。"
        + (f"另有 {hosting['unknown']['tokens']:,} tokens 的來源未記錄，屬於升級前建立的工作階段。"
           if hosting["unknown"]["tokens"] else "")
    ))
    table(document, ["來源", "tokens", "工作階段"],
          [["地端自建", f"{hosting['self_hosted']['tokens']:,}", hosting["self_hosted"]["sessions"]],
           ["外購 API", f"{hosting['external']['tokens']:,}", hosting["external"]["sessions"]],
           ["來源未記錄", f"{hosting['unknown']['tokens']:,}", hosting["unknown"]["sessions"]]])

    heading(document, "ch4", CHAPTERS[3][1], 4)
    body(document, (
        f"平台設定了 {machines['configured_nodes']} 個運算節點。"
        + (f"本期有 {len(machines['unreachable'])} 個節點無法連線："
           + "、".join(machines["unreachable"]) + "。" if machines["unreachable"] else "本期全部節點皆可連線。")
    ))
    table(document, ["節點", "可連線", "tokens", "對話數"],
          [[row["node"], "是" if row["reachable"] else "否",
            f"{row['tokens']:,}" if isinstance(row["tokens"], int) else "本期未收集",
            row["conversations"] if row["conversations"] is not None else "—"] for row in machines["rows"]])

    heading(document, "ch5", CHAPTERS[4][1], 5)
    body(document, (
        "本節列出這套系統所有可能對外連線的管道與其目前狀態。"
        + ("所有項目皆符合建議設定。" if not outbound["unsafe"]
           else "以下項目未達建議設定：" + "、".join(outbound["unsafe"]) + "，處理方式列在第六章。")
    ))
    table(document, ["項目", "狀態", "說明"],
          [[item["name"], item["state"], item["detail"]] for item in outbound["items"]])

    heading(document, "ch6", CHAPTERS[5][1], 6)
    body(document, (
        f"本期記錄到 {health['errors']} 次任務失敗、{health['budget_stops']} 次因預算或配額中斷、"
        f"{health['failed_signins']} 次登入失敗（其中 {health['lockouts']} 次觸發帳號暫時鎖定）。"
    ))
    if health["issues"]:
        table(document, ["待處理事項", "說明", "建議做法"],
              [[row["item"], row["detail"], row["action"]] for row in health["issues"]])
    else:
        body(document, "本期無異常。")
    body(document, "治理原則：" + health["governance"])

    document.save(output)
    return output


def to_pdf(docx_path: str) -> str | None:
    """Optional PDF via LibreOffice, when it happens to be installed."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print("skipping PDF: LibreOffice (soffice) is not installed", file=sys.stderr)
        return None
    target = os.path.dirname(os.path.abspath(docx_path)) or "."
    subprocess.run(  # nosec B603 - fixed argv, path comes from our own argument
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", target, docx_path],
        check=False, capture_output=True, timeout=180,
    )
    pdf = os.path.splitext(docx_path)[0] + ".pdf"
    return pdf if os.path.exists(pdf) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the xHarness service usage report as a Word document")
    parser.add_argument("json_file", nargs="?", help="report JSON from: xharness report --json <file>")
    parser.add_argument("--url", help="fetch straight from a running node instead")
    parser.add_argument("--token", help="bearer token for --url")
    parser.add_argument("--token-file", dest="token_file", help="file holding the bearer token")
    parser.add_argument("-o", "--output", default="xharness-使用報告.docx")
    parser.add_argument("--pdf", action="store_true", help="also convert to PDF with LibreOffice")
    args = parser.parse_args(argv)

    if args.url:
        token = args.token
        if args.token_file:
            with open(os.path.expanduser(args.token_file), encoding="utf-8") as handle:
                token = handle.read().strip()
        report = fetch(args.url, token)
    elif args.json_file:
        with open(args.json_file, encoding="utf-8") as handle:
            report = json.load(handle)
    else:
        parser.error("give a JSON file or --url")

    path = build_docx(report, args.output)
    print(f"wrote {path}")
    if args.pdf:
        pdf = to_pdf(path)
        if pdf:
            print(f"wrote {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
