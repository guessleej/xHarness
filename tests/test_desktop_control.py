"""Desktop tools against a fake backend: element numbers, refusals, approvals that cannot be skipped."""
import pytest

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.tools import ToolContext, tools_plugin
from xharness.tools.desktop_control import Desktop, desktop_control_plugin, parse_combo, _make_tools


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.front = {"app": "TextEdit", "pid": 7, "secure": False}
        self.obeys = True
        self.button = [100, 200, 40, 20]

    def call(self, request, timeout=30):
        self.calls.append(request)
        op = request.get("op")
        if op == "look":
            return {"screen": [1800, 1169], "app": self.front["app"], "pid": self.front["pid"], "front": True,
                    "apps": ["Finder", "TextEdit"], "window": self.front.get("window", "t.txt"), "windows": ["t.txt"], "truncated": False,
                    "elements": [
                        {"role": "AXGroup", "subrole": "", "label": "", "value": "", "rect": [0, 0, 900, 900], "enabled": True, "focused": False},
                        {"role": "AXButton", "subrole": "", "label": "儲存", "value": "", "rect": list(self.button), "enabled": True, "focused": False},
                        {"role": "AXTextField", "subrole": "", "label": "", "value": "舊內容", "rect": [300, 400, 200, 30], "enabled": True, "focused": True},
                    ]}
        if op == "focus":
            return dict(self.front)
        if request.get("action") == "activate" and self.obeys:
            self.front = {**self.front, "app": "TextEdit", "pid": request["pid"]}
        return {}

    def actions(self):
        return [c for c in self.calls if "action" in c and c["action"] != "activate"]


@pytest.fixture()
def rig():
    backend = FakeBackend()
    look, act = _make_tools(Desktop(backend, settle=0), True, platform="darwin")
    asked = []
    ctx = ToolContext(cwd=".", approve=lambda s: asked.append(s) or True)
    return backend, look, act, ctx, asked


def test_look_numbers_labelled_elements_and_skips_empty_containers(rig):
    _backend, look, _act, ctx, asked = rig
    out = look.execute({}, ctx).output
    assert "[1] AXButton 「儲存」 at (120,210)" in out
    assert "[2] AXTextField value=「舊內容」 at (400,415) focused" in out
    assert "AXGroup" not in out
    assert asked == ["desktop_look: read the screen (the app in front)"]


def test_click_by_element_uses_its_centre_and_names_it_in_the_approval(rig):
    backend, look, act, ctx, asked = rig
    look.execute({}, ctx)
    result = act.execute({"action": "click", "element": 1}, ctx)
    assert not result.is_error
    assert backend.actions() == [{"action": "click", "x": 120, "y": 210, "button": "left", "count": 1}]
    assert asked[-1] == "desktop click: AXButton「儲存」 in TextEdit at (120,210)"
    assert "The screen now:" in result.output


def test_element_numbers_need_a_look_and_go_stale_when_another_app_is_in_front(rig):
    backend, look, act, ctx, _asked = rig
    assert "desktop_look first" in act.execute({"action": "click", "element": 1}, ctx).output
    look.execute({}, ctx)
    backend.front = {"app": "LINE", "pid": 9, "secure": False}
    result = act.execute({"action": "click", "element": 1}, ctx)
    assert result.is_error and "LINE is in front now" in result.output
    assert act.execute({"action": "click", "element": 99}, ctx).is_error
    assert backend.actions() == []


def test_type_pastes_and_refuses_password_fields(rig):
    backend, look, act, ctx, asked = rig
    look.execute({}, ctx)
    assert not act.execute({"action": "type", "element": 2, "text": "新內容", "submit": True}, ctx).is_error
    assert [a["action"] for a in backend.actions()] == ["click", "paste", "key"]
    assert "「新內容」 then press return" in asked[-1]
    backend.calls.clear()
    act.execute({"action": "type", "text": "全部換掉", "replace": True}, ctx)
    assert [a.get("code") for a in backend.actions()] == [0, None] and "replacing what is there" in asked[-1]
    backend.calls.clear()
    backend.front = {"app": "TextEdit", "pid": 7, "secure": True}
    result = act.execute({"action": "type", "text": "hunter2"}, ctx)
    assert result.is_error and "password" in result.output
    assert not any(a["action"] == "paste" for a in backend.actions())


