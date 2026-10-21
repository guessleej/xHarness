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
    """Rebuild chat messages from logged events, for --resume.

    "context" events (the system prompt a run started with) are not replayed:
    a resumed session gets today's system prompt, and logs that one.
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("agent") not in (None, "main"):
            continue  # subagent traffic is logged for audit, not replayed
        if event.get("type") == "message":
            content = str(event.get("content") or "")
            if event.get("context"):
                content = f"{content}\n\n[{event['context']}]"  # as agent.with_context sent it
            message: dict[str, Any] = {"role": event.get("role", "user"), "content": content}
            if event.get("tool_calls"):
                message["tool_calls"] = event["tool_calls"]
            if event.get("images"):
                message["images"] = list(event["images"])
            messages.append(message)
        elif event.get("type") == "compaction":
            # Same cut the agent made: summary and acknowledgement, then what it kept.
            from .agent import SUMMARY_ACK, SUMMARY_PREFIX

            kept = int(event.get("kept") or 0)
            tail = messages[-kept:] if kept else []
            messages = [{"role": "user", "content": SUMMARY_PREFIX + str(event.get("summary") or "")},
                        {"role": "assistant", "content": SUMMARY_ACK}] + tail
            continue
        elif event.get("type") == "tool-result":
            messages.append(
                {
                    "role": "tool",
                    "content": str(event.get("output") or ""),
                    "tool_call_id": str(event.get("call_id") or ""),
                }
            )
    return messages


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_SEARCH_FILES = 400
MAX_TITLE_CHARS = 40


def _session_path(session_id: str, user: str | None) -> str | None:
    if not SESSION_ID_RE.match(session_id or ""):
        return None
    path = os.path.join(sessions_dir(user), f"{session_id}.jsonl")
    return path if os.path.isfile(path) else None


def _events(path: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def _taipei(iso: str) -> str:
    """A logged UTC timestamp as Taipei local time, which is how people read it."""
    from datetime import timedelta

    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def clean_title(text: str) -> str:
    """One line, no surrounding quotes or trailing punctuation, bounded."""
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    line = re.sub(r"^(title|標題|标题)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
    line = line.strip(" \"'「」『』“”*#`").rstrip("。.!！?？")
    return line[:MAX_TITLE_CHARS]


def session_summary(session_id: str, user: str | None) -> dict[str, str]:
    """Title (latest title event) and first request, for task lists."""
    path = _session_path(session_id, user)
    title, first = "", ""
    for event in _events(path) if path else []:
        if event.get("agent") not in (None, "main"):
            continue
        if event.get("type") == "title" and event.get("title"):
            title = str(event["title"])
        elif not first and event.get("type") == "message" and event.get("role") == "user":
            first = str(event.get("content") or "")[:80]
    return {"title": title, "preview": first}


def search_sessions(query: str, user: str | None, limit: int = 30) -> list[dict[str, Any]]:
    """Sessions whose requests, replies or title contain `query` (case-insensitive)."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    results: list[dict[str, Any]] = []
    for entry in SessionLog.list(user)[:MAX_SEARCH_FILES]:
        path = _session_path(entry["id"], user)
        if not path:
            continue
        title, first, hit = "", "", ""
        for event in _events(path):
            if event.get("agent") not in (None, "main"):
                continue
            kind = event.get("type")
            text = str(event.get("title") if kind == "title" else event.get("content") or "")
            if kind == "title":
                title = text
            elif kind == "message" and event.get("role") == "user" and not first:
                first = text[:80]
            if kind in ("title", "message") and not hit:
                at = text.lower().find(needle)
                if at >= 0:
                    start = max(0, at - 30)
                    hit = ("…" if start else "") + text[start: at + len(needle) + 50].replace("\n", " ")
        if hit:
            results.append({"id": entry["id"], "mtime": entry["mtime"], "title": title, "preview": first, "snippet": hit})
            if len(results) >= limit:
                break
    return results


