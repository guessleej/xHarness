"""Per-user spending caps across tasks.

The telemetry brake stops one runaway task. A quota stops one person from
spending the whole department's capacity over a day or a month, which is
what an IT office asks about before putting a shared GPU behind a harness.

Spending is read back from the session logs on disk, so a quota survives
restarts and covers CLI, Web UI, Telegram and subagent traffic alike. The
task currently in flight has not been written to disk yet, so its live
telemetry total is added on top, with the current session excluded from the
disk figure to avoid counting its finished tasks twice.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .context import Context, Plugin
from .session import sessions_dir
from .telemetry import BudgetExceeded
from .usage import task_records

DISK_CACHE_SECONDS = 60.0
WINDOWS = ("day", "month")


class QuotaExceeded(BudgetExceeded):
    """Raised before a model call that would take a user past their allowance."""


def window_start(window: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_tokens(user: str | None, window: str, exclude_session: str | None = None, now: datetime | None = None) -> int:
    """Tokens this user has already landed on disk in the current day or month."""
    start = window_start(window, now)
    try:
        directory = sessions_dir(user)
    except ValueError:
        return 0
    total = 0
    for record in task_records(directory, start):
        if exclude_session and record.get("session") == exclude_session:
            continue
        total += int(record.get("total_tokens") or 0)
    return total


class QuotaGuard:
    """Live view of one user's allowance; also feeds the quota chip in the UI."""

    def __init__(
        self,
        user: str,
        per_day: int | None = None,
        per_month: int | None = None,
        session_id: str | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.user = user
        self.limits = {"day": per_day or None, "month": per_month or None}
        self.session_id = session_id
        self.telemetry = telemetry
        self._cache: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return any(self.limits.values())

    def _disk(self, window: str) -> int:
        with self._lock:
            cached = self._cache.get(window)
            if cached and time.monotonic() - cached[0] < DISK_CACHE_SECONDS:
                return cached[1]
        total = spent_tokens(self.user, window, exclude_session=self.session_id)
        with self._lock:
            self._cache[window] = (time.monotonic(), total)
        return total

    def used(self, window: str) -> int:
        live = int(getattr(self.telemetry, "total_tokens", 0) or 0)
        return self._disk(window) + live

    def check(self) -> None:
        """Raise QuotaExceeded if the NEXT model call would cross an allowance."""
        for window in WINDOWS:
            limit = self.limits.get(window)
            if limit and self.used(window) >= limit:
                raise QuotaExceeded(
                    f"quota exceeded: {self.user} has used {self.used(window)} of {limit} tokens this {window}"
                )

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def report(self) -> dict[str, Any]:
        data: dict[str, Any] = {"user": self.user}
        for window in WINDOWS:
            limit = self.limits.get(window)
            used = self.used(window)
            data[window] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used) if limit else None,
                "resets": (window_start(window) + (timedelta(days=1) if window == "day" else timedelta(days=32))).isoformat(),
            }
        return data


def quota_plugin(user: str, per_day: int | None = None, per_month: int | None = None) -> Plugin:
    """Mount the quota service and put its check in front of every model call."""

    def apply(ctx: Context, _config: Any) -> None:
        session = ctx.optional("session")
        guard = QuotaGuard(
            user=user,
            per_day=per_day,
            per_month=per_month,
            session_id=getattr(session, "id", None),
            telemetry=ctx.optional("telemetry"),
        )
        ctx.provide("quota", guard)
        if not guard.active:
            return

        def gate(payload: Any, next_call: Any) -> Any:
            guard.check()
            return next_call(payload)

        ctx.intercept("llm/stream", gate)
        # A finished task has landed on disk; the next check must re-read it.
        ctx.on("agent/task-end", lambda _payload: guard.invalidate())

    return Plugin("quota", apply)
