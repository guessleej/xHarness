"""Goal mode: keep working toward an objective, round after round, until the
model says it is done or blocked, or the round limit is reached.

A single task ends the moment the model replies without calling a tool, and
small models stop early: "I have started the refactor, the rest is similar."
Goal mode gives the model a way to say "finished" explicitly -- the
goal_done tool -- and answers any other stop with another round, up to a
limit. Every round is an ordinary logged request, so the transcript shows
exactly what the model was told and when.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

DEFAULT_ROUNDS = 5
MAX_ROUNDS = 20
GOAL_CONTEXT = (
    "Goal mode: work toward this goal across as many steps as it takes. When the goal is complete, call "
    "goal_done with status \"done\" and a summary of what was achieved and how it was checked. If you cannot "
    "go further (missing access, a decision only the person can make), call goal_done with status \"blocked\" "
    "and say why. If you stop without calling goal_done you will be asked to continue."
)


@dataclass
class GoalState:
    objective: str = ""
    max_rounds: int = DEFAULT_ROUNDS
    round: int = 0
    status: str = ""        # "", "active", "done", "blocked", "incomplete", "stopped"
    summary: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    def public(self) -> dict[str, Any]:
        return {"objective": self.objective, "max_rounds": self.max_rounds, "round": self.round,
                "status": self.status, "summary": self.summary}


def goal_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        state = GoalState()
        ctx.provide("goal", state)

        def done(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            if not state.active:
                return ToolResult("no goal is active", is_error=True)
            status = str(args.get("status") or "done").strip().lower()
            if status not in ("done", "blocked"):
                return ToolResult("status must be done or blocked", is_error=True)
            summary = str(args.get("summary") or "").strip()
            if not summary:
                return ToolResult("summary is required: what was achieved, or why you are blocked", is_error=True)
            state.status, state.summary = status, summary[:4000]
            return ToolResult(f"goal marked {status}. Finish your turn with a short reply to the person.")

        register_tools(ctx, [Tool(
            name="goal_done",
            description=("Only in goal mode: report that the goal is complete (status done, with what was "
                         "achieved and how it was checked) or that you cannot continue (status blocked, with why)."),
            parameters={"type": "object", "properties": {
                "status": {"type": "string", "enum": ["done", "blocked"]},
                "summary": {"type": "string"},
            }, "required": ["status", "summary"]},
            execute=done,
        )])

    return Plugin("goal", apply)


def run_goal(agent: Any, objective: str, max_rounds: int = DEFAULT_ROUNDS,
             on_event: Callable[[dict[str, Any]], None] | None = None) -> str:
    """Drive `agent` toward `objective`. Returns the last reply."""
    state: GoalState | None = agent.ctx.optional("goal")
    if state is None:
        raise RuntimeError("goal mode is not available (goal plugin not mounted)")
    max_rounds = max(1, min(int(max_rounds or DEFAULT_ROUNDS), MAX_ROUNDS))
    state.objective, state.max_rounds, state.round, state.status, state.summary = objective, max_rounds, 0, "active", ""
    session = agent.ctx.optional("session")
    emit = on_event or (lambda _event: None)
    if session:
        session.append({"type": "goal", "objective": objective, "max_rounds": max_rounds})
    emit({"type": "goal", "state": "started", **state.public()})
    answer = ""
    try:
        while True:
            state.round += 1
            emit({"type": "goal", "state": "round", **state.public()})
            if state.round == 1:
                answer = agent.run(f"Goal: {objective}", extra_context=GOAL_CONTEXT)
            else:
                answer = agent.run(
                    "Continue toward the goal.",
                    extra_context=(f"Goal mode, round {state.round} of {max_rounds}: the goal is not marked done yet. "
                                   f"Goal: {objective}. Pick up where you left off; call goal_done when it is "
                                   "complete or if you are blocked."),
                )
            if not state.active:
                break
            if agent.stop_requested:
                state.status = "stopped"
                break
            if state.round >= max_rounds:
                state.status = "incomplete"
                break
    except Exception:
        state.status = "stopped" if agent.stop_requested else "failed"
        raise
    finally:
        if session:
            session.append({"type": "goal-end", **state.public()})
        emit({"type": "goal", "state": state.status, **state.public()})
    return answer