def export_markdown(session_id: str, user: str | None) -> str | None:
    """The conversation as a readable document: requests, replies, the tools used."""
    path = _session_path(session_id, user)
    if not path:
        return None
    events = _events(path)
    title = next((str(e["title"]) for e in reversed(events) if e.get("type") == "title" and e.get("title")), "")
    meta = next((e for e in events if e.get("type") == "meta"), {})
    lines = [f"# {title or session_id}", ""]
    lines.append(f"- session：{session_id}")
    stamp = _taipei(str(events[0].get("ts", ""))) if events else ""
    if stamp:
        lines.append(f"- 開始：{stamp}（台北時間）")
    if meta.get("model"):
        lines.append(f"- 模型：{meta['model']}")
    lines.append("")
    for event in events:
        if event.get("agent") not in (None, "main"):
            continue
        kind = event.get("type")
        if kind == "message" and event.get("role") == "user":
            lines += ["## 提問", "", str(event.get("content") or ""), ""]
        elif kind == "message" and event.get("role") == "assistant":
            calls = event.get("tool_calls") or []
            if calls:
                lines += [f"- 工具 `{c.get('name')}` {str(c.get('arguments') or '')[:160]}" for c in calls]
                lines.append("")
            if event.get("content"):
                lines += ["## 回覆", "", str(event.get("content")), ""]
        elif kind == "compaction":
            lines += ["## 較早內容的摘要", "", str(event.get("summary") or ""), ""]
        elif kind == "discussion-start":
            mode = DISCUSSION_MODES.get(str(event.get("mode")), str(event.get("mode")))
            people = "、".join(
                str(p.get("name")) + (f"（{p.get('team')}{'・代表' if p.get('spokesperson') else ''}）" if p.get("team")
                                      else f"（{p.get('stance') or p.get('role')}）" if p.get("stance") or p.get("role") else "")
                for p in event.get("participants") or [])
            lines += [f"## {mode}：{event.get('topic')}", "", f"- 討論編號：{event.get('room')}", f"- 參與者：{people}",
                      f"- 上限：{event.get('rounds')} 輪、{int(event.get('max_tokens') or 0):,} token", ""]
        elif kind == "discussion-turn":
            used = f"（查了 {'、'.join(event['tools'])}）" if event.get("tools") else ""
            where = (f"・{event.get('team')}{'組內' if event.get('scope') == 'caucus' else '代表發言'}" if event.get("team") else "")
            lines += [f"**第 {event.get('round')} 輪{where}・{event.get('speaker')}**{used}　{_taipei(str(event.get('ts', '')))}",
                      "", str(event.get("text") or ""), ""]
            if event.get("unverified"):
                lines += [f"> 未查證：{'、'.join(event['unverified'])} 在查到的資料裡找不到原文"
                          + ("（已要求先查證）" if event.get("rechecked") else ""), ""]
            if event.get("uncited"):
                lines += [f"> 未查證出處：{'、'.join(event['uncited'])}", ""]
        elif kind == "discussion-note":
            lines += [f"> {event.get('text')}", ""]
        elif kind == "discussion-end":
            lines += [f"### 討論結論（{event.get('turns')} 次發言、{int(event.get('tokens') or 0):,} token、"
                      f"結束原因：{event.get('reason')}）", "", str(event.get("summary") or ""), ""]
    runs = [e for e in events if e.get("type") == "agent-run"]
    if runs:
        lines += ["## 代理執行紀錄", "", "| 代理 | 角色 | 類型 | 狀態 | token | 模型呼叫 | 秒數 |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {r.get('name')} | {r.get('role') or '—'} | {r.get('kind')} | {r.get('status')} | "
                  f"{int(r.get('tokens') or 0):,} | {r.get('calls')} | {r.get('seconds')} |" for r in runs]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


DISCUSSION_MODES = {"roundtable": "圓桌討論", "debate": "辯論", "review": "審查", "free": "自由討論", "teams": "組對組"}


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
