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

    def get(self, agent: str, cwd: str) -> Shell:
        with self._lock:
            shell = self.shells.get(agent)
            if shell is None:
                shell = self.shells[agent] = Shell(cwd, self.sandbox)
            return shell

    def reset(self, agent: str) -> None:
        with self._lock:
            shell = self.shells.pop(agent, None)
        if shell:
            shell.close()

    def shutdown(self) -> None:
        for name in list(self.shells):
            self.reset(name)


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
            if not tool_ctx.approve(f"terminal: {command}"):
                return ToolResult("denied by approval policy", is_error=True)
            timeout = min(float(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
            shell = terminals.get(agent, tool_ctx.cwd)
            try:
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

        sandboxed = ctx.optional("sandbox")
        confinement = f" It runs under {sandboxed.name}." if sandboxed else ""
        register_tools(ctx, [Tool(
            name="terminal",
            description=("Run a command in a shell that stays open for this conversation: the directory, "
                         "exported variables and activated environments (cd, export, source .venv/bin/activate) "
                         "carry over to the next call. Commands cannot read keyboard input. Use reset to start "
                         f"over.{confinement}"),
            parameters={"type": "object", "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "description": f"default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}"},
                "reset": {"type": "boolean", "description": "Close the shell first and start a fresh one"},
            }},
            execute=run,
            mutating=True,
        )])

    return Plugin("terminal", apply)
