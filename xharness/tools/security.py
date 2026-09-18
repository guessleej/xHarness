"""The security_scan tool: run the same scanners the project's own CI uses
(bandit SAST, pip-audit dependency audit, gitleaks secret scan) against a
target directory, from inside an agent task.

Read-only: scanners inspect files and report; nothing is modified. Scanners
that are not installed are reported as skipped rather than failing the tool.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

SCAN_TIMEOUT_SECONDS = 180
MAX_SCANNER_OUTPUT = 8_000


def _scanner_commands(path: str) -> dict[str, tuple[list[str], str]]:
    return {
        "bandit": (["bandit", "-r", path, "-q"], "Python SAST"),
        "pip-audit": (["pip-audit", "--skip-editable"], "dependency vulnerability audit"),
        "gitleaks": (
            ["gitleaks", "detect", "--source", path, "--no-banner", "--redact"],
            "secret scan",
        ),
    }


def _run_scanner(command: list[str], cwd: str) -> tuple[str, str]:
    """Return (status, output). Non-zero exit means findings for all three tools."""
    try:
        completed = subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            capture_output=True,
            timeout=SCAN_TIMEOUT_SECONDS,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "failed", f"[timed out after {SCAN_TIMEOUT_SECONDS}s]"
    except OSError as error:
        return "failed", f"[cannot run: {error}]"
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if len(output) > MAX_SCANNER_OUTPUT:
        output = output[:MAX_SCANNER_OUTPUT] + "\n[truncated]"
    if completed.returncode == 0:
        return "clean", output
    return "findings", output


def _scan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    raw_path = str(args.get("path") or ".")
    path = raw_path if os.path.isabs(raw_path) else os.path.join(ctx.cwd, raw_path)
    if not os.path.isdir(path):
        return ToolResult(f"not a directory: {path}", is_error=True)
    requested = args.get("scanners")
    commands = _scanner_commands(path)
    if requested:
        unknown = [name for name in requested if name not in commands]
        if unknown:
            return ToolResult(
                f"unknown scanners: {', '.join(unknown)} (available: {', '.join(commands)})",
                is_error=True,
            )
        commands = {name: commands[name] for name in requested}

    sections: list[str] = []
    ran = 0
    for name, (command, description) in commands.items():
        if shutil.which(command[0]) is None:
            sections.append(f"== {name} ({description}) == skipped (not installed)")
            continue
        status, output = _run_scanner(command, ctx.cwd)
        ran += 1
        header = f"== {name} ({description}) == {status}"
        sections.append(f"{header}\n{output}" if output else header)

    if not ran:
        return ToolResult(
            "no scanners available. Install bandit, pip-audit, and/or gitleaks first "
            "(e.g. pip install bandit pip-audit).\n" + "\n".join(sections),
            is_error=True,
        )
    return ToolResult("\n\n".join(sections))


def security_scan_tool() -> Tool:
    return Tool(
        name="security_scan",
        description=(
            "Run security scanners against a directory: bandit (Python SAST), "
            "pip-audit (dependency vulnerabilities), gitleaks (secrets). "
            "Read-only; scanners not installed are reported as skipped."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to scan; defaults to cwd"},
                "scanners": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["bandit", "pip-audit", "gitleaks"]},
                    "description": "Subset of scanners to run; omit for all available",
                },
            },
        },
        execute=_scan,
    )


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [security_scan_tool()])


security_tool_plugin = Plugin("tool-security", _apply)
