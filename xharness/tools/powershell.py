"""PowerShell, where it is installed: a one-off `powershell` tool like bash, and
a PowerShell shell that stays open for the `terminal` tool (shell = "powershell").

Both are offered only when `pwsh` is on the PATH (PowerShell 7, which runs on
Windows, macOS and Linux; on Windows the built-in `powershell.exe` is used as a
fallback). Nothing is registered otherwise, so the model is never offered a
tool that cannot run.

The open shell is a small script that waits for a command file, runs it in its
own scope (variables, the directory and imported modules carry over), prints a
marker line with the exit status and the directory, and waits again. Commands
come through a file rather than the shell's standard input, so a command that
reads input gets an empty stream and cannot swallow the next command.
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess  # nosec B404 - a shell is this module's purpose
import tempfile
import threading
import time
import uuid
from typing import Any

from ..context import Context, Plugin
from ..sandbox import Sandbox
from . import Tool, ToolContext, ToolResult, register_tools
from .terminal import DEFAULT_TIMEOUT_SECONDS, MAX_OUTPUT_CHARS, MAX_TIMEOUT_SECONDS

LOOP = r'''
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$__file = $args[0]
while ($true) {
    while (-not (Test-Path -LiteralPath $__file)) { Start-Sleep -Milliseconds 30 }
    $__text = [System.IO.File]::ReadAllText($__file)
    Remove-Item -LiteralPath $__file -Force
    $__marker, $__b64 = $__text.Trim().Split(' ', 2)
    $__cmd = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($__b64))
    $global:LASTEXITCODE = $null
    $__errors = $Error.Count
    try {
        . ([scriptblock]::Create($__cmd)) *>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { "Error: " + $_.Exception.Message } else { $_ } } |
            Out-String -Stream | ForEach-Object { [Console]::Out.WriteLine($_) }
    } catch { [Console]::Out.WriteLine("Error: " + $_.Exception.Message) }
    $__code = if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) { $global:LASTEXITCODE } elseif ($Error.Count -gt $__errors) { 1 } else { 0 }
    [Console]::Out.WriteLine("")
    [Console]::Out.WriteLine("$__marker $__code $($PWD.Path)")
    [Console]::Out.Flush()
}
'''


def find_pwsh() -> str | None:
    found = shutil.which("pwsh")
    if found is None and os.name == "nt":
        found = shutil.which("powershell")
    return found


class PwshShell:
    """Same surface as terminal.Shell: run(command, timeout) -> (output, status, timed_out)."""

    def __init__(self, exe: str, cwd: str, sandbox: Sandbox | None = None) -> None:
        self.exe, self.cwd, self.sandbox = exe, cwd, sandbox
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self._dir: str | None = None
        self._lines: Any = None

    def _start(self) -> None:
        import queue

        self._dir = tempfile.mkdtemp(prefix="xh-ps-", dir="/tmp" if os.path.isdir("/tmp") else None)  # nosec B108
        loop = os.path.join(self._dir, "loop.ps1")
        with open(loop, "w", encoding="utf-8") as handle:
            handle.write(LOOP)
        self._file = os.path.join(self._dir, "command")
        argv = [self.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", loop, self._file]
        if self.sandbox is not None:
            argv = self.sandbox.wrap(argv, self.cwd)
        self._lines = queue.Queue()
        self.process = subprocess.Popen(  # nosec B603 - commands are approved; sandboxed when available
            argv, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=(os.name != "nt"))
        threading.Thread(target=self._pump, args=(self.process, self._lines), daemon=True).start()

    @staticmethod
    def _pump(process: subprocess.Popen, lines: Any) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                lines.put(line)
        lines.put(None)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        if self.process is not None:
            try:
                if os.name == "nt":
                    self.process.kill()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self.process = None
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def run(self, command: str, timeout: float) -> tuple[str, int | None, bool]:
        import queue

        with self.lock:
            if not self.alive():
                self.close()
                self._start()
            marker = f"__XH_END_{uuid.uuid4().hex}__"
            payload = f"{marker} {base64.b64encode(command.encode('utf-8')).decode('ascii')}"
            staged = self._file + ".part"
            with open(staged, "w", encoding="ascii") as handle:
                handle.write(payload)
            os.replace(staged, self._file)     # the loop only ever sees a whole command
            out: list[str] = []
            size = 0
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    return "".join(out), None, True
                try:
                    line = self._lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    self.close()
                    return "".join(out), None, False
                if line.startswith(marker):
                    parts = line.rstrip("\r\n").split(" ", 2)
                    status = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
                    if len(parts) > 2 and os.path.isdir(parts[2]):
                        self.cwd = parts[2]
                    text = "".join(out).rstrip("\r\n")
                    return text, status, False
                if size < MAX_OUTPUT_CHARS:
                    out.append(line)
                    size += len(line)


def _run_once(exe: str, args: dict[str, Any], ctx: ToolContext, sandbox: Sandbox | None) -> ToolResult:
    command = str(args.get("command") or "")
    if not command:
        return ToolResult("command is required", is_error=True)
    if not ctx.approve(f"powershell: {command}"):
        return ToolResult("denied by approval policy", is_error=True)
    timeout = min(float(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    argv = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if sandbox is not None:
        argv = sandbox.wrap(argv, ctx.cwd)
    try:
        done = subprocess.run(  # nosec B603 - approved; sandboxed when available
            argv, cwd=ctx.cwd, capture_output=True, timeout=timeout, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ToolResult(f"[timed out after {timeout:g}s]", is_error=True)
    except OSError as error:
        return ToolResult(f"cannot run PowerShell: {error}", is_error=True)
    output = "\n--- stderr ---\n".join(part for part in (done.stdout, done.stderr) if part)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    if done.returncode != 0:
        return ToolResult(f"{output}\n[exit code {done.returncode}]".strip(), is_error=True)
    return ToolResult(output or "(no output)")


def powershell_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        exe = find_pwsh()
        if exe is None:
            return
        sandbox = ctx.optional("sandbox")
        confinement = f" Commands run under {sandbox.name}." if sandbox else ""
        register_tools(ctx, [Tool(
            name="powershell",
            description=("Run a PowerShell command (pwsh -Command) in the working directory and return its output. "
                         "Each call is a fresh process; for state that carries over use the terminal tool with "
                         f"shell \"powershell\".{confinement}"),
            mutating=True,
            parameters={"type": "object", "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "description": f"default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}"}},
                "required": ["command"]},
            execute=lambda args, tool_ctx: _run_once(exe, args, tool_ctx, sandbox),
        )])

    return Plugin("tool-powershell", apply)
