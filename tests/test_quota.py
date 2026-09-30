import json
from datetime import datetime, timedelta, timezone

import pytest

from xharness.context import Harness
from xharness.quota import QuotaExceeded, QuotaGuard, quota_plugin, spent_tokens, window_start
from xharness.session import sessions_dir
from xharness.telemetry import telemetry_plugin


def write_task(directory, session, tokens, when=None):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now(timezone.utc)).isoformat()
    path = directory / f"{session}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": stamp, "type": "telemetry", "total_tokens": tokens, "calls": 1}) + "\n")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    return tmp_path


def test_spent_reads_only_this_users_directory(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 500)
    write_task(home / "sessions" / "u" / "bob", "s2", 900)
    assert spent_tokens("alice", "day") == 500
    assert spent_tokens("bob", "day") == 900


def test_spent_ignores_earlier_windows(home):
    yesterday = window_start("day") - timedelta(hours=2)
    write_task(home / "sessions" / "u" / "alice", "old", 800, when=yesterday)
    write_task(home / "sessions" / "u" / "alice", "new", 200)
    assert spent_tokens("alice", "day") == 200


def test_excluded_session_is_not_double_counted(home):
    write_task(home / "sessions" / "u" / "alice", "live", 300)
    assert spent_tokens("alice", "day", exclude_session="live") == 0


def test_guard_allows_under_limit(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 100)
    QuotaGuard("alice", per_day=1000).check()


def test_guard_stops_at_limit(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 1000)
    with pytest.raises(QuotaExceeded) as error:
        QuotaGuard("alice", per_day=1000).check()
    assert "1000" in str(error.value)


def test_live_tokens_count_towards_the_limit(home):
    class FakeTelemetry:
        total_tokens = 900

    guard = QuotaGuard("alice", per_day=1000, telemetry=FakeTelemetry())
    write_task(home / "sessions" / "u" / "alice", "s1", 200)
    with pytest.raises(QuotaExceeded):
        guard.check()


def test_monthly_limit_independent_of_daily(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 5000)
    QuotaGuard("alice", per_day=None, per_month=6000).check()
    with pytest.raises(QuotaExceeded):
        QuotaGuard("alice", per_day=None, per_month=5000).check()


def test_no_limits_means_no_gate(home):
    guard = QuotaGuard("alice")
    assert guard.active is False
    guard.check()  # must not raise


def test_report_shape(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 250)
    report = QuotaGuard("alice", per_day=1000).report()
    assert report["day"]["used"] == 250
    assert report["day"]["remaining"] == 750
    assert report["month"]["limit"] is None


def test_plugin_intercepts_model_calls(home):
    write_task(home / "sessions" / "u" / "alice", "s1", 1000)
    harness = Harness()
    harness.use("telemetry", telemetry_plugin())
    harness.use("quota", quota_plugin("alice", per_day=1000))
    with pytest.raises(QuotaExceeded):
        harness.ctx.invoke("llm/stream", None, lambda payload: "should not run")


def test_plugin_without_limits_does_not_intercept(home):
    harness = Harness()
    harness.use("telemetry", telemetry_plugin())
    harness.use("quota", quota_plugin("alice"))
    assert harness.ctx.invoke("llm/stream", None, lambda payload: "ran") == "ran"


def test_unsafe_user_name_yields_zero_not_a_crash(home):
    assert spent_tokens("../../etc", "day") == 0
