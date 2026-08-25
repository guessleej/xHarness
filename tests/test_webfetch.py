import http.server
import threading

from xharness.tools import ToolContext
from xharness.tools.webfetch import webfetch_tool

PAGE = b"""<html><head><title>t</title><script>var x = 1;</script>
<style>body{}</style></head><body><h1>Hello</h1><p>World</p></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_args):
        pass


def serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_fetch_strips_html_to_text(tmp_path):
    server = serve()
    try:
        ctx = ToolContext(cwd=str(tmp_path), approve=lambda _s: True)
        result = webfetch_tool().execute(
            {"url": f"http://127.0.0.1:{server.server_address[1]}/"}, ctx
        )
        assert not result.is_error
        assert "Hello" in result.output and "World" in result.output
        assert "var x" not in result.output  # script content stripped
    finally:
        server.shutdown()


def test_scheme_and_approval_guards(tmp_path):
    ctx = ToolContext(cwd=str(tmp_path), approve=lambda _s: True)
    bad = webfetch_tool().execute({"url": "file:///etc/passwd"}, ctx)
    assert bad.is_error

    deny = ToolContext(cwd=str(tmp_path), approve=lambda _s: False)
    denied = webfetch_tool().execute({"url": "http://127.0.0.1:9/"}, deny)
    assert denied.is_error and "denied" in denied.output
