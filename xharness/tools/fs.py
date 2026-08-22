"""Filesystem tools: read, write, and exact-string edit."""

from __future__ import annotations

import os
from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

MAX_READ_LINES = 2000
MAX_LINE_LENGTH = 2000


def _resolve(cwd: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def _read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx.cwd, str(args.get("path") or ""))
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError as error:
        return ToolResult(f"cannot read {path}: {error}", is_error=True)
    offset = max(1, int(args.get("offset") or 1))
    limit = min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES)
    window = lines[offset - 1 : offset - 1 + limit]
    numbered = []
    for index, line in enumerate(window):
        shown = line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH] + "..."
        numbered.append(f"{offset + index:>6}\t{shown}")
    remaining = len(lines) - (offset - 1 + len(window))
    tail = f"\n[{remaining} more lines; continue with offset={offset + len(window)}]" if remaining > 0 else ""
    return ToolResult("\n".join(numbered) + tail)


def read_tool() -> Tool:
    return Tool(
        name="read",
        description="Read a text file. Returns numbered lines. Use offset/limit for large files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
                "offset": {"type": "number", "description": "1-based line to start from"},
                "limit": {"type": "number", "description": f"Max lines to return; default {MAX_READ_LINES}"},
            },
            "required": ["path"],
        },
        execute=_read,
    )


def _write(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx.cwd, str(args.get("path") or ""))
    if not ctx.approve(f"write: {path}"):
        return ToolResult("denied by approval policy", is_error=True)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(args.get("content") or ""))
    except OSError as error:
        return ToolResult(f"cannot write {path}: {error}", is_error=True)
    return ToolResult(f"wrote {path}")


def write_tool() -> Tool:
    return Tool(
        name="write",
        description="Write a file, creating parent directories. Overwrites existing content.",
        mutating=True,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        execute=_write,
    )


def _edit(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx.cwd, str(args.get("path") or ""))
    old = str(args.get("old_string") or "")
    new = str(args.get("new_string") or "")
    if not old:
        return ToolResult("old_string is required", is_error=True)
    if old == new:
        return ToolResult("old_string and new_string are identical", is_error=True)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return ToolResult(f"cannot read {path}: {error}", is_error=True)
    count = text.count(old)
    if count == 0:
        return ToolResult(f"old_string not found in {path}", is_error=True)
    if count > 1 and not args.get("replace_all"):
        return ToolResult(
            f"old_string occurs {count} times in {path}; add more context or set replace_all",
            is_error=True,
        )
    if not ctx.approve(f"edit: {path}"):
        return ToolResult("denied by approval policy", is_error=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new))
    plural = "s" if count > 1 else ""
    return ToolResult(f"edited {path} ({count} replacement{plural})")


def edit_tool() -> Tool:
    return Tool(
        name="edit",
        description=(
            "Replace an exact string in a file. old_string must match exactly and, "
            "unless replace_all is true, must occur exactly once."
        ),
        mutating=True,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        execute=_edit,
    )


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [read_tool(), write_tool(), edit_tool()])


fs_tools_plugin = Plugin("tool-fs", _apply)
