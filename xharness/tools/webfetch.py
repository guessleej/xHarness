"""The webfetch tool: fetch a URL and return its readable text.

Disabled by default: enabling it is the only way xHarness talks to anything
besides the model endpoint, so it is an explicit opt-in (`[tools] webfetch =
true`) and every fetch goes through the approval policy.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

MAX_BYTES = 2_000_000
MAX_OUTPUT = 50_000
TIMEOUT_SECONDS = 30


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def _to_text(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    text = body.decode(charset, errors="replace")
    if "html" not in content_type:
        return text
    extractor = _TextExtractor()
    extractor.feed(text)
    return "\n".join(extractor.parts)


def _run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = str(args.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return ToolResult("url must start with http:// or https://", is_error=True)
    if not ctx.approve(f"webfetch: {url}"):
        return ToolResult("denied by approval policy", is_error=True)
    request = urllib.request.Request(url, headers={"User-Agent": "xharness-webfetch"})  # noqa: S310 - scheme checked above
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310  # nosec B310
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_BYTES)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return ToolResult(f"fetch failed: {error}", is_error=True)
    output = _to_text(body, content_type).strip()
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[truncated]"
    return ToolResult(output or "(empty response)")


def webfetch_tool() -> Tool:
    return Tool(
        name="webfetch",
        description=(
            "Fetch an http(s) URL and return its readable text (HTML is reduced "
            "to visible text). Output is truncated to 50k characters."
        ),
        mutating=True,  # network egress goes through the approval policy
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The URL to fetch"}},
            "required": ["url"],
        },
        execute=_run,
    )


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [webfetch_tool()])


webfetch_tool_plugin = Plugin("tool-webfetch", _apply)
