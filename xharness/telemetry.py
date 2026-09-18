"""Telemetry and cost brakes.

An "llm/stream" middleware counts every model call (tokens, tool calls,
latency) and can hard-stop a runaway agent when a configured budget is
exceeded. Zero core changes: token counts come from the adapter's Usage,
tool-call counts from the returned turn, latency from wrapping the call.
"""

from __future__ import annotations

import time
from typing import Any

from .context import Context, Plugin


class BudgetExceeded(RuntimeError):
    """Raised before a model call would cross a configured budget limit."""


class Telemetry:
    def __init__(
        self,
        max_total_tokens: int | None = None,
        max_llm_calls: int | None = None,
        cost_per_1k_tokens: float | None = None,
    ) -> None:
        self.max_total_tokens = max_total_tokens
        self.max_llm_calls = max_llm_calls
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_calls = 0
        self.elapsed_seconds = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float | None:
        if self.cost_per_1k_tokens is None:
            return None
        return self.total_tokens / 1000 * self.cost_per_1k_tokens

    def check_budget(self) -> None:
        """Raise BudgetExceeded if the NEXT call would cross a limit."""
        if self.max_llm_calls is not None and self.calls >= self.max_llm_calls:
            raise BudgetExceeded(
                f"budget exceeded: {self.calls} model calls reached the limit of {self.max_llm_calls}"
            )
        if self.max_total_tokens is not None and self.total_tokens >= self.max_total_tokens:
            raise BudgetExceeded(
                f"budget exceeded: {self.total_tokens} tokens reached the limit of {self.max_total_tokens}"
            )

    def record(self, turn: Any, elapsed: float) -> None:
        self.calls += 1
        self.elapsed_seconds += elapsed
        usage = getattr(turn, "usage", None)
        if usage:
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
        self.tool_calls += len(getattr(turn, "tool_calls", []) or [])

    def report(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "max_total_tokens": self.max_total_tokens,
            "max_llm_calls": self.max_llm_calls,
        }
        if self.cost is not None:
            data["cost"] = round(self.cost, 6)
        return data

    def summary(self) -> str:
        parts = [
            f"{self.calls} model calls",
            f"{self.prompt_tokens}+{self.completion_tokens} tokens",
            f"{self.tool_calls} tool calls",
            f"{self.elapsed_seconds:.1f}s in model",
        ]
        if self.cost is not None:
            parts.append(f"cost {self.cost:.4f}")
        return "usage: " + ", ".join(parts)


def telemetry_plugin(config: dict[str, Any] | None = None) -> Plugin:
    """Mount the telemetry service and the budget-brake middleware."""
    settings = config or {}

    def apply(ctx: Context, _config: Any) -> None:
        telemetry = Telemetry(
            max_total_tokens=settings.get("max_total_tokens"),
            max_llm_calls=settings.get("max_llm_calls"),
            cost_per_1k_tokens=settings.get("cost_per_1k_tokens"),
        )

        def brake(payload: Any, next_call: Any) -> Any:
            telemetry.check_budget()
            start = time.monotonic()
            turn = next_call(payload)
            telemetry.record(turn, time.monotonic() - start)
            return turn

        ctx.intercept("llm/stream", brake)
        ctx.provide("telemetry", telemetry)

        def persist(_payload: Any) -> None:
            session = ctx.optional("session")
            if session:
                session.append({"type": "telemetry", **telemetry.report()})

        ctx.on("agent/task-end", persist)

    return Plugin("telemetry", apply)
