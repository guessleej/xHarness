"""python_run: run a Python snippet and return what it printed.

For computing, not for guessing: totals, percentages, dates and anything read
from a spreadsheet are worked out by code the person can read, not by the model
(local models get dates and sums wrong). Same guards as bash -- the approval
policy, and the sandbox when one is configured -- plus three of its own:

- secrets are not handed down: environment variables named like keys, tokens,
  secrets and passwords are removed before the child starts;
- a memory ceiling (Linux and macOS, `[tools] python_memory_mb`, default 2048);
- a timeout, and output capped like bash.

The interpreter is this one, or, in the packaged app (whose executable is not a
Python), `[tools] python` or the first python3 / python / py on the PATH. Without
one the tool is not offered.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - running the snippet is this tool's purpose
import sys
import tempfile
from typing import Any

from ..context import Context, Plugin
from ..sandbox import Sandbox
from . import Tool, ToolContext, ToolResult, register_tools

MAX_OUTPUT = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
DEFAULT_MEMORY_MB = 2048
MAX_CODE = 200_000
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?$|^XHARNESS_.*", re.IGNORECASE)


def find_interpreter(configured: str | None = None) -> str | None:
    if configured:
        return shutil.which(configured) or (configured if os.path.isfile(configured) else None)
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"       # a Windows pipe follows the locale otherwise
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _limit(memory_mb: int):
    if os.name != "posix" or memory_mb <= 0:
        return None

    def apply() -> None:
        import resource
        size = memory_mb * 1024 * 1024
        for which in ("RLIMIT_AS", "RLIMIT_DATA"):
            try:
                resource.setrlimit(getattr(resource, which), (size, size))
            except (ValueError, OSError, AttributeError):
                pass    # macOS refuses RLIMIT_AS; RLIMIT_DATA still applies

    return apply


def _run(args: dict[str, Any], ctx: ToolContext, interpreter: str, sandbox: Sandbox | None, memory_mb: int) -> ToolResult:
    code = str(args.get("code") or "")
    if not code.strip():
        return ToolResult("code is required", is_error=True)
    if len(code) > MAX_CODE:
        return ToolResult(f"code is longer than {MAX_CODE} characters; write it to a file and run that with bash", is_error=True)
    preview = code if len(code) <= 600 else code[:600] + f"\n... ({len(code.splitlines())} lines)"
    if not ctx.approve(f"python_run:\n{preview}"):
        return ToolResult("denied by approval policy", is_error=True)
    timeout = min(float(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    workdir = tempfile.mkdtemp(prefix="xh-py-")
    path = os.path.join(workdir, "snippet.py")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(code)
        argv = [interpreter, "-u", path]
        if sandbox is not None:
            argv = sandbox.wrap(argv, ctx.cwd)
        completed = subprocess.run(  # nosec B603 - approved snippet, fixed argv, no shell
            argv, cwd=ctx.cwd, capture_output=True, timeout=timeout, text=True, errors="replace",
            env=child_env(), preexec_fn=_limit(memory_mb),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"[timed out after {timeout:g}s]", is_error=True)
    except OSError as error:
        return ToolResult(f"cannot run python: {error}", is_error=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    parts = [part for part in (completed.stdout, completed.stderr) if part]
    output = "\n--- stderr ---\n".join(parts).replace(path, "<snippet>")
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[truncated]"
    if completed.returncode != 0:
        hint = "\n[hint: out of memory? the limit is %d MB]" % memory_mb if "MemoryError" in output else ""
        return ToolResult(f"{output}\n[exit code {completed.returncode}]{hint}".strip(), is_error=True)
    return ToolResult(output or "(no output: print what you want to see)")


def python_run_tool(interpreter: str, sandbox: Sandbox | None = None, memory_mb: int = DEFAULT_MEMORY_MB) -> Tool:
    confinement = f" Runs under {sandbox.name}." if sandbox else ""
    return Tool(
        name="python_run",
        description=(
            "Run a Python 3 snippet in the working directory and return what it printed (print the results). "
            "Use it for any calculation -- sums, percentages, dates, statistics, reading CSV or Excel -- instead "
            f"of working numbers out yourself. Secrets are not in its environment; memory is capped at {memory_mb} MB."
            f"{confinement}"
        ),
        mutating=True,
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to run"},
                "timeout_seconds": {"type": "number",
                                    "description": f"default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}"},
            },
            "required": ["code"],
        },
        execute=lambda args, tool_ctx: _run(args, tool_ctx, interpreter, sandbox, memory_mb),
    )


def python_run_plugin(configured: str | None = None, memory_mb: int = DEFAULT_MEMORY_MB) -> Plugin:
    def _apply(ctx: Context, _config: Any) -> None:
        interpreter = find_interpreter(configured)
        if interpreter:
            register_tools(ctx, [python_run_tool(interpreter, ctx.optional("sandbox"), memory_mb)])

    return Plugin("tool-python", _apply)
