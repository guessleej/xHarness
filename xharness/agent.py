"""The default agent loop: call the model, execute tool calls, feed results
back, and stop when the model answers without tool calls or max_turns hits.
Model calls go through the interceptable "llm/stream" seam.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Context
from .llm import AssistantTurn, Usage
from .session import SessionLog
from .tools import ToolContext, ToolRegistry, ToolResult

DEFAULT_MAX_TURNS = 40
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTIONS_CHARS = 24_000


def load_project_instructions(cwd: str) -> str | None:
    """Read the project's agent instructions (AGENTS.md, then CLAUDE.md).

    First file found in cwd wins. Content is treated like any other repo
    content: useful context, but a trust decision — see docs/ssdlc.md.
    """
    for name in INSTRUCTION_FILES:
        path = os.path.join(cwd, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > MAX_INSTRUCTIONS_CHARS:
            text = text[:MAX_INSTRUCTIONS_CHARS] + "\n[instructions truncated]"
        return f"## Project instructions ({name})\n\n{text}"
    return None


def default_system_prompt(cwd: str, model: str | None = None) -> str:
    identity = (
        f"You run on the model '{model}', an open-weight model served on the operator's own "
        "infrastructure. If asked which model or vendor you are, state exactly that; never claim "
        "to be GPT, Claude, Gemini, or any other vendor's model."
        if model
        else "You run on a model served on the operator's own infrastructure; never claim to be "
        "GPT, Claude, Gemini, or any other vendor's model."
    )
    return "\n".join(
        [
            "You are xHarness, a coding agent by xCloudinfo.",
            identity,
            f"Working directory: {cwd}",
            "Use the available tools to inspect and change files and to run commands.",
            "Prefer small verified steps: read before you edit, run checks after you change.",
            "When the task is done, reply with a concise summary in the language the user used.",
        ]
    )


@dataclass
class AgentOptions:
    system_prompt: str | None = None
    system_suffix: str | None = None  # appended to whichever system prompt applies (channel guidance etc.)
    max_turns: int = DEFAULT_MAX_TURNS
    cwd: str | None = None
    #: "auto" approves mutating tools silently; "prompt" asks via `prompt`.
    approval_mode: str = "auto"
    #: Asks the human. Absent under approval_mode "prompt" means deny.
    prompt: Callable[[str], bool] | None = None
    on_delta: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, str], None] | None = None
    on_tool_end: Callable[[str, str, bool], None] | None = None
    #: Resume: prior messages, including their system prompt if any.
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    #: Read AGENTS.md / CLAUDE.md from cwd into the system prompt.
    project_instructions: bool = True
    #: Agent name; session events of non-main agents are tagged with it.
    name: str = "main"
    #: Tool names hidden from this agent (e.g. children may not spawn children).
    exclude_tools: set[str] = field(default_factory=set)


class Agent:
    def __init__(self, ctx: Context, options: AgentOptions | None = None) -> None:
        self.ctx = ctx
        self.options = options or AgentOptions()
        self.messages: list[dict[str, Any]] = []
        self.total_usage = Usage()
        self._stop = threading.Event()
        cwd = self.options.cwd or os.getcwd()
        if self.options.initial_messages:
            self.messages.extend(self.options.initial_messages)
        if not any(message.get("role") == "system" for message in self.messages):
            llm = ctx.optional("llm")
            system = self.options.system_prompt or default_system_prompt(cwd, getattr(llm, "model", None))
            if self.options.system_suffix:
                system = f"{system}\n{self.options.system_suffix}"
            if self.options.project_instructions:
                instructions = load_project_instructions(cwd)
                if instructions:
                    system = f"{system}\n\n{instructions}"
            self.messages.insert(0, {"role": "system", "content": system})

    def stop(self) -> None:
        """Ask a running task to stop before its next model call or tool.

        An in-flight model request cannot be interrupted; the loop exits as
        soon as it returns.
        """
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def run(self, task: str) -> str:
        self._stop.clear()
        llm = self.ctx.get("llm")
        registry: ToolRegistry = self.ctx.get("tools")
        session: SessionLog | None = self.ctx.optional("session")

        self.messages.append({"role": "user", "content": task})
        if session:
            session.append({"type": "message", "role": "user", "content": task, **self._tag()})
        self.ctx.emit("agent/task-start", {"task": task})

        for turn in range(self.options.max_turns):
            if self._stop.is_set():
                self.ctx.emit("agent/task-end", {"turns": turn, "usage": self.total_usage, "stopped": True})
                return "[stopped by operator]"
            schemas = [
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in registry.list()
                if tool.name not in self.options.exclude_tools
            ]
            payload = {"messages": self.messages, "tools": schemas}
            reply: AssistantTurn = self.ctx.invoke(
                "llm/stream",
                payload,
                lambda current: llm.stream(
                    current["messages"], current["tools"], on_delta=self.options.on_delta
                ),
            )
            if reply.usage:
                self.total_usage.prompt_tokens += reply.usage.prompt_tokens
                self.total_usage.completion_tokens += reply.usage.completion_tokens

            assistant: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                assistant["tool_calls"] = reply.tool_calls
            self.messages.append(assistant)
            if session:
                session.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": reply.content,
                        "tool_calls": reply.tool_calls or None,
                        "usage": reply.usage.__dict__ if reply.usage else None,
                        **self._tag(),
                    }
                )

            if not reply.tool_calls:
                self.ctx.emit("agent/task-end", {"turns": turn + 1, "usage": self.total_usage})
                return reply.content

            for call in reply.tool_calls:
                if self._stop.is_set():
                    result = ToolResult("skipped: stopped by operator", is_error=True)
                else:
                    result = self._execute_call(registry, call)
                self.messages.append(
                    {"role": "tool", "content": result.output, "tool_call_id": call["id"]}
                )
                if session:
                    session.append(
                        {
                            "type": "tool-result",
                            "call_id": call["id"],
                            "name": call["name"],
                            "output": result.output,
                            "is_error": result.is_error,
                            **self._tag(),
                        }
                    )

        self.ctx.emit(
            "agent/task-end",
            {"turns": self.options.max_turns, "usage": self.total_usage, "stopped": True},
        )
        return "[stopped: max turns reached]"

    def _tag(self) -> dict[str, str]:
        return {"agent": self.options.name} if self.options.name != "main" else {}

    def _execute_call(self, registry: ToolRegistry, call: dict[str, str]) -> ToolResult:
        tool = registry.get(call["name"])
        if tool is None or call["name"] in self.options.exclude_tools:
            return ToolResult(f"unknown tool: {call['name']}", is_error=True)
        try:
            args = json.loads(call["arguments"]) if call["arguments"] else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must decode to an object")
        except (json.JSONDecodeError, ValueError):
            return ToolResult("tool arguments are not valid JSON", is_error=True)

        def approve(summary: str) -> bool:
            if self.options.approval_mode == "auto":
                return True
            if self.options.prompt is None:
                return False
            return self.options.prompt(summary)

        tool_ctx = ToolContext(cwd=self.options.cwd or os.getcwd(), approve=approve, agent=self.options.name)
        if self.options.on_tool_start:
            self.options.on_tool_start(call["name"], call["arguments"])
        try:
            result = tool.execute(args, tool_ctx)
        except Exception as error:  # noqa: BLE001 - a broken tool must not kill the loop
            result = ToolResult(f"tool failed: {error}", is_error=True)
        if self.options.on_tool_end:
            self.options.on_tool_end(call["name"], result.output, result.is_error)
        return result
