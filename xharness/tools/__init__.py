"""Tool registry and the vocabulary tools speak."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..context import Context, Plugin


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


@dataclass
class ToolContext:
    cwd: str
    #: Ask the approval policy whether a side-effectful action may run.
    #: Mutating tools call this before acting; the policy is owned by the agent.
    approve: Callable[[str], bool]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any], ToolContext], ToolResult]
    #: Marks tools with side effects, for documentation and policy surfaces.
    mutating: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        return lambda: self._tools.pop(tool.name, None)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


def _apply_tools(ctx: Context, _config: Any) -> None:
    ctx.provide("tools", ToolRegistry())


tools_plugin = Plugin("tools", _apply_tools)


def register_tools(ctx: Context, tools: list[Tool]) -> None:
    """Helper for tool plugins: register tools and unwind them with the plugin."""
    registry: ToolRegistry = ctx.get("tools")
    for tool in tools:
        ctx.on_dispose(registry.register(tool))
