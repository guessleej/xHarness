"""Append-only JSONL session log under $XHARNESS_HOME/sessions/<id>.jsonl."""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from .context import Context, Plugin


def harness_home() -> str:
    return os.environ.get("XHARNESS_HOME") or os.path.join(os.path.expanduser("~"), ".xharness")


def sessions_dir() -> str:
    return os.path.join(harness_home(), "sessions")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionLog:
    def __init__(self, session_id: str | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.id = session_id or f"{stamp}-{uuid.uuid4().hex[:8]}"
        os.makedirs(sessions_dir(), exist_ok=True)
        self._file = os.path.join(sessions_dir(), f"{self.id}.jsonl")

    def append(self, event: dict[str, Any]) -> None:
        try:
            with open(self._file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": _now(), **event}, ensure_ascii=False) + "\n")
        except OSError as error:
            print(f"[xharness] session append failed: {error}", file=sys.stderr)

    @staticmethod
    def load(session_id: str) -> list[dict[str, Any]]:
        file = os.path.join(sessions_dir(), f"{session_id}.jsonl")
        if not os.path.exists(file):
            raise FileNotFoundError(f"session not found: {session_id}")
        events: list[dict[str, Any]] = []
        with open(file, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip corrupt lines rather than losing the whole session
        return events

    @staticmethod
    def list() -> list[dict[str, Any]]:
        if not os.path.isdir(sessions_dir()):
            return []
        entries = []
        for name in os.listdir(sessions_dir()):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(sessions_dir(), name)
            entries.append({"id": name[: -len(".jsonl")], "mtime": os.path.getmtime(path)})
        return sorted(entries, key=lambda entry: entry["mtime"], reverse=True)


def messages_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild chat messages from logged events, for --resume."""
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "message":
            message: dict[str, Any] = {
                "role": event.get("role", "user"),
                "content": str(event.get("content") or ""),
            }
            if event.get("tool_calls"):
                message["tool_calls"] = event["tool_calls"]
            messages.append(message)
        elif event.get("type") == "tool-result":
            messages.append(
                {
                    "role": "tool",
                    "content": str(event.get("output") or ""),
                    "tool_call_id": str(event.get("call_id") or ""),
                }
            )
    return messages


def session_plugin(resume_id: str | None = None) -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        ctx.provide("session", SessionLog(resume_id))

    return Plugin("session", apply)
