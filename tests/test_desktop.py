"""Desktop window: server on a random loopback port, window opens it, close shuts everything down, no-config page."""
import http.client
import json

from xharness.config import ResolvedConfig
from xharness.desktop import run_desktop
from xharness.web import start_server

from test_web import factory_with

CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")


class FakeWebview:
    """Records the window it was asked for and, on start(), probes the URL like the real webview would."""

    def __init__(self):
        self.windows = []
        self.probe = None

    def create_window(self, title, url=None, html=None, **kwargs):
        self.windows.append({"title": title, "url": url, "html": html, **kwargs})
        return self

    def start(self, **_kwargs):
        url = self.windows[-1]["url"]
        if url:
            host, port = url.split("//")[1].rstrip("/").split(":")
            conn = http.client.HTTPConnection(host, int(port), timeout=5)
            conn.request("GET", "/api/meta", headers={"Host": f"{host}:{port}"})
            response = conn.getresponse()
            self.probe = (response.status, json.loads(response.read()))
            conn.close()

    def destroy(self):
        pass


def test_window_opens_loopback_server_and_closes_it(monkeypatch):
    view = FakeWebview()
    monkeypatch.setattr("xharness.desktop.load_config", lambda _path: CONFIG)

    def start(config, **kwargs):
        assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 0 and kwargs["token"] is None
        return start_server(config, harness_factory=factory_with([]), **kwargs)

    assert run_desktop(webview=view, start_server=start) == 0
    window = view.windows[0]
    assert window["title"] == "xHarness" and window["url"].startswith("http://127.0.0.1:")
    assert view.probe[0] == 200 and view.probe[1]["model"] == "fake"
    # the server is gone once the window closed
    host, port = window["url"].split("//")[1].rstrip("/").split(":")
    try:
        http.client.HTTPConnection(host, int(port), timeout=1).request("GET", "/")
        alive = True
    except OSError:
        alive = False
    assert not alive


def test_missing_config_shows_setup_page(monkeypatch):
    view = FakeWebview()

    def broken(_path):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("xharness.desktop.load_config", broken)
    assert run_desktop(webview=view) == 1
    page = view.windows[0]["html"]
    assert "還沒有設定" in page and "no provider configured" in page and "config.toml" in page


def test_missing_pywebview_is_a_plain_message(monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no webview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert run_desktop() == 1
    assert 'xharness[desktop]' in capsys.readouterr().err
