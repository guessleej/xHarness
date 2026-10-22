"""A terminal that stays open: the working directory, exported variables and
activated environments carry over from one command to the next.

bash runs every command in a fresh shell, so `cd src` followed by `ls` lists
the wrong directory and `source .venv/bin/activate` is forgotten at once.
The terminal tool keeps one bash per agent for the whole conversation.

Each command is followed by a marker line carrying its exit status and the
shell's directory, which is how the end of a command is found. Commands read
from /dev/null, so one that waits for keyboard input cannot swallow the
marker. A command that runs past its timeout takes the shell with it: the
shell is restarted in the last known directory and the model is told that
variables were reset. The shell runs inside the same sandbox as bash.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess  # nosec B404 - a shell is this module's purpose
import threading
import time
import uuid
from typing import Any

from ..context import Context, Plugin
from ..sandbox import Sandbox
from . import Tool, ToolContext, ToolResult, register_tools

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_CHARS = 1_000_000


class Shell:
    def __init__(self, cwd: str, sandbox: Sandbox | None = None) -> None:
        self.cwd = cwd
        self.sandbox = sandbox
        self.process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self.lock = threading.Lock()

    def _start(self) -> None:
        argv = ["bash", "--noprofile", "--norc"]
        if self.sandbox is not None:
            argv = self.sandbox.wrap(argv, self.cwd)
        self._lines = queue.Queue()
        self.process = subprocess.Popen(  # nosec B603 - commands are approved; sandboxed when available
            argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, start_new_session=True,
            env={**os.environ, "PS1": "", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"},
        )
        threading.Thread(target=self._pump, args=(self.process, self._lines), daemon=True).start()

    @staticmethod
    def _pump(process: subprocess.Popen, lines: queue.Queue) -> None:
        if process.stdout is None:
            lines.put(None)
            return
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        self.process = None

    def run(self, command: str, timeout: float) -> tuple[str, int | None, bool]:
        """(output, exit status, timed_out)."""
        with self.lock:
            if not self.alive():
                self._start()
            if self.process is None or self.process.stdin is None:
                return "the terminal could not be started", None, False
            marker = f"__XH_END_{uuid.uuid4().hex}__"
            script = (f"{{\n{command}\n}} </dev/null\n"
                      f"printf '\\n%s %s %s\\n' '{marker}' \"$?\" \"$PWD\"\n")
            try:
                self.process.stdin.write(script)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self.close()
                return "the terminal had exited; it will start fresh on the next command", None, False
            out: list[str] = []
            size = 0
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()   # the next command starts a fresh shell in self.cwd
                    return "".join(out), None, True
                try:
                    line = self._lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:   # the shell itself exited (e.g. `exit`)
                    self.close()
                    return "".join(out), None, False
                if line.startswith(marker):
                    parts = line.rstrip("\n").split(" ", 2)
                    status = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
                    if len(parts) > 2 and os.path.isdir(parts[2]):
                        self.cwd = parts[2]
                    text = "".join(out)
                    return (text[:-1] if text.endswith("\n") else text), status, False
                if size < MAX_OUTPUT_CHARS:
                    out.append(line)
                    size += len(line)


class Terminals:
    """One shell per agent (the main agent and each subagent get their own)."""

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        self.sandbox = sandbox
        self.shells: dict[str, Shell] = {}
        self._lock = threading.Lock()

    def get(self, agent: str, cwd: str, kind: str = "bash") -> Any:
        with self._lock:
            key = agent if kind == "bash" else f"{agent}:{kind}"
            shell = self.shells.get(key)
            if shell is None:
                if kind == "powershell":
                    from .powershell import PwshShell, find_pwsh

                    exe = find_pwsh()
                    if exe is None:
                        raise OSError("PowerShell (pwsh) is not installed here")
                    shell = PwshShell(exe, cwd, self.sandbox)
                else:
                    shell = Shell(cwd, self.sandbox)
                self.shells[key] = shell
            return shell

    def reset(self, agent: str) -> None:
        """Close every shell this agent has open (bash and PowerShell)."""
        with self._lock:
            keys = [k for k in self.shells if k == agent or k.startswith(agent + ":")]
            doomed = [self.shells.pop(k) for k in keys]
        for shell in doomed:
            shell.close()

    def shutdown(self) -> None:
        with self._lock:
            doomed, self.shells = list(self.shells.values()), {}
        for shell in doomed:
            shell.close()


def terminal_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        terminals = Terminals(ctx.optional("sandbox"))
        ctx.provide("terminals", terminals)
        ctx.on_dispose(terminals.shutdown)

        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            agent = tool_ctx.agent or "main"
            if args.get("reset"):
                terminals.reset(agent)
                if not str(args.get("command") or "").strip():
                    return ToolResult("terminal reset: the next command starts a fresh shell in the working directory")
            command = str(args.get("command") or "").strip()
            if not command:
                return ToolResult("command is required", is_error=True)
            kind = str(args.get("shell") or "bash").lower()
            if kind in ("pwsh", "ps"):
                kind = "powershell"
            if kind not in ("bash", "powershell"):
                return ToolResult("shell must be \"bash\" or \"powershell\"", is_error=True)
            if not tool_ctx.approve(f"terminal ({kind}): {command}"):
                return ToolResult("denied by approval policy", is_error=True)
            timeout = min(float(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
            try:
                shell = terminals.get(agent, tool_ctx.cwd, kind)
                output, status, timed_out = shell.run(command, timeout)
            except OSError as error:
                return ToolResult(f"cannot start the terminal: {error}", is_error=True)
            where = f"(cwd: {shell.cwd})"
            if timed_out:
                return ToolResult(f"{output}\n[timed out after {timeout:g}s; the terminal was restarted in {shell.cwd}, "
                                  "so variables and activated environments are gone]".strip(), is_error=True)
            if status not in (0, None):
                return ToolResult(f"{output}\n[exit code {status}] {where}".strip(), is_error=True)
            return ToolResult(f"{output}\n{where}".strip() if output else f"(no output) {where}")

        from .powershell import find_pwsh

        has_pwsh = find_pwsh() is not None      # offered only where it can run
        powershell_hint = ' shell "powershell" opens a PowerShell instead of bash.' if has_pwsh else ""
        sandboxed = ctx.optional("sandbox")
        confinement = f" It runs under {sandboxed.name}." if sandboxed else ""
        register_tools(ctx, [Tool(
            name="terminal",
            description=("Run a command in a shell that stays open for this conversation: the directory, "
                         "exported variables and activated environments (cd, export, source .venv/bin/activate) "
                         "carry over to the next call. Commands cannot read keyboard input. Use reset to start "
                         f"over.{powershell_hint}{confinement}"),
            parameters={"type": "object", "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "description": f"default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}"},
                "reset": {"type": "boolean", "description": "Close the shell first and start a fresh one"},
                **({"shell": {"type": "string", "enum": ["bash", "powershell"], "description": "bash (default) or powershell"}}
                   if has_pwsh else {}),
            }},
            execute=run,
            mutating=True,
        )])

    return Plugin("terminal", apply)