def test_character_keys_are_refused_in_a_password_field(rig):
    backend, _look, act, ctx, _asked = rig
    backend.front = {"app": "TextEdit", "pid": 7, "secure": True}
    result = act.execute({"action": "key", "keys": "shift+h"}, ctx)
    assert result.is_error and "password" in result.output and backend.actions() == []
    assert not act.execute({"action": "key", "keys": "tab"}, ctx).is_error      # moving on is fine
    assert not act.execute({"action": "key", "keys": "cmd+a"}, ctx).is_error
    assert all("typing" not in a for a in backend.actions())


def test_an_element_that_moves_while_waiting_for_approval_is_not_clicked(rig):
    backend, look, act, _ctx, _asked = rig
    def approve(summary):
        if summary.startswith("desktop click"):
            backend.button[1] = 500             # the window scrolled while the person read the prompt
        return True

    ctx = ToolContext(cwd=".", approve=approve)
    look.execute({}, ctx)
    result = act.execute({"action": "click", "element": 1}, ctx)
    assert result.is_error and "moved" in result.output and backend.actions() == []


def test_keys_parse_and_emptying_the_trash_is_refused_before_asking(rig):
    backend, _look, act, ctx, asked = rig
    assert parse_combo("cmd+s") == (1, 0x100000, frozenset({"cmd", "s"}))
    assert parse_combo("Return")[0] == 36
    with pytest.raises(ValueError):
        parse_combo("cmd+nosuchkey")
    result = act.execute({"action": "key", "keys": "cmd+shift+delete"}, ctx)
    assert result.is_error and "Trash" in result.output
    assert act.execute({"action": "key", "keys": "cmd+option+shift+backspace"}, ctx).is_error
    assert asked == [] and backend.actions() == []


def test_denied_approval_sends_nothing(rig):
    backend, _look, act, _ctx, _asked = rig
    ctx = ToolContext(cwd=".", approve=lambda s: False)
    assert act.execute({"action": "open_app", "app": "Finder"}, ctx).output == "denied by approval policy"
    assert backend.actions() == []


def _agent(name, approval_mode, prompt):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("tool-desktop", desktop_control_plugin(desktop=Desktop(FakeBackend(), settle=0)))
    call = {"id": "c1", "name": "desktop_act", "arguments": '{"action": "open_app", "app": "Finder"}'}

    class LLM:
        model = "fake"
        turns = [AssistantTurn(content="", tool_calls=[call]), AssistantTurn(content="ok")]

        def stream(self, messages, tools, on_delta=None):
            self.tools = [t["function"]["name"] if "function" in t else t.get("name") for t in tools]
            return self.turns.pop(0)

    llm = LLM()
    harness.ctx.provide("llm", llm)
    return Agent(harness.ctx, AgentOptions(name=name, approval_mode=approval_mode, prompt=prompt)), llm


def test_auto_approval_mode_still_asks_the_person_and_nobody_means_no():
    asked = []
    agent, _llm = _agent("main", "auto", lambda s: asked.append(s) or False)
    agent.run("open Finder")
    assert asked == ["desktop open_app: bring Finder to the front"]
    tool_result = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert tool_result == "denied by approval policy"
    agent, _llm = _agent("main", "auto", None)
    agent.run("open Finder")
    assert [m for m in agent.messages if m["role"] == "tool"][0]["content"] == "denied by approval policy"


def test_only_the_main_agent_gets_the_desktop_tools():
    agent, llm = _agent("helper", "prompt", lambda s: True)
    agent.run("open Finder")
    assert not any(str(n).startswith("desktop_") for n in llm.tools)
    assert [m for m in agent.messages if m["role"] == "tool"][0]["content"] == "desktop_act is not available now"


def test_security_check_flags_desktop_control_without_per_action_approval():
    from xharness.config import ResolvedConfig
    from xharness.seccheck import run
    provider = {"base_url": "http://127.0.0.1:8080/v1", "model": "m"}
    item = lambda **kw: next(i for i in run(ResolvedConfig(provider=provider, provider_name="p", **kw), bound_host="127.0.0.1")["items"]
                             if i["id"] == "desktop-approval")
    assert item(desktop_control=True)["state"] == "pass"
    assert item(desktop_control=True, desktop_approval=False)["state"] == "warn"


