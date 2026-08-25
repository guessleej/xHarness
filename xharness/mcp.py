"""MCP client: connect stdio servers and expose their tools in the registry.

Speaks the Model Context Protocol as newline-delimited JSON-RPC 2.0 over a
child process's stdio. Only the tools surface is used (initialize, tools/list,
tools/call); resources and prompts are out of scope. A server that fails to
start is skipped with a warning so the harness stays usable.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess  # nosec B404
import sys
import threading
from typing import Any

from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

PROTOCOL_VERSION = "2025-06-18"
MAX_OUTPUT = 50_000
DEFAULT_TIMEOUT_SECONDS = 60


class McpError(RuntimeError):
    pass


class McpClient:
    """One stdio MCP server: spawn, handshake, list tools, call tools."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self._argv = [command] + list(args or [])
        self._env = {**os.environ, **(env or {})}
        self._cwd = cwd
        self._timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0

    def start(self) -> None:
        # The command comes from the user's own config file, same trust level
        # as the model endpoint; there is nothing to sanitize against.
        self._proc = subprocess.Popen(  # nosec B603
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
            cwd=self._cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "xharness", "version": _version()},
            },
        )
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise McpError(f"{self.name}: tools/list returned no tool array")
        return tools

    def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        parts: list[str] = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        output = "\n".join(parts) or "(no content)"
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n[truncated]"
        return ToolResult(output, is_error=bool(result.get("isError")))

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(f"{self.name}: server not running")
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise McpError(f"{self.name}: server pipe closed: {error}") from error

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=self._timeout)
            except queue.Empty:
                raise McpError(f"{self.name}: timed out waiting for {method}") from None
            if line is None:
                raise McpError(f"{self.name}: server exited during {method}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON-RPC (a stray log line); keep waiting
            if message.get("id") != request_id:
                continue  # notification or unrelated message
            if "error" in message:
                error = message["error"]
                raise McpError(f"{self.name}: {error.get('message', error)}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}


def _version() -> str:
    from . import __version__

    return __version__


def _bridge_tool(client: McpClient, spec: dict[str, Any]) -> Tool:
    remote_name = str(spec["name"])
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{client.name}__{remote_name}")
    annotations = spec.get("annotations") or {}
    # Unknown side effects are side effects: only an explicit readOnlyHint
    # exempts an MCP tool from the approval policy.
    mutating = not bool(annotations.get("readOnlyHint"))

    def execute(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if mutating and not ctx.approve(f"mcp {client.name}.{remote_name}: {json.dumps(args, ensure_ascii=False)[:200]}"):
            return ToolResult("denied by approval policy", is_error=True)
        try:
            return client.call(remote_name, args)
        except McpError as error:
            return ToolResult(str(error), is_error=True)

    return Tool(
        name=safe,
        description=f"[MCP {client.name}] {spec.get('description', remote_name)}",
        mutating=mutating,
        parameters=spec.get("inputSchema") or {"type": "object", "properties": {}},
        execute=execute,
    )


def mcp_plugin(servers: dict[str, dict[str, Any]]) -> Plugin:
    def _apply(ctx: Context, _config: Any) -> None:
        for name, spec in servers.items():
            if spec.get("disabled"):
                continue
            client = McpClient(
                name,
                str(spec["command"]),
                args=[str(a) for a in spec.get("args", [])],
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                cwd=spec.get("cwd"),
                timeout=float(spec.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            )
            try:
                client.start()
                tools = [_bridge_tool(client, tool_spec) for tool_spec in client.list_tools()]
            except (McpError, OSError) as error:
                client.close()
                print(f"[xharness] mcp server {name!r} skipped: {error}", file=sys.stderr)
                continue
            ctx.on_dispose(client.close)
            register_tools(ctx, tools)
            ctx.emit("mcp/connected", {"server": name, "tools": len(tools)})

    return Plugin("mcp", _apply)
