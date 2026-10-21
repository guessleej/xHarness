"""Change previews: what a write or edit is about to do, shown before approval."""

import json
import os
import time

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.telemetry import telemetry_plugin
from xharness.tools import tools_plugin
from xharness.tools.fs import fs_tools_plugin
from xharness.session import session_summary
from xharness.web import MAX_DIFF_LINES, WebApp, preview_change

CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")


def args(**kwargs):
    return json.dumps(kwargs, ensure_ascii=False)


def test_edit_shows_the_lines_that_change(tmp_path):
    (tmp_path / "n.md").write_text("標題\n日期：11 月 15 日\n結尾\n", encoding="utf-8")
    change = preview_change("edit", args(path="n.md", old_string="11 月 15 日", new_string="11 月 22 日"), str(tmp_path))
    assert change["path"] == "n.md" and not change["created"]
    assert "-日期：11 月 15 日" in change["diff"] and "+日期：11 月 22 日" in change["diff"]
    assert (change["added"], change["removed"]) == (1, 1)
    assert not any(line.startswith(("---", "+++")) for line in change["diff"])


def test_write_to_a_new_file_is_marked_created(tmp_path):
    change = preview_change("write", args(path="sub/new.txt", content="a\nb\n"), str(tmp_path))
    assert change["created"] and change["added"] == 2 and change["removed"] == 0


def test_replace_all_is_honoured(tmp_path):
    (tmp_path / "f.txt").write_text("x\nx\n", encoding="utf-8")
    one = preview_change("edit", args(path="f.txt", old_string="x", new_string="y"), str(tmp_path))
    every = preview_change("edit", args(path="f.txt", old_string="x", new_string="y", replace_all=True), str(tmp_path))
    assert one["added"] == 1 and every["added"] == 2


def test_nothing_to_show_returns_none(tmp_path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    assert preview_change("read", args(path="f.txt"), str(tmp_path)) is None
    assert preview_change("edit", "not json", str(tmp_path)) is None
    assert preview_change("edit", args(path="f.txt", old_string="absent", new_string="x"), str(tmp_path)) is None
    assert preview_change("write", args(content="no path"), str(tmp_path)) is None


def test_long_diffs_are_cut_and_large_files_are_named_only(tmp_path):
    change = preview_change("write", args(path="big.txt", content="\n".join(map(str, range(2000)))), str(tmp_path))
    assert change["truncated"] and len(change["diff"]) == MAX_DIFF_LINES
    (tmp_path / "huge.bin").write_bytes(b"0" * 600_000)
    named = preview_change("write", args(path="huge.bin", content="x"), str(tmp_path))
    assert named["note"] and named["diff"] == []


def test_secrets_in_a_diff_are_masked(tmp_path):
    fake = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"   # assembled so scanners do not flag a made-up key
    change = preview_change("write", args(path=".env", content=f"OPENAI_API_KEY={fake}\n"), str(tmp_path))
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in "".join(change["diff"])


def test_paths_outside_the_working_directory_stay_absolute(tmp_path):
    inside, outside = tmp_path / "w", tmp_path / "o"
    inside.mkdir()
    outside.mkdir()
    change = preview_change("write", args(path=str(outside / "f.txt"), content="x"), str(inside))
    assert change["path"] == str(outside / "f.txt")


class Scripted:
    model = "fake"

    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, messages, tools, on_delta=None):
        return self.turns.pop(0) if self.turns else AssistantTurn(content="(done)")


