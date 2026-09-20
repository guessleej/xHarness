"""Browser tools: `web_search` and `browser_open`, driven by a local camofox
browser service (a real Firefox behind a small HTTP API, default :9377).

Why a real browser and not a search API: the pages the agent reads are the
rendered DOM a person would see, search included (DuckDuckGo's HTML results
page opened in the browser), so there is no third-party API key, no vendor
quota, and nothing leaves the site except the query itself.

Disabled by default: `[tools] browser = "http://127.0.0.1:9377"` turns it on.
Both tools go through the approval policy like `webfetch` unless
`[tools] browser_approval = false`, which is reasonable on a node whose
browser is already sandboxed on its own machine.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

USER_ID = "xharness"
MAX_OUTPUT = 40_000
MAX_RESULTS = 10
SETTLE_ROUNDS = 6  # SPA pages load asynchronously: poll innerText until it stops growing
SETTLE_SECONDS = 2.0
SEARCH_URL = "https://duckduckgo.com/html/?q="
# The results page is plain server-rendered HTML; these selectors have been stable for years.
SEARCH_JS = (
    "JSON.stringify(Array.from(document.querySelectorAll('.result')).slice(0,%d).map(r=>({"
    "title:(r.querySelector('.result__a')||{}).textContent||'',"
    "url:(r.querySelector('.result__a')||{}).href||'',"
    "snippet:(r.querySelector('.result__snippet')||{}).textContent||''})))"
)


def _unwrap_redirect(url: str) -> str:
    """DuckDuckGo wraps results as duckduckgo.com/l/?uddg=<real url>; hand the model the real one."""
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.endswith("duckduckgo.com") and parts.path == "/l/":
        real = urllib.parse.parse_qs(parts.query).get("uddg", [""])[0]
        return real or url
    return url


class Browser:
    def __init__(self, base_url: str, settle: float = SETTLE_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.settle = settle

    def _post(self, path: str, payload: dict[str, Any], timeout: float = 90) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - operator-configured http(s) base
            return json.loads(response.read().decode("utf-8") or "{}")

    def _close(self, tab: str) -> None:
        try:
            request = urllib.request.Request(f"{self.base_url}/tabs/{tab}?userId={USER_ID}", method="DELETE")
            urllib.request.urlopen(request, timeout=30).close()  # nosec B310
        except (urllib.error.URLError, OSError):
            pass

    def evaluate(self, url: str, expression: str, settle: bool = True) -> str:
        """Open `url`, wait for the page to settle, run `expression`, return its string result."""
        tab = self._post("/tabs/open", {"userId": USER_ID, "url": url}, timeout=120).get("tabId")
        if not tab:
            raise RuntimeError("browser did not open a tab")
        try:
            if settle:
                last = -1
                for _ in range(SETTLE_ROUNDS):
                    time.sleep(self.settle)
                    length = len(self._eval(tab, "document.body.innerText"))
                    if length > 400 and length <= last + 20:
                        break
                    last = length
            return self._eval(tab, expression)
        finally:
            self._close(tab)

    def _eval(self, tab: str, expression: str) -> str:
        result = self._post(f"/tabs/{tab}/evaluate", {"userId": USER_ID, "expression": expression})
        return str(result.get("result") or "")

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        url = SEARCH_URL + urllib.parse.quote_plus(query)
        raw = self.evaluate(url, SEARCH_JS % limit, settle=False)
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        out = []
        for i in items:
            url = _unwrap_redirect(str(i.get("url") or ""))
            if url.startswith("http"):
                out.append({"title": str(i.get("title") or "").strip(), "url": url, "snippet": str(i.get("snippet") or "").strip()})
        return out

    def open(self, url: str) -> str:
        return self.evaluate(url, "document.body.innerText")


def _make_tools(browser: Browser, approval: bool) -> list[Tool]:
    def gate(ctx: ToolContext, summary: str) -> bool:
        return ctx.approve(summary) if approval else True

    def run_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult("query is required", is_error=True)
        limit = max(1, min(int(args.get("max_results") or 8), MAX_RESULTS))
        if not gate(ctx, f"web_search: {query}"):
            return ToolResult("denied by approval policy", is_error=True)
        try:
            items = browser.search(query, limit)
        except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as error:
            return ToolResult(f"search failed: {error}", is_error=True)
        if not items:
            return ToolResult("no results")
        lines = [f"{n}. {i['title']}\n   {i['url']}\n   {i['snippet']}" for n, i in enumerate(items, 1)]
        return ToolResult("\n".join(lines))

    def run_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return ToolResult("url must start with http:// or https://", is_error=True)
        if not gate(ctx, f"browser_open: {url}"):
            return ToolResult("denied by approval policy", is_error=True)
        try:
            text = browser.open(url).strip()
        except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as error:
            return ToolResult(f"open failed: {error}", is_error=True)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n[truncated]"
        return ToolResult(text or "(empty page)")

    return [
        Tool(
            name="web_search",
            description=(
                "Search the web in a real browser (DuckDuckGo results page) and return "
                "up to 10 results as title, URL and snippet. Follow up with browser_open "
                "to read a result."
            ),
            mutating=approval,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                    "max_results": {"type": "integer", "description": "1-10, default 8"},
                },
                "required": ["query"],
            },
            execute=run_search,
        ),
        Tool(
            name="browser_open",
            description=(
                "Open an http(s) URL in a real browser and return the rendered visible "
                "text (works for JavaScript-rendered pages, unlike webfetch). Output is "
                "truncated to 40k characters."
            ),
            mutating=approval,
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to open"}},
                "required": ["url"],
            },
            execute=run_open,
        ),
    ]


def browser_tools_plugin(base_url: str, approval: bool = True, browser: Browser | None = None) -> Plugin:
    def _apply(ctx: Context, _config: Any) -> None:
        register_tools(ctx, _make_tools(browser or Browser(base_url), approval))

    return Plugin("tool-browser", _apply)
