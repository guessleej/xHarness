"""Subagents: delegate a bounded task to a fresh child agent, or fan several
out in parallel.

A child shares the parent's harness (tools, model, telemetry, session log)
but starts with its own empty conversation, cannot spawn children itself,
and routes every approval through the parent's policy: the child runs in
"prompt" mode with the parent's approve() as its prompter, so "auto" parents
approve silently and interactive parents still get asked.

`agent` names a role (roles.py): the child then follows the role's
instructions, gets only the role's tools, runs on the role's model, and stops
at the role's turn and token limits. Every child is entered in the
conversation's live-agent register while it works.

`isolate: true` gives a child that changes code a git worktree of its own;
its work reaches the person's files only after the `check` command passes and
the change still applies cleanly (isolation.py).
"""

from __future__ import annotations

import subprocess  # nosec B404 - only for its exception types; isolation.py runs git
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import isolation
from .agent import Agent, AgentOptions
from .context import Context, Plugin
from .roles import exclusions
from .tools import Tool, ToolContext, ToolResult, register_tools

DEFAULT_MAX_WORKERS = 4
DEFAULT_CHILD_MAX_TURNS = 20
MAX_CHILD_OUTPUT = 20_000
#: Hidden from children. They cannot spawn children, and they do not question the
#: person: a parallel batch would bury them in questions. A child that is missing
#: something says so in its answer, and the parent can ask.
CHILD_TOOLS = {"subagent", "subagent_batch", "ask_user", "goal_done", "workflow", "discussion", "discussion_join",
               "schedule_add", "schedule_list", "schedule_cancel", "ralph", "interrupt_agent"}
#: Background children running at once, per conversation.
MAX_BACKGROUND = 4
ISOLATE_HELP = ("Work in a separate git worktree (a copy of the working directory as it is now). The change is "
                "applied to the working directory only if check passes and it still applies cleanly; otherwise it "
                "is kept as a branch. Use for children that edit code, above all several at once.")
CHECK_HELP = ("With isolate: a shell command the system runs in the child's worktree when it finishes, e.g. "
              "\"python -m pytest -q\"; exit code 0 means the change may be applied.")


def child_system_prompt(cwd: str, name: str, context: str, model: str | None = None, role: Any = None,
                        isolated: bool = False) -> str:
    lines = [
        f"You are a child agent named {name!r}, working for a parent agent.",
        (f"You run on the model '{model}' on the operator's own infrastructure; never claim to be another vendor's model."
         if model else "You run on the operator's own model; never claim to be another vendor's model."),
        f"Working directory: {cwd}",
        "Complete only the task you are given, using tools as needed, then reply with",
        "a concise, self-contained result the parent can use directly.",
    ]
    if isolated:
        lines += ["", "You work in a separate copy of the project (a git worktree). Change files here as needed; "
                  "do not run git commit, push or checkout -- your changes are collected, checked and applied for you "
                  "when you finish. Use paths relative to the working directory."]
    if role is not None and role.prompt:
        lines += ["", f"Your role ({role.name}):", role.prompt]
    if context:
        lines += ["", "Context from the parent:", context]
    return "\n".join(lines)


def find_role(ctx: Context, name: str | None, cwd: str) -> tuple[Any, str]:
    """(role or None, error). An unknown name is an error listing the roles there are."""
    if not name:
        return None, ""
    store = ctx.optional("roles")
    role = store.get(name, cwd) if store is not None else None
    if role is None:
        known = ", ".join(r.name for r in store.discover(cwd)[:30]) if store is not None else ""
        return None, f"no agent role named {name!r}; available: {known or '(none)'}"
    return role, ""


