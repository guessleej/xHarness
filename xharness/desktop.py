"""Desktop window: the same Web UI, in a native window, no browser tab.

`xharness desktop` starts the web server on a random loopback port and opens
it in the operating system's own web view (WebKit on macOS, WebView2 on
Windows, WebKitGTK on Linux) through pywebview — the one optional
dependency (`pip install "xharness[desktop]"`). Closing the window stops
the server and disposes every conversation.

Nothing about the harness changes: same config file, same approvals, same
session log, same fleet view. Because the server binds a loopback port that
only this process knows, no token is needed and nothing is reachable from
the network.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable

from . import __version__
from .config import ResolvedConfig, harness_home, load_config

WINDOW_TITLE = "xHarness"
WINDOW_SIZE = (1180, 820)

SETUP_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>xHarness</title>
<style>body{font-family:"Microsoft JhengHei","PingFang TC",system-ui,sans-serif;background:#f4f7f9;color:#16202a;margin:0;padding:48px 24px;line-height:1.7}
.card{max-width:720px;margin:0 auto;background:#fff;border-radius:16px;padding:32px 36px;box-shadow:0 12px 32px rgba(22,32,42,.11)}
h1{color:#bf181f;font-size:26px;margin:0 0 8px}p{margin:8px 0}code,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px}
pre{background:#f4f7f9;border-radius:10px;padding:14px 16px;overflow:auto}</style></head><body><div class="card">
<h1>xHarness 還沒有設定</h1>
<p>{error}</p>
<p>請建立設定檔 <code>{path}</code>，內容至少要有一個模型端點，例如：</p>
<pre>default_provider = "local"
approval = "prompt"

[providers.local]
base_url = "http://127.0.0.1:8080/v1"
model = "your-model-id"</pre>
<p>存檔後重新開啟 xHarness 即可。完整範例見 repo 內的 <code>xharness.example.zh.toml</code>。</p>
</div></body></html>"""


def _config_path() -> str:
    return os.path.join(harness_home(), "config.toml")


def run_desktop(
    config_path: str | None = None,
    approval_mode: str | None = None,
    webview: Any | None = None,
    start_server: Callable[..., Any] | None = None,
) -> int:
    """Open the window. `webview` and `start_server` are injectable for tests."""
    if webview is None:
        try:
            import webview as _webview  # type: ignore[import-not-found]
        except ImportError:
            print(
                'xharness: the desktop window needs pywebview; install with  pip install "xharness[desktop]"',
                file=sys.stderr,
            )
            return 1
        webview = _webview
    if start_server is None:
        from .web import start_server as _start_server

        start_server = _start_server

    server = app = None
    try:
        config: ResolvedConfig = load_config(config_path)
        server, app, url = start_server(
            config, host="127.0.0.1", port=0, token=None, approval_mode=approval_mode or config.approval
        )
    except Exception as error:  # noqa: BLE001 - a missing config is the normal first-run case, shown in the window
        html = SETUP_HTML.replace("{error}", str(error)).replace("{path}", _config_path())
        webview.create_window(WINDOW_TITLE, html=html, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])
        webview.start()
        return 1

    threading.Thread(target=server.serve_forever, name="xharness-desktop-server", daemon=True).start()
    print(f"xHarness {__version__} desktop at {url}  (model: {config.provider['model']})", file=sys.stderr)
    window = webview.create_window(
        WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=(720, 520), text_select=True
    )
    try:
        webview.start(private_mode=False)  # keep localStorage (theme, last conversation) between launches
    finally:
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - already closed
            pass
        server.shutdown()
        server.server_close()
        app.dispose()
    return 0
