"""Hooks: run your own commands at points in the agent's life, using the
protocol Claude Code defines, so existing hook scripts work unchanged.

Events and what a hook can do:
  SessionStart       add context to the first request
  UserPromptSubmit   add context to a request, or block it
  PreToolUse         allow (skip the approval prompt), ask (force it), deny/block a tool call
  PostToolUse        add a note the model sees after a tool's result
  Stop               keep the agent going with a further instruction (at most 3 times a task)
  PreCompact         veto a compaction

A hook receives the event as JSON on stdin (session_id, transcript_path, cwd,
hook_event_name, plus tool_name/tool_input/tool_response or prompt). Exit 0
with JSON on stdout uses Claude Code's fields (decision, reason, continue,
hookSpecificOutput.permissionDecision, additionalContext); plain stdout from
SessionStart and UserPromptSubmit becomes context. Exit 2 blocks, with stderr
as the reason the model is given. Any other exit is a logged, non-blocking error.

Codex's `notify` is supported too: after every task the program gets one JSON
argument describing the turn.

Where hooks come from: only the operator's configuration -- [[hooks.<Event>]]
tables and Claude Code settings files listed in [hooks] files. A repository
the agent works on can never register a hook: that would let any checked-out
project run commands on the server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - running the operator's hook commands is this module's purpose
import threading
from dataclasses import dataclass, field
from typing import Any

from .context import Context, Plugin

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "PreCompact")
DEFAULT_TIMEOUT = 60
MAX_OUTPUT = 20_000
MAX_STOP_CONTINUES = 3
#: Claude Code's names for the same tools, so a matcher written for it still matches.
CLAUDE_NAMES = {"bash": "Bash", "terminal": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
                "glob": "Glob", "grep": "Grep", "webfetch": "WebFetch", "web_search": "WebSearch",
                "subagent": "Task", "subagent_batch": "Task", "todo_write": "TodoWrite"}


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: float = DEFAULT_TIMEOUT
    source: str = "config"

    def matches(self, tool: str | None) -> bool:
        if not self.matcher or self.matcher == "*" or tool is None:
            return True
        names = [tool, CLAUDE_NAMES.get(tool, "")]
        try:
            return any(name and re.fullmatch(self.matcher, name) for name in names)
        except re.error:
            return any(name == self.matcher for name in names)


@dataclass
class HookOutcome:
    block: bool = False
    reason: str = ""
    context: list[str] = field(default_factory=list)
    permission: str = ""        # "", "allow", "deny", "ask"
    ran: int = 0


def load_hooks(settings: dict[str, Any]) -> list[Hook]:
    hooks: list[Hook] = []
    default_timeout = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT))
    for event in EVENTS:
        for entry in settings.get(event, []) or []:
            if isinstance(entry, dict) and entry.get("command"):
                hooks.append(Hook(event, str(entry["command"]), str(entry.get("matcher") or ""),
                                  float(entry.get("timeout") or default_timeout)))
    for path in settings.get("files", []) or []:
        full = os.path.expanduser(str(path))
        try:
            with open(full, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"[hooks] cannot read {full}: {error}") from error
        for event, groups in (data.get("hooks") or {}).items():
            if event not in EVENTS:
                continue
            for group in groups or []:
                for item in group.get("hooks") or []:
                    if item.get("type", "command") == "command" and item.get("command"):
                        hooks.append(Hook(event, str(item["command"]), str(group.get("matcher") or ""),
                                          float(item.get("timeout") or default_timeout), source=full))
    return hooks


class HookRunner:
    def __init__(self, hooks: list[Hook], notify: list[str] | None = None) -> None:
        self.hooks = hooks
        self.notify_argv = [str(part) for part in notify or []]

    def __bool__(self) -> bool:
        return bool(self.hooks or self.notify_argv)

    def run(self, event: str, payload: dict[str, Any], cwd: str, tool: str | None = None,
            log: Any = None) -> HookOutcome:
        outcome = HookOutcome()
        for hook in self.hooks:
            if hook.event != event or not hook.matches(tool):
                continue
            outcome.ran += 1
            data = {"hook_event_name": event, "cwd": cwd, **payload}
            try:
                # The operator's own command, from the operator's own config (see module docstring).
                done = subprocess.run(  # nosec B602 - shell syntax is part of the hook protocol
                    hook.command, shell=True, input=json.dumps(data, ensure_ascii=False), capture_output=True,
                    text=True, errors="replace", timeout=hook.timeout, cwd=cwd if os.path.isdir(cwd) else None,
                )
                code, out, err = done.returncode, done.stdout[:MAX_OUTPUT], done.stderr[:MAX_OUTPUT]
            except subprocess.TimeoutExpired:
                code, out, err = -1, "", f"hook timed out after {hook.timeout:g}s"
            except OSError as error:
                code, out, err = -1, "", f"hook could not run: {error}"
            if log is not None:
                log.append({"type": "hook", "event": event, "tool": tool, "command": hook.command[:200],
                            "exit": code, "stderr": err[:500]})
            if code == 2:
                outcome.block, outcome.reason = True, err.strip() or "blocked by a hook"
                break
            if code != 0:
                continue   # a broken hook is logged, never fatal
            self._read(out, event, outcome)
            if outcome.block or outcome.permission == "deny":
                break
        return outcome

    @staticmethod
    def _read(out: str, event: str, outcome: HookOutcome) -> None:
        text = out.strip()
        if text.startswith("{"):
            try:
                reply = json.loads(text)
            except json.JSONDecodeError:
                reply = None
            if isinstance(reply, dict):
                specific = reply.get("hookSpecificOutput") or {}
                if reply.get("continue") is False:
                    outcome.block, outcome.reason = True, str(reply.get("stopReason") or reply.get("reason") or "stopped by a hook")
                if reply.get("decision") == "block":
                    outcome.block, outcome.reason = True, str(reply.get("reason") or "blocked by a hook")
                decision = specific.get("permissionDecision") or {"approve": "allow"}.get(str(reply.get("decision")), "")
                if decision in ("allow", "deny", "ask"):
                    outcome.permission = decision
                    if decision == "deny":
                        outcome.reason = str(specific.get("permissionDecisionReason") or reply.get("reason") or "denied by a hook")
                if specific.get("additionalContext"):
                    outcome.context.append(str(specific["additionalContext"]))
                return
        if text and event in ("SessionStart", "UserPromptSubmit"):
            outcome.context.append(text)

    def notify(self, payload: dict[str, Any]) -> None:
        """Codex-style: the program gets the turn as one JSON argument; fire and forget."""
        if not self.notify_argv:
            return

        def call() -> None:
            try:
                subprocess.run(self.notify_argv + [json.dumps(payload, ensure_ascii=False)],  # nosec B603 - operator's program
                               capture_output=True, timeout=DEFAULT_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                pass

        threading.Thread(target=call, name="xharness-notify", daemon=True).start()


def hooks_plugin(settings: dict[str, Any] | None) -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        runner = HookRunner(load_hooks(settings or {}), (settings or {}).get("notify"))
        ctx.provide("hooks", runner)

    return Plugin("hooks", apply)
