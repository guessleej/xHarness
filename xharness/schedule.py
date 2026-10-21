"""Reminders inside a conversation: "check the training job again in 30
minutes", "every 10 minutes, report the queue length, six times".

The model sets them with schedule_add; when one is due and the conversation
is idle, the Web server sends it as a new request (marked as a reminder, so
the model knows nobody typed it). Reminders are written to the session log,
so a resumed conversation picks up the ones still pending, including any
that came due while it was closed.

Reminders only fire while something keeps the conversation alive -- the Web
UI (and the desktop app built on it). In the terminal the tools say so
instead of promising something nobody will deliver.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

MAX_PENDING = 20
MAX_HORIZON_DAYS = 7
MIN_EVERY_MINUTES = 5
MAX_REPEATS = 48


@dataclass
class Reminder:
    id: str
    due: float
    message: str
    every_minutes: float = 0
    remaining: int = 1

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "due": self.due, "message": self.message,
                "every_minutes": self.every_minutes, "remaining": self.remaining,
                "due_local": datetime.fromtimestamp(self.due).astimezone().strftime("%m/%d %H:%M")}


class Scheduler:
    def __init__(self) -> None:
        self.reminders: dict[str, Reminder] = {}
        self.deliverable = False      # set by a front end that delivers them (the Web UI)
        self._counter = 0
        self._lock = threading.Lock()
        #: The session log, looked up when needed (it is mounted after this plugin).
        self.session: Any = lambda: None
        self.on_change: Any = None

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change([r.public() for r in self.pending()])
            except Exception:  # noqa: BLE001 - a UI listener must not break scheduling  # nosec B110
                pass

    def pending(self) -> list[Reminder]:
        return sorted(self.reminders.values(), key=lambda r: r.due)

    def add(self, message: str, due: float, every_minutes: float = 0, times: int = 1, rid: str | None = None,
            record: bool = True) -> Reminder:
        with self._lock:
            if len(self.reminders) >= MAX_PENDING:
                raise ValueError(f"at most {MAX_PENDING} pending reminders")
            if rid is None:
                self._counter += 1
                rid = f"r{self._counter}"
            else:
                self._counter = max(self._counter, int(rid[1:]) if rid[1:].isdigit() else 0)
            reminder = Reminder(rid, due, message, every_minutes, max(1, times))
            self.reminders[rid] = reminder
        log = self.session()
        if record and log is not None:
            log.append({"type": "schedule-add", **reminder.public()})
        self._changed()
        return reminder

    def cancel(self, rid: str) -> Reminder | None:
        with self._lock:
            reminder = self.reminders.pop(rid, None)
        if reminder is not None:
            log = self.session()
            if log is not None:
                log.append({"type": "schedule-cancel", "id": rid})
            self._changed()
        return reminder

    def take_due(self, now: float | None = None) -> list[Reminder]:
        """Due reminders, advancing repeating ones (one firing per call)."""
        now = now or time.time()
        fired: list[Reminder] = []
        with self._lock:
            for reminder in list(self.reminders.values()):
                if reminder.due > now:
                    continue
                fired.append(Reminder(reminder.id, reminder.due, reminder.message, reminder.every_minutes, reminder.remaining))
                reminder.remaining -= 1
                if reminder.remaining > 0 and reminder.every_minutes:
                    reminder.due = max(now, reminder.due) + reminder.every_minutes * 60
                else:
                    self.reminders.pop(reminder.id, None)
        log = self.session()
        for reminder in fired:
            if log is not None:
                log.append({"type": "schedule-fired", "id": reminder.id})
        if fired:
            self._changed()
        return fired

    def restore(self, events: list[dict[str, Any]]) -> None:
        """Rebuild pending reminders from a session log."""
        state: dict[str, dict[str, Any]] = {}
        for event in events:
            kind = event.get("type")
            if kind == "schedule-add":
                state[str(event["id"])] = dict(event)
            elif kind == "schedule-cancel":
                state.pop(str(event.get("id")), None)
            elif kind == "schedule-fired" and str(event.get("id")) in state:
                entry = state[str(event["id"])]
                entry["remaining"] = int(entry.get("remaining") or 1) - 1
                if entry["remaining"] <= 0 or not entry.get("every_minutes"):
                    state.pop(str(event["id"]))
                else:
                    entry["due"] = float(entry["due"]) + float(entry["every_minutes"]) * 60
        for rid, entry in state.items():
            self.add(str(entry.get("message") or ""), float(entry.get("due") or time.time()),
                     float(entry.get("every_minutes") or 0), int(entry.get("remaining") or 1), rid=rid, record=False)


def parse_when(args: dict[str, Any], now: datetime | None = None) -> float:
    """in_minutes, or at = "HH:MM" (next occurrence) or an ISO date-time, in local time."""
    now = now or datetime.now().astimezone()
    if args.get("in_minutes") not in (None, ""):
        minutes = float(args["in_minutes"])
        if minutes < 1:
            raise ValueError("in_minutes must be at least 1")
        due = now + timedelta(minutes=minutes)
    elif args.get("at"):
        text = str(args["at"]).strip()
        try:
            if len(text) <= 5 and ":" in text:
                hour, minute = (int(part) for part in text.split(":"))
                due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if due <= now:
                    due += timedelta(days=1)
            else:
                due = datetime.fromisoformat(text)
                if due.tzinfo is None:
                    due = due.replace(tzinfo=now.tzinfo)
        except ValueError as error:
            raise ValueError("at must be HH:MM or an ISO date-time such as 2026-10-22T09:00") from error
        if due <= now:
            raise ValueError("that time has already passed")
    else:
        raise ValueError("give in_minutes or at")
    if due - now > timedelta(days=MAX_HORIZON_DAYS):
        raise ValueError(f"reminders can be at most {MAX_HORIZON_DAYS} days ahead")
    return due.timestamp()


def schedule_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        scheduler = Scheduler()
        ctx.provide("schedule", scheduler)
        scheduler.session = lambda: ctx.optional("session")

        def add(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            if not scheduler.deliverable:
                return ToolResult("reminders only fire while the Web UI keeps this conversation open; "
                                  "it is not available here, so tell the person instead of promising one", is_error=True)
            message = str(args.get("message") or "").strip()
            if not message:
                return ToolResult("message is required: what to do when the reminder fires", is_error=True)
            try:
                due = parse_when(args)
                every = float(args.get("every_minutes") or 0)
                times = int(args.get("times") or (1 if not every else 6))
                if every and every < MIN_EVERY_MINUTES:
                    raise ValueError(f"every_minutes must be at least {MIN_EVERY_MINUTES}")
                if times > MAX_REPEATS:
                    raise ValueError(f"times can be at most {MAX_REPEATS}")
                reminder = scheduler.add(message[:1000], due, every, times)
            except (ValueError, TypeError) as error:
                return ToolResult(str(error), is_error=True)
            repeat = f", then every {every:g} minutes, {times} times in all" if every else ""
            return ToolResult(f"reminder {reminder.id} set for {reminder.public()['due_local']}{repeat}. "
                              "When it fires you will receive it as a new request.")

        def listing(_args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            items = scheduler.pending()
            if not items:
                return ToolResult("no pending reminders")
            return ToolResult("\n".join(f"{r.id}: {r.public()['due_local']}"
                                        + (f" (every {r.every_minutes:g} min, {r.remaining} left)" if r.every_minutes else "")
                                        + f" -- {r.message}" for r in items))

        def cancel(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            rid = str(args.get("id") or "")
            return ToolResult(f"cancelled {rid}") if scheduler.cancel(rid) else ToolResult(f"no reminder {rid}", is_error=True)

        register_tools(ctx, [
            Tool("schedule_add", "Set a reminder for this conversation: it comes back to you as a new request "
                 "at the given time (in_minutes, or at = HH:MM / ISO date-time, local time), optionally repeating "
                 "every_minutes for `times` firings. Use it to check on long work later.",
                 {"type": "object", "properties": {
                     "message": {"type": "string", "description": "What to do when it fires"},
                     "in_minutes": {"type": "number"}, "at": {"type": "string"},
                     "every_minutes": {"type": "number"}, "times": {"type": "integer"}},
                  "required": ["message"]}, add),
            Tool("schedule_list", "List this conversation's pending reminders.", {"type": "object", "properties": {}}, listing),
            Tool("schedule_cancel", "Cancel a pending reminder.",
                 {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, cancel),
        ])

    return Plugin("schedule", apply)
