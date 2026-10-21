"""Workflows: a multi-step plan the model writes as steps with dependencies,
run by subagents in dependency order.

subagent_batch runs independent tasks side by side; real work is rarely that
flat ("read the three modules, then design the change, then update the code
and the tests, then review"). The workflow tool takes the steps and their
`depends_on`, checks the graph (unknown steps, cycles), runs every step whose
dependencies are done -- in parallel where it can -- and hands each step the
results of the steps it depends on. A failed step skips everything that
depends on it instead of building on a hole. Each step is an ordinary child
agent: same tools, same approval policy, same session log.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .context import Context, Plugin
from .subagent import DEFAULT_CHILD_MAX_TURNS, DEFAULT_MAX_WORKERS, run_child
from .tools import Tool, ToolContext, ToolResult, register_tools

MAX_STEPS = 12
MAX_HANDOFF_CHARS = 4000
MAX_STEP_REPORT = 6000
STEP_ID = re.compile(r"^[A-Za-z0-9][\w-]{0,40}$")


def plan_order(steps: list[dict[str, Any]]) -> list[list[str]]:
    """Waves of step ids that can run together; ValueError for a bad graph."""
    ids = [str(step.get("id") or "") for step in steps]
    if len(set(ids)) != len(ids):
        raise ValueError("step ids must be unique")
    bad = [i for i in ids if not STEP_ID.match(i)]
    if bad:
        raise ValueError(f"invalid step id {bad[0]!r}: letters, digits, - and _ only")
    deps = {}
    for step in steps:
        needs = [str(d) for d in (step.get("depends_on") or [])]
        unknown = [d for d in needs if d not in ids]
        if unknown:
            raise ValueError(f"step {step['id']!r} depends on unknown step {unknown[0]!r}")
        deps[str(step["id"])] = set(needs)
    waves: list[list[str]] = []
    done: set[str] = set()
    while len(done) < len(ids):
        ready = [i for i in ids if i not in done and deps[i] <= done]
        if not ready:
            raise ValueError("the steps depend on each other in a cycle")
        waves.append(ready)
        done.update(ready)
    return waves


def workflow_plugin(config: dict[str, Any] | None = None) -> Plugin:
    settings = config or {}
    max_workers = int(settings.get("max_workers", DEFAULT_MAX_WORKERS))
    child_turns = int(settings.get("max_turns", DEFAULT_CHILD_MAX_TURNS))

    def apply(ctx: Context, _config: Any) -> None:
        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            steps = args.get("steps")
            if not isinstance(steps, list) or not steps:
                return ToolResult("steps must be a non-empty list", is_error=True)
            if len(steps) > MAX_STEPS:
                return ToolResult(f"at most {MAX_STEPS} steps; split the work", is_error=True)
            if not all(isinstance(s, dict) and str(s.get("task") or "").strip() for s in steps):
                return ToolResult("every step needs an id and a non-empty task", is_error=True)
            try:
                waves = plan_order(steps)
            except ValueError as error:
                return ToolResult(f"invalid workflow: {error}", is_error=True)
            by_id = {str(s["id"]): s for s in steps}
            results: dict[str, ToolResult] = {}
            skipped: dict[str, str] = {}
            approve_lock = threading.Lock()
            shared = str(args.get("context") or "")
            ctx.emit("workflow/start", {"steps": [s["id"] for s in steps]})

            def run_step(step_id: str) -> tuple[str, ToolResult]:
                step = by_id[step_id]
                handoff = [f"## Result of step {d}\n{results[d].output[:MAX_HANDOFF_CHARS]}"
                           for d in step.get("depends_on") or []]
                context = "\n\n".join(part for part in (shared, str(step.get("context") or ""),
                                                          "\n\n".join(handoff)) if part)
                return step_id, run_child(ctx, tool_ctx, str(step["task"]).strip(), f"step-{step_id}",
                                          context=context, max_turns=child_turns, approve_lock=approve_lock)

            for wave in waves:
                runnable = []
                for step_id in wave:
                    failed = [d for d in by_id[step_id].get("depends_on") or []
                              if d in skipped or (d in results and results[d].is_error)]
                    if failed:
                        skipped[step_id] = f"skipped because step {failed[0]} did not succeed"
                    else:
                        runnable.append(step_id)
                with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(runnable) or 1))) as pool:
                    for step_id, result in pool.map(run_step, runnable):
                        results[step_id] = result
            report = []
            for step in steps:
                step_id = str(step["id"])
                if step_id in skipped:
                    report.append(f"## {step_id}: skipped\n{skipped[step_id]}")
                else:
                    result = results[step_id]
                    report.append(f"## {step_id}: {'failed' if result.is_error else 'done'}\n{result.output[:MAX_STEP_REPORT]}")
            ok = all(step in results and not results[step].is_error for step in by_id)
            ctx.emit("workflow/end", {"ok": ok})
            return ToolResult("\n\n".join(report), is_error=not ok and not any(not r.is_error for r in results.values()))

        register_tools(ctx, [Tool(
            name="workflow",
            description=("Run a multi-step plan as subagents in dependency order. Give each step an id, a task, "
                         "and depends_on (ids of steps whose results it needs). Steps without pending dependencies "
                         "run in parallel; each step receives the results of the steps it depends on; a failed "
                         f"step skips its dependents. Up to {MAX_STEPS} steps. Use it for work with real stages "
                         "(investigate, then change, then verify); use subagent_batch for independent tasks."),
            parameters={"type": "object", "properties": {
                "steps": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "task": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string"}}, "required": ["id", "task"]}},
                "context": {"type": "string", "description": "Shared context every step receives"},
            }, "required": ["steps"]},
            execute=run,
            mutating=True,
        )])

    return Plugin("workflow", apply)