def test_the_app_is_put_back_in_front_after_approval_and_nothing_is_sent_if_it_will_not_come(rig):
    backend, look, act, _ctx, _asked = rig
    browser = {"app": "Microsoft Edge", "pid": 3, "secure": False}

    def approve_in_browser(_summary):
        backend.front = dict(browser)     # the person clicked "allow" in a browser tab
        return True

    ctx = ToolContext(cwd=".", approve=approve_in_browser)
    assert not act.execute({"action": "key", "keys": "cmd+s"}, ctx).is_error
    assert {"action": "activate", "pid": 7} in backend.calls or any(c.get("action") == "activate" and c["pid"] == 7 for c in backend.calls)
    assert backend.actions() == [{"action": "key", "code": 1, "flags": 0x100000, "mods": ["cmd"], "key": "s"}]
    backend.calls.clear()
    backend.front, backend.obeys = {"app": "TextEdit", "pid": 7, "secure": False}, False
    result = act.execute({"action": "key", "keys": "cmd+s"}, ctx)
    assert result.is_error and "nothing was sent" in result.output
    assert backend.actions() == []


def test_windows_refuses_deleting_past_the_recycle_bin_and_sends_keys_by_name():
    backend = FakeBackend()
    look, act = _make_tools(Desktop(backend, settle=0), True, platform="win32")
    asked = []
    ctx = ToolContext(cwd=".", approve=lambda s: asked.append(s) or True)
    result = act.execute({"action": "key", "keys": "shift+delete"}, ctx)
    assert result.is_error and "Recycle Bin" in result.output and asked == []
    assert not act.execute({"action": "key", "keys": "ctrl+shift+s"}, ctx).is_error
    step = backend.actions()[-1]
    assert step["mods"] == ["ctrl", "shift"] and step["key"] == "s"
    look2, act2 = _make_tools(Desktop(FakeBackend(), settle=0), True, platform="darwin")
    assert not act2.execute({"action": "key", "keys": "shift+delete"}, ctx).is_error   # harmless on macOS


def test_an_app_running_as_administrator_gets_nothing(rig):
    backend, look, act, ctx, _asked = rig
    backend.front = {"app": "regedit", "pid": 7, "secure": False, "elevated": True}
    result = act.execute({"action": "key", "keys": "ctrl+s"}, ctx)
    assert result.is_error and "administrator" in result.output and backend.actions() == []


def test_the_packaged_app_runs_the_backend_through_itself(monkeypatch):
    import sys
    from xharness.tools import desktop_control as dc
    assert dc.backend_argv("desktop_win")[1:3] == ["-I", dc.os.path.join(dc.HERE, "desktop_win.py")]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert dc.backend_argv("desktop_win")[1:] == ["--desktop-backend", "desktop_win"]


def test_the_windows_screenshot_is_a_png_with_the_colours_in_order(tmp_path):
    import zlib
    from xharness.tools import desktop_win
    path = tmp_path / "s.png"
    desktop_win._png(str(path), 2, 1, bytes([1, 2, 3, 0, 4, 5, 6, 0]))     # two pixels, blue-green-red-pad
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG") and zlib.decompress(data[data.index(b"IDAT") + 4:-16]) == bytes([0, 3, 2, 1, 6, 5, 4])


def test_a_backend_that_dies_without_output_is_reported_as_such(monkeypatch):
    import subprocess
    from xharness.tools import desktop_control as dc
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0xC0000005, "", ""))
    with pytest.raises(RuntimeError, match="screen reader stopped"):
        dc.ScriptBackend("desktop_win").call({"op": "look"})


def test_another_document_in_the_same_app_is_a_changed_screen(rig):
    backend, look, act, ctx, asked = rig
    backend.front = {"app": "Notepad", "pid": 7, "secure": False, "window": "xh-test.txt - 記事本"}
    look.execute({}, ctx)
    assert "Notepad「xh-test.txt - 記事本」" in act.execute({"action": "key", "keys": "ctrl+s"}, ctx).output
    backend.front = {**backend.front, "window": "*xh-test.txt - 記事本"}         # an unsaved edit: same window
    assert not act.execute({"action": "key", "keys": "ctrl+s"}, ctx).is_error
    look.execute({}, ctx)
    backend.front = {**backend.front, "window": "hwinfo_report.ps1 - 記事本"}   # another tab came forward
    backend.calls.clear()
    result = act.execute({"action": "click", "element": 1}, ctx)
    assert result.is_error and "look again" in result.output and backend.actions() == []


def test_typing_says_whether_the_field_now_holds_the_text(rig):
    backend, look, act, ctx, _asked = rig
    look.execute({}, ctx)
    out = act.execute({"action": "type", "element": 2, "text": "舊內容", "replace": True}, ctx).output
    assert "holds exactly that text. Do not type it again" in out
    out = act.execute({"action": "type", "text": "別的字", "replace": True}, ctx).output
    assert "which is not that text" in out
