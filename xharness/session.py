"""Append-only JSONL session log under $XHARNESS_HOME/sessions/<id>.jsonl.

With [auth] on, each person's transcripts live in their own directory
($XHARNESS_HOME/sessions/u/<user>/) rather than a shared one, so one
signed-in user cannot read another's work by listing sessions, and usage
can be attributed per person without parsing every file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from .context import Context, Plugin

USER_ROOT = "u"
SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def harness_home() -> str:
    return os.environ.get("XHARNESS_HOME") or os.path.join(os.path.expanduser("~"), ".xharness")


def sessions_dir(user: str | None = None) -> str:
    base = os.path.join(harness_home(), "sessions")
    if not user:
        return base
    if not SAFE_USER.match(user):
        raise ValueError(f"unsafe user name for a session directory: {user!r}")
    return os.path.join(base, USER_ROOT, user)


def session_owners() -> list[tuple[str | None, str]]:
    """(user, directory) for every transcript store, shared one included."""
    base = os.path.join(harness_home(), "sessions")
    owners: list[tuple[str | None, str]] = [(None, base)]
    user_root = os.path.join(base, USER_ROOT)
    if os.path.isdir(user_root):
        for name in sorted(os.listdir(user_root)):
            path = os.path.join(user_root, name)
            if os.path.isdir(path) and SAFE_USER.match(name):
                owners.append((name, path))
    return owners


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionLog:
    def __init__(self, session_id: str | None = None, user: str | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.id = session_id or f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.user = user
        directory = sessions_dir(user)
        os.makedirs(directory, exist_ok=True)
        self._file = os.path.join(directory, f"{self.id}.jsonl")

    def append(self, event: dict[str, Any]) -> None:
        try:
            with open(self._file, "a", encoding="utf-8") as handle:
                record = {"ts": _now(), **event}
                if self.user and "user" not in record:
                    record["user"] = self.user
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            print(f"[xharness] session append failed: {error}", file=sys.stderr)

    @staticmethod
    def load(session_id: str, user: str | None = None) -> list[dict[str, Any]]:
        if not re.match(r"^[A-Za-z0-9._-]{1,128}$", session_id or ""):
            raise FileNotFoundError(f"session not found: {session_id}")
        file = os.path.join(sessions_dir(user), f"{session_id}.jsonl")
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
    def list(user: str | None = None) -> list[dict[str, Any]]:
        directory = sessions_dir(user)
        if not os.path.isdir(directory):
            return []
        entries = []
        for name in os.listdir(directory):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            entries.append({"id": name[: -len(".jsonl")], "mtime": os.path.getmtime(path)})
        return sorted(entries, key=lambda entry: entry["mtime"], reverse=True)


def messages_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild chat messages from logged events, for --resume."""
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("agent") not in (None, "main"):
            continue  # subagent traffic is logged for audit, not replayed
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


def session_plugin(
    resume_id: str | None = None,
    user: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Plugin:
    """`meta` is written as the first line of a new transcript (which model, which
    endpoint, whose session): usage reports read it back to tell self-hosted
    inference apart from a paid API without re-reading any configuration."""

    def apply(ctx: Context, _config: Any) -> None:
        log = SessionLog(resume_id, user)
        ctx.provide("session", log)
        if meta and not resume_id:
            log.append({"type": "meta", **meta})

    return Plugin("session", apply)
