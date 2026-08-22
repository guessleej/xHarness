"""The default agent loop: call the model, execute tool calls, feed results
back, and stop when the model answers without tool calls or max_turns hits.
Model calls go through the interceptable "llm/stream" seam.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Context
from .llm import AssistantTurn, Usage
from .session import SessionLog
from .tools import ToolContext, ToolRegistry, ToolResult

DEFAULT_MAX_TURNS = 40


def default_system_prompt(cwd: str) -> str:
    return "\n".join(
        [
            "You are xHarness, a coding agent by xCloudinfo.",
            f"Working directory: {cwd}",
            "Use the available tools to inspect and change files and to run commands.",
            "Prefer small verified steps: read before you edit, run checks after you change.",
            "When the task is done, reply with a concise summary in the language the user used.",
        ]
    )


@dataclass
class AgentOptions:
    system_prompt: str | None = None
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


class Agent:
    def __init__(self, ctx: Context, options: AgentOptions | None = None) -> None:
        self.ctx = ctx
        self.options = options or AgentOptions()
        self.messages: list[dict[str, Any]] = []
        self.total_usage = Usage()
        cwd = self.options.cwd or os.getcwd()
        if self.options.initial_messages:
            self.messages.extend(self.options.initial_messages)
        if not any(message.get("role") == "system" for message in self.messages):
            self.messages.insert(
                0,
                {
                    "role": "system",
                    "content": self.options.system_prompt or default_system_prompt(cwd),
                },
            )

    def run(self, task: str) -> str:
        llm = self.ctx.get("llm")
        registry: ToolRegistry = self.ctx.get("tools")
        session: SessionLog | None = self.ctx.optional("session")

        self.messages.append({"role": "user", "content": task})
        if session:
            session.append({"type": "message", "role": "user", "content": task})
        self.ctx.emit("agent/task-start", {"task": task})

        for turn in range(self.options.max_turns):
            schemas = [
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in registry.list()
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
                    }
                )

            if not reply.tool_calls:
                self.ctx.emit("agent/task-end", {"turns": turn + 1, "usage": self.total_usage})
                return reply.content

            for call in reply.tool_calls:
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
                        }
                    )

        self.ctx.emit(
            "agent/task-end",
            {"turns": self.options.max_turns, "usage": self.total_usage, "stopped": True},
        )
        return "[stopped: max turns reached]"

    def _execute_call(self, registry: ToolRegistry, call: dict[str, str]) -> ToolResult:
        tool = registry.get(call["name"])
        if tool is None:
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

        tool_ctx = ToolContext(cwd=self.options.cwd or os.getcwd(), approve=approve)
        if self.options.on_tool_start:
            self.options.on_tool_start(call["name"], call["arguments"])
        try:
            result = tool.execute(args, tool_ctx)
        except Exception as error:  # noqa: BLE001 - a broken tool must not kill the loop
            result = ToolResult(f"tool failed: {error}", is_error=True)
        if self.options.on_tool_end:
            self.options.on_tool_end(call["name"], result.output, result.is_error)
        return result
