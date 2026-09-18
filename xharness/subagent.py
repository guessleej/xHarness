"""Subagents: delegate a bounded task to a fresh child agent, or fan several
out in parallel.

A child shares the parent's harness (tools, model, telemetry, session log)
but starts with its own empty conversation, cannot spawn children itself,
and routes every approval through the parent's policy: the child runs in
"prompt" mode with the parent's approve() as its prompter, so "auto" parents
approve silently and interactive parents still get asked.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .agent import Agent, AgentOptions
from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

DEFAULT_MAX_WORKERS = 4
DEFAULT_CHILD_MAX_TURNS = 20
MAX_CHILD_OUTPUT = 20_000
CHILD_TOOLS = {"subagent", "subagent_batch"}


def child_system_prompt(cwd: str, name: str, context: str) -> str:
    lines = [
        f"You are a child agent named {name!r}, working for a parent agent.",
        f"Working directory: {cwd}",
        "Complete only the task you are given, using tools as needed, then reply with",
        "a concise, self-contained result the parent can use directly.",
    ]
    if context:
        lines += ["", "Context from the parent:", context]
    return "\n".join(lines)


def run_child(
    ctx: Context,
    tool_ctx: ToolContext,
    task: str,
    name: str,
    context: str = "",
    max_turns: int = DEFAULT_CHILD_MAX_TURNS,
    approve_lock: threading.Lock | None = None,
) -> ToolResult:
    def approve(summary: str) -> bool:
        if approve_lock is None:
            return tool_ctx.approve(f"[{name}] {summary}")
        with approve_lock:
            return tool_ctx.approve(f"[{name}] {summary}")

    agent = Agent(
        ctx,
        AgentOptions(
            name=name,
            cwd=tool_ctx.cwd,
            system_prompt=child_system_prompt(tool_ctx.cwd, name, context),
            max_turns=max_turns,
            approval_mode="prompt",
            prompt=approve,
            exclude_tools=CHILD_TOOLS,
            project_instructions=False,
        ),
    )
    try:
        answer = agent.run(task)
    except Exception as error:  # noqa: BLE001 - a failed child is a result, not a crash
        return ToolResult(f"[{name}] failed: {error}", is_error=True)
    if len(answer) > MAX_CHILD_OUTPUT:
        answer = answer[:MAX_CHILD_OUTPUT] + "\n[truncated]"
    return ToolResult(answer or "(child returned no answer)")


def subagent_plugin(config: dict[str, Any] | None = None) -> Plugin:
    settings = config or {}
    max_workers = int(settings.get("max_workers", DEFAULT_MAX_WORKERS))
    child_max_turns = int(settings.get("max_turns", DEFAULT_CHILD_MAX_TURNS))

    def apply(ctx: Context, _config: Any) -> None:
        counter = {"n": 0}
        counter_lock = threading.Lock()

        def next_name(prefix: str | None) -> str:
            with counter_lock:
                counter["n"] += 1
                return prefix or f"child-{counter['n']}"

        def run_one(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            task = str(args.get("task") or "").strip()
            if not task:
                return ToolResult("task is required", is_error=True)
            name = next_name(str(args.get("name") or "") or None)
            return run_child(
                ctx,
                tool_ctx,
                task,
                name,
                context=str(args.get("context") or ""),
                max_turns=min(int(args.get("max_turns") or child_max_turns), child_max_turns),
            )

        def run_batch(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            tasks = args.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                return ToolResult("tasks must be a non-empty list", is_error=True)
            jobs: list[tuple[str, str, str]] = []
            for item in tasks:
                if isinstance(item, str):
                    item = {"task": item}
                if not isinstance(item, dict) or not str(item.get("task") or "").strip():
                    return ToolResult("each task needs a non-empty task string", is_error=True)
                jobs.append(
                    (
                        next_name(str(item.get("name") or "") or None),
                        str(item["task"]).strip(),
                        str(item.get("context") or ""),
                    )
                )
            approve_lock = threading.Lock()  # interactive prompts must not interleave
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(run_child, ctx, tool_ctx, task, name, context, child_max_turns, approve_lock)
                    for name, task, context in jobs
                ]
                results = [future.result() for future in futures]
            sections = [
                f"## {name}\n{result.output}" for (name, _task, _context), result in zip(jobs, results)
            ]
            any_error = any(result.is_error for result in results)
            return ToolResult("\n\n".join(sections), is_error=any_error and all(r.is_error for r in results))

        register_tools(
            ctx,
            [
                Tool(
                    name="subagent",
                    description=(
                        "Delegate one bounded task to a fresh child agent with its own conversation "
                        "and return its final answer. Use for a self-contained sub-problem whose "
                        "details would clutter your own context."
                    ),
                    mutating=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "description": "What the child must accomplish"},
                            "context": {"type": "string", "description": "Facts the child needs"},
                            "name": {"type": "string", "description": "Short label for logs"},
                            "max_turns": {"type": "number"},
                        },
                        "required": ["task"],
                    },
                    execute=run_one,
                ),
                Tool(
                    name="subagent_batch",
                    description=(
                        "Run several independent tasks in parallel child agents and return every "
                        "result under a heading per task. Tasks must not depend on each other."
                    ),
                    mutating=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "task": {"type": "string"},
                                        "context": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                    "required": ["task"],
                                },
                            }
                        },
                        "required": ["tasks"],
                    },
                    execute=run_batch,
                ),
            ],
        )

    return Plugin("subagent", apply)
