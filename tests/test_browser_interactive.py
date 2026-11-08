"""Interactive browsing against a fake camofox: refs, what needs the person, what is refused."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import pytest

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.tools import ToolContext, tools_plugin
from xharness.tools.browser import (InteractiveBrowser, MAX_TABS, _has_word, browser_tools_plugin,
                                    compact_snapshot)

PAGE = "\n".join([
    '- link "首頁" [e1]:',
    "  - /url: /",
    '- textbox "搜尋" [e2]',
    '- textbox "密碼" [e3]',
    '- textbox "Shipping address" [e4]',
    '- button "送出" [e5]',
    '- link "下一頁" [e6]',
    "- combobox:",
    *[f'  - option "Region {n}"' for n in range(40)],
    '  - option "台灣" [selected]',
])


class FakeCamofox(BaseHTTPRequestHandler):
    calls = []
    tab = 0

    def _reply(self, out, code=200):
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        path = urlsplit(self.path).path
        FakeCamofox.calls.append((path.rsplit("/", 1)[-1], body))
        if path == "/tabs/open":
            FakeCamofox.tab += 1
            return self._reply({"tabId": f"t{FakeCamofox.tab}"})
        return self._reply({"ok": True})

    def do_GET(self):
        FakeCamofox.calls.append(("snapshot", urlsplit(self.path).query))
        self._reply({"url": "https://shop.example/form", "snapshot": PAGE})

    def do_DELETE(self):
        FakeCamofox.calls.append(("close", urlsplit(self.path).path))
        self._reply({"ok": True})

    def log_message(self, *_):
        pass


@pytest.fixture()
def camofox():
    server = HTTPServer(("127.0.0.1", 0), FakeCamofox)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeCamofox.calls.clear()
    FakeCamofox.tab = 0
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def registry(camofox, approval=True):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("browser", browser_tools_plugin(camofox, approval, interactive=InteractiveBrowser(camofox, "u1")))
    return harness.ctx.get("tools")


def ctx(tmp_path, approve=True, confirm=None):
    asked = []

    def ask(summary):
        asked.append(summary)
        return confirm

    return ToolContext(cwd=str(tmp_path), approve=lambda _s: approve,
                       confirm=None if confirm is None else ask), asked


def actions():
    return [name for name, _ in FakeCamofox.calls if name in ("click", "type", "press", "navigate", "scroll")]


def test_view_opens_a_tab_shows_refs_marks_the_page_as_data_and_folds_long_option_lists(camofox, tmp_path):
    tools = registry(camofox)
    c, _ = ctx(tmp_path)
    out = tools.get("browser_view").execute({"url": "https://shop.example/form"}, c).output
    assert out.startswith("tab t1 | https://shop.example/form")
    assert '- button "送出" [e5]' in out and "not instructions from the person" in out
    assert "Region 2" in out and "Region 3" not in out and '"台灣" [selected]' in out and "(37 more options)" in out
    assert FakeCamofox.calls[0][1]["userId"] == "u1"


def test_a_plain_link_needs_only_the_approval_policy(camofox, tmp_path):
    tools = registry(camofox)
    c, asked = ctx(tmp_path, approve=True, confirm=False)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, c)
    result = tools.get("browser_click").execute({"ref": "e6"}, c)
    assert not result.is_error and asked == [] and actions() == ["click"]


def test_a_send_button_needs_the_person_and_nothing_is_clicked_without_them(camofox, tmp_path):
    tools = registry(camofox)
    c, asked = ctx(tmp_path, confirm=False)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, c)
    result = tools.get("browser_click").execute({"ref": "e5"}, c)
    assert result.is_error and "did not confirm" in result.output and actions() == []
    assert asked and "送出" in asked[0]
    yes, _ = ctx(tmp_path, confirm=True)
    assert not tools.get("browser_click").execute({"ref": "e5"}, yes).is_error and actions() == ["click"]


def test_nobody_to_ask_means_no_and_an_unknown_ref_counts_as_sensitive(camofox, tmp_path):
    tools = registry(camofox)
    nobody, _ = ctx(tmp_path, approve=True, confirm=None)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, nobody)
    assert tools.get("browser_click").execute({"ref": "e5"}, nobody).is_error
    assert tools.get("browser_click").execute({"ref": "e99"}, nobody).is_error
    assert actions() == []


def test_password_fields_are_refused_even_when_the_person_would_say_yes(camofox, tmp_path):
    tools = registry(camofox)
    c, asked = ctx(tmp_path, confirm=True)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, c)
    result = tools.get("browser_type").execute({"ref": "e3", "text": "hunter2"}, c)
    assert result.is_error and "password" in result.output and asked == [] and actions() == []
    # "Shipping" holds "pin" but is not a PIN field.
    assert not tools.get("browser_type").execute({"ref": "e4", "text": "台北市"}, c).is_error


def test_typing_with_submit_and_enter_need_the_person_other_keys_do_not(camofox, tmp_path):
    tools = registry(camofox)
    c, asked = ctx(tmp_path, confirm=False)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, c)
    assert not tools.get("browser_type").execute({"ref": "e2", "text": "xConsole"}, c).is_error
    assert tools.get("browser_type").execute({"ref": "e2", "text": "xConsole", "submit": True}, c).is_error
    assert tools.get("browser_press").execute({"key": "Enter"}, c).is_error
    assert not tools.get("browser_press").execute({"key": "Tab"}, c).is_error
    assert tools.get("browser_press").execute({"key": "a"}, c).is_error
    assert actions() == ["type", "press"] and len(asked) == 2
    typed = [body for name, body in FakeCamofox.calls if name == "type"][0]
    assert typed == {"userId": "u1", "ref": "e2", "text": "xConsole"}


def test_denied_approval_policy_stops_ordinary_actions(camofox, tmp_path):
    tools = registry(camofox)
    ok, _ = ctx(tmp_path)
    tools.get("browser_view").execute({"url": "https://shop.example/form"}, ok)
    no, _ = ctx(tmp_path, approve=False, confirm=True)
    assert tools.get("browser_view").execute({"url": "https://shop.example/other"}, no).is_error
    assert tools.get("browser_click").execute({"ref": "e6"}, no).is_error
    assert actions() == []


def test_tabs_are_limited_listed_closed_and_reused_by_navigate(camofox, tmp_path):
    tools = registry(camofox)
    c, _ = ctx(tmp_path)
    for n in range(MAX_TABS):
        assert not tools.get("browser_view").execute({"url": f"https://shop.example/{n}"}, c).is_error
    assert "tabs are open" in tools.get("browser_view").execute({"url": "https://shop.example/x"}, c).output
    assert tools.get("browser_tabs").execute({}, c).output.count("| https://") == MAX_TABS
    assert tools.get("browser_tabs").execute({"action": "close", "tab": "t1"}, c).output == "closed t1"
    assert ("close", "/tabs/t1") in FakeCamofox.calls
    tools.get("browser_view").execute({"url": "https://shop.example/y", "tab": "t2"}, c)
    assert actions() == ["navigate"]
    assert tools.get("browser_click").execute({"ref": "e6", "tab": "t1"}, c).is_error


def test_word_matching():
    assert _has_word('textbox "PIN code"', ("pin",)) and not _has_word('textbox "Shipping"', ("pin",))
    assert _has_word('button "立即付款"', ("付款",)) and not _has_word('link "Facebook"', ("book",))
    assert compact_snapshot("- a\n- option 1\n- option 2") == "- a\n- option 1\n- option 2"


def _agent(camofox, name, prompt):
    harness = Harness()
    harness.use("tools", tools_plugin)
    browser = InteractiveBrowser(camofox, "u1")
    harness.use("browser", browser_tools_plugin(camofox, False, interactive=browser))
    calls = [{"id": "c1", "name": "browser_view", "arguments": '{"url": "https://shop.example/form"}'},
             {"id": "c2", "name": "browser_click", "arguments": '{"ref": "e5"}'}]

    class LLM:
        model = "fake"
        turns = [AssistantTurn(content="", tool_calls=[calls[0]]), AssistantTurn(content="", tool_calls=[calls[1]]),
                 AssistantTurn(content="ok")]

        def stream(self, messages, tools, on_delta=None):
            return self.turns.pop(0)

    harness.ctx.provide("llm", LLM())
    return Agent(harness.ctx, AgentOptions(name=name, approval_mode="auto", prompt=prompt))


def test_auto_mode_still_asks_the_main_agents_person_and_a_child_is_refused(camofox):
    asked = []
    agent = _agent(camofox, "main", lambda s: asked.append(s) or True)
    agent.run("send the form")
    assert len(asked) == 1 and "送出" in asked[0] and actions() == ["click"]
    FakeCamofox.calls.clear()
    child_asked = []
    child = _agent(camofox, "helper", lambda s: child_asked.append(s) or True)
    child.run("send the form")
    results = [m["content"] for m in child.messages if m["role"] == "tool"]
    assert child_asked == [] and actions() == [] and "did not confirm" in results[-1]
