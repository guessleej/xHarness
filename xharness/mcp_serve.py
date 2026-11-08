"""`xharness mcp-serve`: offer xHarness's own tools to an MCP client (Claude Code,
an editor, another agent) over stdio.

What is offered: the tools that do things on this machine and that a coding
assistant lacks -- the browser, python_run, the desktop -- not the agent's own
machinery (subagents, memory, todo). `[mcp_serve] tools = [...]` replaces the
default list; only tools this config actually mounts are listed.

Approvals: an MCP client puts every tool call to its person before sending it
(Claude Code does), so an ordinary call runs. What xHarness asks for even under
approval mode "auto" -- a browser click on "送出", the desktop tools -- is put to
the person again through MCP elicitation when the client says it supports it,
and refused when it does not. Nothing in the protocol lets the client approve
those for the person.

Stdout belongs to the protocol: anything a tool prints goes to stderr. Every
call is logged to stderr as one line (tool, ok or error, seconds), without its
arguments or output.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from typing import IO, Any

from .config import ResolvedConfig
from .tools import Tool, ToolContext, ToolResult

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TOOLS = (
    "web_search", "browser_open", "browser_view", "browser_click", "browser_type", "browser_press",
    "browser_scroll", "browser_tabs", "python_run", "desktop_look", "desktop_act", "webfetch",
)


def _version() -> str:
    from . import __version__
    return __version__


class McpServer:
    def __init__(self, tools: list[Tool], cwd: str, stdin: IO[str], stdout: IO[str], log: IO[str] = sys.stderr) -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.cwd = cwd
        self.stdin = stdin
        self.stdout = stdout
        self.log = log
        self.elicitation = False
        self.pending: deque[dict[str, Any]] = deque()
        self.next_id = 0

    # -- wire --------------------------------------------------------------
    def _send(self, message: dict[str, Any]) -> None:
        self.stdout.write(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n")
        self.stdout.flush()

    def _read(self) -> dict[str, Any] | None:
        while True:
            line = self.stdin.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(message, dict):
                return message

    def _next(self) -> dict[str, Any] | None:
        return self.pending.popleft() if self.pending else self._read()

    # -- asking the person -------------------------------------------------
    def _elicit(self, summary: str) -> bool:
        if not self.elicitation:
            return False
        self.next_id += 1
        ask_id = f"xh-{self.next_id}"
        self._send({"id": ask_id, "method": "elicitation/create", "params": {
            "message": "xHarness 要先問你：\n" + summary,
            "requestedSchema": {"type": "object", "properties": {
                "allow": {"type": "boolean", "title": "允許這個動作", "description": "勾選才會執行"}},
                "required": ["allow"]},
        }})
        while True:
            message = self._read()
            if message is None:
                return False
            if message.get("id") == ask_id and "method" not in message:
                result = message.get("result") or {}
                return result.get("action") == "accept" and (result.get("content") or {}).get("allow") is True
            self.pending.append(message)      # handled after this call

    # -- methods -----------------------------------------------------------
    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        tool = self.tools.get(name)
        if tool is None:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return {"content": [{"type": "text", "text": "arguments must be an object"}], "isError": True}

        def approve(summary: str) -> bool:
            # The client already put this call to its person; the desktop tools ask on every action.
            return self._elicit(summary) if tool.always_ask else True

        ctx = ToolContext(cwd=self.cwd, approve=approve, agent="mcp", confirm=self._elicit)
        started = time.time()
        real_stdout, sys.stdout = sys.stdout, sys.stderr    # a stray print must not corrupt the protocol
        try:
            result = tool.execute(args, ctx)
        except Exception as error:  # noqa: BLE001 - a broken tool must not end the server
            result = ToolResult(f"tool failed: {error}", is_error=True)
        finally:
            sys.stdout = real_stdout
        self.log.write(f"[xharness mcp] {name} {'error' if result.is_error else 'ok'} {time.time() - started:.1f}s\n")
        self.log.flush()
        return {"content": [{"type": "text", "text": result.output}], "isError": result.is_error}

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if method is None:
            return                              # a stray response
        if method == "initialize":
            self.elicitation = "elicitation" in ((params.get("capabilities") or {}))
            result: Any = {"protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "xharness", "version": _version()}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.parameters}
                                for t in self.tools.values()]}
        elif method == "tools/call":
            result = self._call(params)
        elif msg_id is None:
            return                              # notifications/initialized and the like
        else:
            self._send({"id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}})
            return
        if msg_id is not None:
            self._send({"id": msg_id, "result": result})

    def serve(self) -> int:
        while True:
            message = self._next()
            if message is None:
                return 0
            self.handle(message)


def offered_tools(registry: Any, names: list[str] | tuple[str, ...]) -> list[Tool]:
    return [tool for name in names if (tool := registry.get(name)) is not None]


def run_mcp_serve(config: ResolvedConfig) -> int:
    from .presets import build_harness

    real_stdout, sys.stdout = sys.stdout, sys.stderr     # plugins that print while mounting
    try:
        harness = build_harness(config, no_session=True)
    finally:
        sys.stdout = real_stdout
    names = (getattr(config, "mcp_serve", None) or {}).get("tools") or DEFAULT_TOOLS
    tools = offered_tools(harness.ctx.get("tools"), list(names))
    sys.stderr.write(f"[xharness mcp] serving {len(tools)} tools: {', '.join(t.name for t in tools) or 'none'}\n")
    return McpServer(tools, os.getcwd(), sys.stdin, sys.stdout).serve()
