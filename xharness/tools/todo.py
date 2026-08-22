"""A per-harness working todo list the model maintains for multi-step tasks."""

from __future__ import annotations

from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

VALID_STATUSES = {"pending", "in_progress", "completed"}


def _apply(ctx: Context, _config: Any) -> None:
    items: list[dict[str, str]] = []

    def execute(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
        todos = args.get("todos")
        if not isinstance(todos, list):
            return ToolResult("todos must be a list", is_error=True)
        for item in todos:
            if not isinstance(item, dict) or not item.get("content") or item.get("status") not in VALID_STATUSES:
                return ToolResult("each todo needs content and a valid status", is_error=True)
        items[:] = todos
        if not items:
            return ToolResult("(todo list cleared)")
        return ToolResult("\n".join(f"[{item['status']}] {item['content']}" for item in items))

    tool = Tool(
        name="todo_write",
        description=(
            "Replace the working todo list for the current task. "
            "Statuses: pending, in_progress, completed. Keep exactly one item in_progress."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
        execute=execute,
    )
    register_tools(ctx, [tool])
    ctx.provide("todos", {"list": lambda: list(items)})


todo_tool_plugin = Plugin("tool-todo", _apply)
