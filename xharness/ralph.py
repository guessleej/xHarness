"""ralph: one objective, handed to a fresh child agent round after round.

A single long conversation drifts: it fills with its own history, and what it
"remembers" is what happened to survive compaction. Ralph does the opposite.
Every round is a new child that sees only the unchanged objective, which round
this is, and the previous round's handoff. What the earlier rounds did lives in
the working directory (files, tests, git), which is the authoritative state.

Each child ends its answer with a status line:

    STATUS: complete | continue | blocked
    HANDOFF: what is done, what is not, what to do first next round

`complete` ends the run, `blocked` ends it and says why, `continue` starts the
next round. Running out of rounds ends it as `budget-limited`. The rounds run
one after another, in the foreground; changes go through the same approval as
any other child's.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from .context import Context, Plugin
from .subagent import find_role, run_child
from .tools import Tool, ToolContext, ToolResult, register_tools

DEFAULT_ROUNDS = 5
MAX_ROUNDS = 10
MAX_OBJECTIVE_CHARS = 4_000
MAX_HANDOFF_CHARS = 4_000
STATUS_RE = re.compile(r"^\s*STATUS\s*[:：]\s*(complete|continue|blocked)\s*$", re.I | re.M)
HANDOFF_RE = re.compile(r"^\s*HANDOFF\s*[:：]\s*(.*)\Z", re.I | re.M | re.S)

ROUND_BRIEF = (
    "This is round {n} of at most {limit} on one objective. Earlier rounds were done by other agents that you cannot "
    "ask; the working directory (files, tests, git history) is the authoritative record of what has been done, so "
    "look there before you trust any summary.\n\n"
    "Objective (unchanged for every round):\n{objective}\n\n{previous}"
    "Do the next useful part of the work, check it, then end your answer with exactly these two parts:\n"
    "STATUS: complete   (the objective is met and checked)  |  continue   (more to do)  |  blocked   (cannot go on, say why)\n"
    "HANDOFF: what is done, what is not, and what to do first next round."
)


def parse_round(answer: str) -> tuple[str, str]:
    """(status, handoff). A child that forgot the status line counts as still going."""
    statuses = STATUS_RE.findall(answer)
    status = statuses[-1].lower() if statuses else "continue"
    found = HANDOFF_RE.search(answer)
    handoff = (found.group(1) if found else answer).strip()
    return status, handoff[:MAX_HANDOFF_CHARS]


def ralph_plugin(config: dict[str, Any] | None = None) -> Plugin:
    settings = config or {}
    limit = max(1, min(int(settings.get("ralph_rounds", DEFAULT_ROUNDS)), MAX_ROUNDS))

    def apply(ctx: Context, _config: Any) -> None:
        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            objective = str(args.get("objective") or "").strip()
            if not objective:
                return ToolResult("objective is required", is_error=True)
            if len(objective) > MAX_OBJECTIVE_CHARS:
                return ToolResult(f"objective is {len(objective)} characters; keep it under {MAX_OBJECTIVE_CHARS}", is_error=True)
            try:
                rounds = max(1, min(int(args.get("max_rounds") or limit), limit))
            except (TypeError, ValueError):
                return ToolResult("max_rounds must be a number", is_error=True)
            role = str(args.get("agent") or "") or None
            _found, problem = find_role(ctx, role, tool_ctx.cwd)
            if problem:
                return ToolResult(problem, is_error=True)
            run_id = secrets.token_hex(3)
            session = ctx.optional("session")
            handoff, result, last, started = "", "budget-limited", "", 0
            for number in range(1, rounds + 1):
                previous = f"Handoff from round {number - 1}:\n{handoff}\n\n" if handoff else ""
                brief = ROUND_BRIEF.format(n=number, limit=rounds, objective=objective, previous=previous)
                out = run_child(ctx, tool_ctx, brief, f"ralph-{run_id}-r{number}", "", 20, None, role, kind="ralph")
                started = number
                if session is not None:
                    session.append({"type": "ralph-round", "run": run_id, "round": number, "error": out.is_error})
                if out.is_error:
                    return ToolResult(f"ralph {run_id}: round {number} failed\n{out.output}", is_error=True)
                status, handoff = parse_round(out.output)
                last = out.output
                if status in ("complete", "blocked"):
                    result = status
                    break
            summary = last if len(last) <= 3_000 else last[:3_000] + "\n[truncated]"
            return ToolResult(f"ralph {run_id}: {result} after {started} round(s) of {rounds}\n\nLast round:\n{summary}",
                              is_error=result == "blocked")

        register_tools(ctx, [Tool(
            name="ralph",
            description=("Work on one objective in rounds, each by a fresh agent that sees only the objective and the "
                         "last round's handoff; the files in the working directory carry the progress. Use for a long, "
                         f"checkable piece of work (up to {limit} rounds) that would swamp one conversation. It waits "
                         "until the work is complete, blocked or out of rounds."),
            mutating=True,
            parameters={"type": "object", "properties": {
                "objective": {"type": "string", "description": "What must be true when done, and how to check it"},
                "max_rounds": {"type": "number", "description": f"default and maximum {limit}"},
                "agent": {"type": "string", "description": "Optional agent role for every round"}},
                "required": ["objective"]},
            execute=run,
        )])

    return Plugin("ralph", apply)
