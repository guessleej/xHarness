"""The bash tool: bounded shell execution in the working directory."""

from __future__ import annotations

import subprocess  # nosec B404
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

MAX_OUTPUT = 50_000
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600


def _run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "")
    if not command:
        return ToolResult("command is required", is_error=True)
    if not ctx.approve(f"bash: {command}"):
        return ToolResult("denied by approval policy", is_error=True)
    timeout = min(float(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    try:
        # Arbitrary commands are this tool's purpose; the guard is the approval
        # policy above, and the residual risk (no sandbox) is in docs/ssdlc.md.
        completed = subprocess.run(  # nosec B603 B607
            ["bash", "-c", command],
            cwd=ctx.cwd,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"[timed out after {timeout:g}s]", is_error=True)
    except OSError as error:
        return ToolResult(f"cannot run bash: {error}", is_error=True)
    parts = [part for part in (completed.stdout, completed.stderr) if part]
    output = "\n--- stderr ---\n".join(parts)
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[truncated]"
    if completed.returncode != 0:
        return ToolResult(f"{output}\n[exit code {completed.returncode}]".strip(), is_error=True)
    return ToolResult(output or "(no output)")


def bash_tool() -> Tool:
    return Tool(
        name="bash",
        description=(
            "Run a shell command with bash -c in the working directory and return "
            "stdout and stderr. Use for builds, tests, git, and anything the "
            "filesystem tools do not cover."
        ),
        mutating=True,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run"},
                "timeout_seconds": {
                    "type": "number",
                    "description": f"Timeout in seconds; default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}",
                },
            },
            "required": ["command"],
        },
        execute=_run,
    )


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [bash_tool()])


bash_tool_plugin = Plugin("tool-bash", _apply)
