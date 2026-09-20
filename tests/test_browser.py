"""Browser tools against a fake camofox: search parses results, open returns rendered text, approval gates."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.presets import build_harness
from xharness.tools import ToolContext
from xharness.tools.browser import Browser, browser_tools_plugin

RESULTS = [{"title": "臺灣海洋保育署", "url": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.oca.gov.tw%2Fch%2F&rut=1", "snippet": "海洋保育"},
           {"title": "junk", "url": "javascript:void(0)", "snippet": ""}]


class FakeCamofox(BaseHTTPRequestHandler):
    opened = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tabs/open":
            FakeCamofox.opened.append(body["url"])
            out = {"tabId": "t1"}
        else:
            expr = body["expression"]
            out = {"result": json.dumps(RESULTS) if "querySelectorAll('.result')" in expr else "第一行\n第二行 rendered"}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

    def do_DELETE(self):
        FakeCamofox.opened.append("closed:" + self.path)
        self.send_response(200); self.end_headers()

    def log_message(self, *_):
        pass


@pytest.fixture()
def camofox():
    server = HTTPServer(("127.0.0.1", 0), FakeCamofox)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeCamofox.opened.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def tools_with(camofox, approval=True):
    harness = Harness()
    from xharness.tools import tools_plugin
    harness.use("tools", tools_plugin)
    harness.use("browser", browser_tools_plugin(camofox, approval, browser=Browser(camofox, settle=0)))
    return harness.ctx.get("tools")


def test_search_parses_results_and_drops_non_http(camofox, tmp_path):
    registry = tools_with(camofox)
    ctx = ToolContext(cwd=str(tmp_path), approve=lambda _s: True)
    result = registry.get("web_search").execute({"query": "海洋保育署", "max_results": 5}, ctx)
    assert not result.is_error and "1. 臺灣海洋保育署" in result.output and "junk" not in result.output
    assert "https://www.oca.gov.tw/ch/" in result.output and "uddg" not in result.output
    assert FakeCamofox.opened[0].startswith("https://duckduckgo.com/html/?q=") and "closed:/tabs/t1" in FakeCamofox.opened[-1]


def test_open_returns_rendered_text_and_tab_is_closed(camofox, tmp_path):
    registry = tools_with(camofox)
    ctx = ToolContext(cwd=str(tmp_path), approve=lambda _s: True)
    result = registry.get("browser_open").execute({"url": "https://example.org/spa"}, ctx)
    assert result.output == "第一行\n第二行 rendered"
    assert any(o.startswith("closed:") for o in FakeCamofox.opened)
    assert registry.get("browser_open").execute({"url": "ftp://x"}, ctx).is_error


def test_approval_gate_and_opt_out(camofox, tmp_path):
    denied = ToolContext(cwd=str(tmp_path), approve=lambda _s: False)
    gated = tools_with(camofox, approval=True)
    assert gated.get("web_search").execute({"query": "x"}, denied).is_error
    assert gated.get("web_search").mutating is True
    free = tools_with(camofox, approval=False)
    assert not free.get("web_search").execute({"query": "x"}, denied).is_error
    assert free.get("web_search").mutating is False


def test_config_mounts_browser_tools_only_when_set(tmp_path):
    base = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "m"}, provider_name="p")
    off = build_harness(base, no_session=True)
    assert off.ctx.get("tools").get("web_search") is None
    on = build_harness(ResolvedConfig(**{**base.__dict__, "browser": "http://127.0.0.1:9"}), no_session=True)
    assert on.ctx.get("tools").get("web_search") is not None and on.ctx.get("tools").get("browser_open") is not None
