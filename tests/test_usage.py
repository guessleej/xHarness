import json
from datetime import datetime, timedelta, timezone

from xharness.usage import bucketize, choose_bucket, format_history, history, parse_since, task_records

NOW = datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc)


def write_session(directory, name, events):
    path = directory / f"{name}.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def telemetry(ts, total, calls, tools):
    return {"ts": ts.isoformat(), "type": "telemetry", "total_tokens": total, "prompt_tokens": total - 10, "completion_tokens": 10, "calls": calls, "tool_calls": tools}


def test_parse_since_and_bucket_choice():
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("30d") == timedelta(days=30)
    assert parse_since("nonsense") == timedelta(days=7)
    assert choose_bucket(timedelta(hours=24)) == "hour" and choose_bucket(timedelta(days=7)) == "day"


def test_task_records_are_per_task_deltas(tmp_path):
    t0 = NOW - timedelta(hours=3)
    write_session(tmp_path, "s1", [
        {"ts": t0.isoformat(), "type": "message", "role": "user", "content": "a"},
        telemetry(t0 + timedelta(minutes=1), 1000, 2, 1),   # first task: 1000 tokens
        telemetry(t0 + timedelta(minutes=30), 1600, 3, 2),  # second task in the same REPL: +600
        {"ts": "garbage"},
    ])
    records = task_records(str(tmp_path), NOW - timedelta(days=1))
    assert [(r["total_tokens"], r["calls"], r["tool_calls"]) for r in records] == [(1000, 2, 1), (600, 1, 1)]
    assert records[0]["session"] == "s1"
    assert task_records(str(tmp_path / "missing"), NOW) == []


def test_since_filters_old_events_but_keeps_delta_baseline(tmp_path):
    old = NOW - timedelta(days=3)
    write_session(tmp_path, "s2", [telemetry(old, 500, 1, 0), telemetry(NOW - timedelta(hours=1), 900, 2, 1)])
    records = task_records(str(tmp_path), NOW - timedelta(days=1))
    assert [r["total_tokens"] for r in records] == [400]  # the old task is excluded, the baseline still applies


def test_bucketize_zero_fills_and_sums(tmp_path):
    since = NOW - timedelta(hours=5)
    records = [
        {"ts": (NOW - timedelta(hours=4, minutes=10)).isoformat(), "total_tokens": 100, "prompt_tokens": 90, "completion_tokens": 10, "calls": 1, "tool_calls": 0},
        {"ts": (NOW - timedelta(hours=4, minutes=5)).isoformat(), "total_tokens": 50, "prompt_tokens": 40, "completion_tokens": 10, "calls": 1, "tool_calls": 1},
        {"ts": (NOW - timedelta(minutes=5)).isoformat(), "total_tokens": 7, "prompt_tokens": 0, "completion_tokens": 7, "calls": 1, "tool_calls": 0},
    ]
    series = bucketize(records, since, NOW, "hour")
    assert len(series) == 6  # 07:00 (floored from 07:30) .. 12:00
    assert series[1]["tasks"] == 2 and series[1]["total_tokens"] == 150 and series[1]["tool_calls"] == 1
    assert series[-1]["total_tokens"] == 7
    assert all(slot["tasks"] == 0 for index, slot in enumerate(series) if index not in (1, 5))


def test_history_report_and_formatting(tmp_path):
    write_session(tmp_path, "s3", [telemetry(NOW - timedelta(hours=2), 300, 1, 1), telemetry(NOW - timedelta(hours=1), 800, 2, 3)])
    report = history(str(tmp_path), since="24h", now=NOW)
    assert report["bucket"] == "hour" and report["totals"]["total_tokens"] == 800
    assert report["totals"]["tasks"] == 2 and report["totals"]["sessions"] == 1
    text = format_history(report, "local")
    assert "2 tasks in 1 sessions" in text and "800 tokens" in text
    assert report["series"][-2]["total_tokens"] == 500 or any(slot["total_tokens"] == 500 for slot in report["series"])
    day = history(str(tmp_path), since="30d", now=NOW)
    assert day["bucket"] == "day" and len(day["series"]) == 31