def run_child(
    ctx: Context,
    tool_ctx: ToolContext,
    task: str,
    name: str,
    context: str = "",
    max_turns: int = DEFAULT_CHILD_MAX_TURNS,
    approve_lock: threading.Lock | None = None,
    role: str | None = None,
    kind: str = "subagent",
    read_only: bool = False,
    isolate: bool = False,
    check: str = "",
) -> ToolResult:
    found, problem = find_role(ctx, role, tool_ctx.cwd)
    if problem:
        return ToolResult(problem, is_error=True)
    workspace = None
    if isolate and not read_only:
        try:
            workspace = isolation.prepare(tool_ctx.cwd, name)
        except (isolation.IsolationError, OSError, subprocess.SubprocessError) as error:
            return ToolResult(f"[{name}] could not make a separate workspace: {error}", is_error=True)
    llm, note = (None, "")
    if found is not None:
        llm, note = ctx.get("roles").resolve_model(found)
        max_turns = min(found.max_turns or max_turns, max_turns)

    def approve(summary: str) -> bool:
        if approve_lock is None:
            return tool_ctx.approve(f"[{name}] {summary}")
        with approve_lock:
            return tool_ctx.approve(f"[{name}] {summary}")

    live = ctx.optional("agents")
    holder: dict[str, str] = {}
    agent = Agent(
        ctx,
        AgentOptions(
            name=name,
            cwd=workspace.cwd if workspace else tool_ctx.cwd,
            system_prompt=child_system_prompt(workspace.cwd if workspace else tool_ctx.cwd, name, context,
                                              getattr(llm or ctx.optional("llm"), "model", None), found,
                                              isolated=workspace is not None),
            max_turns=max_turns,
            approval_mode="prompt",
            prompt=approve,
            exclude_tools=exclusions(ctx.get("tools"), found, CHILD_TOOLS | (
                {t.name for t in ctx.get("tools").list() if t.mutating} if read_only else set())),
            project_instructions=False,
            llm=llm,
            max_tokens=found.max_tokens if found is not None else None,
            on_tool_start=lambda tool, _args: live.activity(holder.get("id", ""), tool) if live else None,
        ),
    )
    if live is not None:
        holder["id"] = live.start(agent, name, found.name if found else "", kind, task, parent=tool_ctx.agent)
    ctx.emit("subagent/start", {"name": name, "task": task, "parent": tool_ctx.agent, "role": found.name if found else ""})
    try:
        answer = agent.run(task)
    except Exception as error:  # noqa: BLE001 - a failed child is a result, not a crash
        if live is not None:
            live.end(holder["id"], ok=False)
        ctx.emit("subagent/end", {"name": name, "ok": False})
        if workspace is not None:
            outcome = _integrate(ctx, tool_ctx, workspace, check, name)
            return ToolResult(f"[{name}] failed: {error}\n{isolation.describe(outcome, name)}", is_error=True)
        return ToolResult(f"[{name}] failed: {error}", is_error=True)
    if live is not None:
        live.end(holder["id"], ok=not answer.startswith("[stopped"))
    ctx.emit("subagent/end", {"name": name, "ok": True})
    if len(answer) > MAX_CHILD_OUTPUT:
        answer = answer[:MAX_CHILD_OUTPUT] + "\n[truncated]"
    if note:
        answer = f"({note})\n{answer}"
    answer = answer or "(child returned no answer)"
    if workspace is not None:
        outcome = _integrate(ctx, tool_ctx, workspace, check, name)
        failed = outcome.get("status") in ("check-failed", "conflict")
        return ToolResult(f"{answer}\n\n{isolation.describe(outcome, name)}", is_error=failed)
    return ToolResult(answer)