def test_web_flow_without_accounts_previews_and_links_events(tmp_path, monkeypatch):
    """No [auth]: there is no per-user workspace, the agent works in the
    server's directory. The preview once crashed the whole task there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "n.md").write_text("日期：11 月 15 日\n", encoding="utf-8")
    edit = {"id": "e1", "name": "edit", "arguments": args(path="n.md", old_string="15", new_string="22")}
    adapter = Scripted([AssistantTurn(content="", tool_calls=[edit]), AssistantTurn(content="改好了")])

    def factory(resume, user=None):
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.use("tool-fs", fs_tools_plugin)
        harness.use("telemetry", telemetry_plugin())
        harness.ctx.provide("llm", adapter)
        return harness

    app = WebApp(CONFIG, approval_mode="auto", harness_factory=factory)
    conv = app.create()
    app.send(conv, "改日期")
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["type"] in ("done", "error") for e in conv.events):
        time.sleep(0.02)
    kinds = [e["type"] for e in conv.events]
    assert "error" not in kinds, [e for e in conv.events if e["type"] == "error"]
    start = next(e for e in conv.events if e["type"] == "tool_start")
    change = next(e for e in conv.events if e["type"] == "change")
    end = next(e for e in conv.events if e["type"] == "tool_end")
    assert start["call"] == change["call"] == end["call"] == 1
    assert kinds.index("change") < kinds.index("tool_end")
    assert "+日期：11 月 22 日" in change["diff"]
    assert (tmp_path / "n.md").read_text(encoding="utf-8") == "日期：11 月 22 日\n"


def test_a_broken_preview_never_stops_the_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("xharness.web.preview_change", lambda *a: 1 / 0)
    write = {"id": "w1", "name": "write", "arguments": args(path="a.txt", content="x")}
    adapter = Scripted([AssistantTurn(content="", tool_calls=[write]), AssistantTurn(content="ok")])

    def factory(resume, user=None):
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.use("tool-fs", fs_tools_plugin)
        harness.ctx.provide("llm", adapter)
        return harness

    app = WebApp(CONFIG, approval_mode="auto", harness_factory=factory)
    conv = app.create()
    app.send(conv, "寫檔")
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["type"] in ("done", "error") for e in conv.events):
        time.sleep(0.02)
    assert "done" in [e["type"] for e in conv.events] and "error" not in [e["type"] for e in conv.events]
    assert (tmp_path / "a.txt").read_text() == "x"


def test_session_preview_is_the_first_request(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    from xharness.session import sessions_dir

    os.makedirs(sessions_dir(None), exist_ok=True)
    with open(os.path.join(sessions_dir(None), "s1.jsonl"), "w", encoding="utf-8") as handle:
        for event in ({"type": "meta"}, {"type": "message", "role": "user", "content": "子代理的", "agent": "c1"},
                      {"type": "message", "role": "user", "content": "整理 README"}):
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    assert session_summary("s1", None) == {"title": "", "preview": "整理 README"}
    assert session_summary("missing", None) == {"title": "", "preview": ""}


def test_page_has_no_colour_bars_and_honours_hidden():
    import re

    from xharness.web import INDEX_HTML

    css = INDEX_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "[hidden]{display:none!important}" in css
    # no accent bars on any side: no border-left/right/top/bottom colour strips
    assert not re.search(r"border-(left|right|top|bottom)\s*:\s*\d+px\s+solid\s+var\(--(brand|warn|ok)", css)
    assert not re.search(r":before\{[^}]*background:var\(--brand", css)


def test_model_text_is_built_as_nodes_never_html():
    """Assistant replies go through md(); it must build DOM nodes, not HTML strings."""
    from xharness.web import INDEX_HTML

    body = INDEX_HTML.split("function inline(parent,text){", 1)[1].split("// ---- what a tool call is", 1)[0]
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body


def test_every_enter_to_send_field_is_ime_safe():
    from xharness.web import INDEX_HTML

    script = INDEX_HTML.split("<script>")[2]
    composer = script.split("// composer: IME-safe Enter", 1)[1].split("function autosize", 1)[0]
    assert "compositionstart" in composer and "isComposing" in composer and "keyCode===229" in composer
    users = script.split("the user-add form submits on Enter", 1)[1][:600]
    assert "compositionstart" in users and "isComposing" in users and "keyCode===229" in users


def test_side_panels_float_and_start_closed():
    """A docked full-height panel is the fixed-sidebar layout the product does
    not use; it also left the page lopsided. Both panels float and start closed."""
    from xharness.web import INDEX_HTML

    css = INDEX_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    panel = css.split(".drawer,.review{", 1)[1].split("}", 1)[0]
    assert "position:fixed" in panel and "border-radius" in panel
    assert "margin-left:-" not in css and "margin-right:-" not in css
    init = INDEX_HTML.split("async function init(){", 1)[1].split("})();", 1)[0]
    assert "openDrawer()" not in init and "openReview(" not in init
