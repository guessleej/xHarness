"""Usage history: tokens, calls, and tool calls over time, per node.

The source of truth is the session log on disk: the telemetry plugin appends
a cumulative report at the end of every task, so each session file yields a
per-task delta series that covers CLI, headless, Web UI, and subagent traffic
alike, whether or not a web server was running. A hub asks each node for its
own history and lines the series up on shared time buckets.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .session import sessions_dir

RANGE = re.compile(r"^(\d+)([hd])$")
FIELDS = ("total_tokens", "prompt_tokens", "completion_tokens", "calls", "tool_calls")


def parse_since(text: str) -> timedelta:
    """'24h', '7d', '30d' -> timedelta (default 7d)."""
    match = RANGE.match((text or "").strip().lower())
    if not match:
        return timedelta(days=7)
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(hours=amount) if unit == "h" else timedelta(days=amount)


def choose_bucket(since: timedelta) -> str:
    return "hour" if since <= timedelta(days=2) else "day"


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def task_records(directory: str | None, since: datetime) -> list[dict[str, Any]]:
    """Per-task usage deltas from every session log touched since `since`."""
    directory = directory or sessions_dir()
    if not os.path.isdir(directory):
        return []
    records: list[dict[str, Any]] = []
    for name in os.listdir(directory):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(directory, name)
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < since:
                continue
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        previous = {field: 0 for field in FIELDS}
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "telemetry":
                continue
            current = {field: int(event.get(field) or 0) for field in FIELDS}
            delta = {field: max(0, current[field] - previous[field]) for field in FIELDS}
            previous = current
            stamp = _parse_ts(event.get("ts"))
            if stamp is None or stamp < since:
                continue
            records.append({"ts": stamp.isoformat(), "session": name[:-6], **delta})
    records.sort(key=lambda record: record["ts"])
    return records


def _bucket_start(stamp: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return stamp.replace(minute=0, second=0, microsecond=0)
    return stamp.replace(hour=0, minute=0, second=0, microsecond=0)


def bucketize(records: list[dict[str, Any]], since: datetime, until: datetime, bucket: str) -> list[dict[str, Any]]:
    """Zero-filled buckets from `since` to `until`, each summing its task deltas."""
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    start = _bucket_start(since, bucket)
    buckets: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor <= until:
        key = cursor.isoformat()
        buckets[key] = {"start": key, "tasks": 0, **{field: 0 for field in FIELDS}}
        cursor += step
    for record in records:
        stamp = _parse_ts(record["ts"])
        if stamp is None:
            continue
        key = _bucket_start(stamp, bucket).isoformat()
        slot = buckets.get(key)
        if slot is None:
            continue
        slot["tasks"] += 1
        for field in FIELDS:
            slot[field] += int(record.get(field) or 0)
    return list(buckets.values())


def history(directory: str | None = None, since: str = "7d", bucket: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    span = parse_since(since)
    bucket = bucket if bucket in ("hour", "day") else choose_bucket(span)
    start = now - span
    records = task_records(directory, start)
    series = bucketize(records, start, now, bucket)
    totals = {field: sum(int(record.get(field) or 0) for record in records) for field in FIELDS}
    totals["tasks"] = len(records)
    totals["sessions"] = len({record["session"] for record in records})
    return {"since": since, "bucket": bucket, "from": start.isoformat(), "to": now.isoformat(), "series": series, "totals": totals}


def format_history(report: dict[str, Any], label: str = "local") -> str:
    totals = report["totals"]
    lines = [
        f"{label}: last {report['since']} by {report['bucket']} — {totals['tasks']} tasks in {totals['sessions']} sessions, "
        f"{totals['total_tokens']} tokens ({totals['prompt_tokens']} prompt + {totals['completion_tokens']} completion), "
        f"{totals['calls']} model calls, {totals['tool_calls']} tool calls"
    ]
    for slot in report["series"]:
        if slot["tasks"]:
            when = slot["start"][:13].replace("T", " ") if report["bucket"] == "hour" else slot["start"][:10]
            lines.append(f"    {when}  {slot['total_tokens']:>9} tok  {slot['calls']:>4} calls  {slot['tool_calls']:>4} tools  {slot['tasks']:>3} tasks")
    return "\n".join(lines)