def _integrate(ctx: Context, tool_ctx: ToolContext, workspace: Any, check: str, name: str) -> dict[str, Any]:
    """Collect a child's worktree: check it, apply it or keep it as a branch; log what happened."""
    try:
        outcome = isolation.finish(workspace, check, lambda s: tool_ctx.approve(f"[{name}] {s}"), ctx.optional("sandbox"))
    except (isolation.IsolationError, OSError, subprocess.SubprocessError) as error:
        outcome = {"status": "error", "detail": str(error)}
    session = ctx.optional("session")
    if session is not None:
        session.append({"type": "workspace", "agent": name, "parent": tool_ctx.agent,
                        **{k: outcome[k] for k in ("status", "stat", "branch") if k in outcome},
                        "files": len(outcome.get("files") or []),
                        "check_exit": (outcome.get("check") or {}).get("exit")})
    ctx.emit("subagent/workspace", {"name": name, **outcome})
    return outcome


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

        background = {"running": 0}

        def start_background(tool_ctx: ToolContext, task: str, name: str, context: str, max_turns: int,
                             role: str | None) -> ToolResult:
            """The child works while the parent carries on; its reports and final answer arrive as messages.

            Read-only: nobody is waiting at an approval prompt for a child the parent has already left."""
            mail = ctx.optional("agentmail")
            parent = mail.get(tool_ctx.agent) if mail is not None else None
            if parent is None:
                return ToolResult("background is not available here", is_error=True)
            found, problem = find_role(ctx, role, tool_ctx.cwd)
            if problem:
                return ToolResult(problem, is_error=True)
            with counter_lock:
                if background["running"] >= MAX_BACKGROUND:
                    return ToolResult(f"{MAX_BACKGROUND} background agents are already running; wait for one to finish "
                                      "or stop one with interrupt_agent", is_error=True)
                background["running"] += 1

            def work() -> None:
                try:
                    result = run_child(ctx, tool_ctx, task, name, context, max_turns, None, role, read_only=True)
                finally:
                    with counter_lock:
                        background["running"] -= 1
                head = "Final answer" if not result.is_error else "Failed"
                parent.deliver(name, f"{head} of background agent {name} (task: {task[:120]}):\n{result.output}")

            threading.Thread(target=work, name=f"xh-bg-{name}", daemon=True).start()
            return ToolResult(f"started {name} in the background (read-only). Its reports and final answer arrive as "
                              "messages from it at the start of your later steps; carry on with other work meanwhile. "
                              "Do not start it again: if list_agents shows it gone, it has finished and its answer is "
                              "already on the way. interrupt_agent stops one that is still running.")

        def run_one(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            task = str(args.get("task") or "").strip()
            if not task:
                return ToolResult("task is required", is_error=True)
            name = next_name(str(args.get("name") or "") or None)
            if args.get("background"):
                return start_background(tool_ctx, task, name, str(args.get("context") or ""),
                                        min(int(args.get("max_turns") or child_max_turns), child_max_turns),
                                        str(args.get("agent") or "") or None)
            return run_child(
                ctx,
                tool_ctx,
                task,
                name,
                context=str(args.get("context") or ""),
                max_turns=min(int(args.get("max_turns") or child_max_turns), child_max_turns),
                role=str(args.get("agent") or "") or None,
                isolate=bool(args.get("isolate")),
                check=str(args.get("check") or "").strip(),
            )

        def run_batch(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            tasks = args.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                return ToolResult("tasks must be a non-empty list", is_error=True)
            isolate = bool(args.get("isolate"))
            default_check = str(args.get("check") or "").strip()
            jobs: list[tuple[str, str, str, str | None, str]] = []
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
                        str(item.get("agent") or "") or None,
                        str(item.get("check") or "").strip() or default_check,
                    )
                )
            approve_lock = threading.Lock()  # interactive prompts must not interleave
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(run_child, ctx, tool_ctx, task, name, context, child_max_turns, approve_lock, role,
                                isolate=isolate, check=check)
                    for name, task, context, role, check in jobs
                ]
                results = [future.result() for future in futures]
            sections = [
                f"## {name}\n{result.output}" for (name, *_rest), result in zip(jobs, results)
            ]
            if isolate and default_check and any("applied to the working directory" in r.output for r in results):
                # Each change passed on its own; check that they also work together.
                final = isolation.run_check(tool_ctx.cwd, default_check, tool_ctx.approve, ctx.optional("sandbox"),
                                            label="all changes together")
                sections.append(f"## all changes together\ncheck `{default_check}` exit {final['exit']}"
                                + ("" if final["exit"] == 0 else "\n" + final["output"][-1500:]))
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
                            "agent": {"type": "string", "description": "Optional agent role to run as (see Agent roles)"},
                            "max_turns": {"type": "number"},
                            "background": {"type": "boolean", "description": (
                                "Start it and carry on without waiting. It can only read, not change anything; its "
                                "reports and final answer reach you as messages. At most 4 at a time.")},
                            "isolate": {"type": "boolean", "description": ISOLATE_HELP},
                            "check": {"type": "string", "description": CHECK_HELP},
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
                                        "agent": {"type": "string", "description": "Optional agent role"},
                                        "check": {"type": "string", "description": "This task's own check command"},
                                    },
                                    "required": ["task"],
                                },
                            },
                            "isolate": {"type": "boolean", "description": ISOLATE_HELP},
                            "check": {"type": "string", "description": CHECK_HELP + " With isolate, it also runs once "
                                      "more on the working directory after every change is applied."},
                        },
                        "required": ["tasks"],
                    },
                    execute=run_batch,
                ),
            ],
        )

    return Plugin("subagent", apply)
