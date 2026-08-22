"""Search tools: glob over paths and grep over contents."""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

SKIP_DIRS = {".git", "node_modules", "dist", ".next", "__pycache__", ".venv", ".tox"}
MAX_DEPTH = 14
MAX_GLOB_RESULTS = 500
MAX_GREP_MATCHES = 200
MAX_GREP_FILE_BYTES = 1_000_000


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern (supports **, *, ?) into a regex over posix paths."""
    double_slash = "\x01"  # private placeholders, never present in patterns
    double = "\x02"
    escaped = re.escape(pattern)
    # re.escape turns * into \* and ? into \? and / stays /
    escaped = escaped.replace(r"\*\*/", double_slash).replace(r"\*\*", double)
    escaped = escaped.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    escaped = escaped.replace(double_slash, "(?:.*/)?").replace(double, ".*")
    return re.compile(f"^{escaped}$")


def _walk(root: str) -> Iterator[str]:
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - root_depth > MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def _to_posix(path: str) -> str:
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _root(args: dict[str, Any], ctx: ToolContext) -> str:
    raw = str(args.get("path") or ".")
    return raw if os.path.isabs(raw) else os.path.join(ctx.cwd, raw)


def _glob(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = _root(args, ctx)
    regex = glob_to_regex(str(args.get("pattern") or ""))
    matches: list[str] = []
    for file in _walk(root):
        relative = _to_posix(os.path.relpath(file, root))
        if regex.match(relative):
            matches.append(relative)
            if len(matches) >= MAX_GLOB_RESULTS:
                break
    if not matches:
        return ToolResult("no files matched")
    capped = f"\n[capped at {MAX_GLOB_RESULTS} results]" if len(matches) >= MAX_GLOB_RESULTS else ""
    return ToolResult("\n".join(sorted(matches)) + capped)


def glob_tool() -> Tool:
    return Tool(
        name="glob",
        description="Find files whose path matches a glob pattern (supports **, *, ?), relative to the search root.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "e.g. src/**/*.py"},
                "path": {"type": "string", "description": "Search root; defaults to cwd"},
            },
            "required": ["pattern"],
        },
        execute=_glob,
    )


def _grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        regex = re.compile(str(args.get("pattern") or ""), flags)
    except re.error as error:
        return ToolResult(f"invalid regex: {error}", is_error=True)
    root = _root(args, ctx)
    include = glob_to_regex(str(args["include"])) if args.get("include") else None
    results: list[str] = []
    for file in _walk(root):
        relative = _to_posix(os.path.relpath(file, root))
        if include and not include.match(relative):
            continue
        try:
            if os.path.getsize(file) > MAX_GREP_FILE_BYTES:
                continue
            with open(file, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue  # skip binary files
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.split("\n"), start=1):
            if regex.search(line):
                results.append(f"{relative}:{line_number}: {line.strip()[:300]}")
                if len(results) >= MAX_GREP_MATCHES:
                    break
        if len(results) >= MAX_GREP_MATCHES:
            break
    if not results:
        return ToolResult("no matches")
    capped = f"\n[capped at {MAX_GREP_MATCHES} matches]" if len(results) >= MAX_GREP_MATCHES else ""
    return ToolResult("\n".join(results) + capped)


def grep_tool() -> Tool:
    return Tool(
        name="grep",
        description="Search file contents with a regular expression. Returns path:line: text.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex"},
                "path": {"type": "string", "description": "Search root; defaults to cwd"},
                "include": {"type": "string", "description": "Only files matching this glob, e.g. **/*.py"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
        execute=_grep,
    )


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [glob_tool(), grep_tool()])


search_tools_plugin = Plugin("tool-search", _apply)
