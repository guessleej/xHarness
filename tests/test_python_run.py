"""python_run: prints come back, secrets stay out, limits hold, approval gates."""
import os
import sys

import pytest

from xharness.config import ResolvedConfig
from xharness.presets import build_harness
from xharness.tools import ToolContext
from xharness.tools.python_run import SECRET_ENV, child_env, find_interpreter, python_run_tool


@pytest.fixture()
def tool():
    return python_run_tool(sys.executable, memory_mb=512)


def ctx(tmp_path, approve=True, asked=None):
    def policy(summary):
        if asked is not None:
            asked.append(summary)
        return approve
    return ToolContext(cwd=str(tmp_path), approve=policy)


def test_prints_come_back_and_run_in_the_working_directory(tool, tmp_path):
    (tmp_path / "sales.csv").write_text("name,amount\n王,1200\n李,800\n", encoding="utf-8")
    code = ("import csv\nrows=list(csv.DictReader(open('sales.csv',encoding='utf-8')))\n"
            "print(sum(int(r['amount']) for r in rows), rows[0]['name'])")
    result = tool.execute({"code": code}, ctx(tmp_path))
    assert not result.is_error and result.output.strip() == "2000 王"


def test_the_approval_shows_the_code_and_a_no_runs_nothing(tool, tmp_path):
    asked = []
    marker = tmp_path / "ran"
    result = tool.execute({"code": f"open({str(marker)!r},'w').write('x')"}, ctx(tmp_path, False, asked))
    assert result.is_error and not marker.exists() and "open(" in asked[0] and asked[0].startswith("python_run:")


def test_secrets_are_not_in_the_childs_environment(tool, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-leak")
    monkeypatch.setenv("XHARNESS_CHANNELS_KEY_FILE", "/run/secrets/x")
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    code = "import os\nprint(sorted(k for k in os.environ if k in ('OPENAI_API_KEY','GITHUB_TOKEN','XHARNESS_CHANNELS_KEY_FILE','HARMLESS_SETTING')))"
    assert tool.execute({"code": code}, ctx(tmp_path)).output.strip() == "['HARMLESS_SETTING']"
    assert SECRET_ENV.search("DB_PASSWORD") and not SECRET_ENV.search("KEYBOARD_LAYOUT")
    assert "PATH" in child_env()


def test_errors_timeouts_and_empty_output(tool, tmp_path):
    failed = tool.execute({"code": "raise SystemExit(3)"}, ctx(tmp_path))
    assert failed.is_error and "[exit code 3]" in failed.output
    trace = tool.execute({"code": "1/0"}, ctx(tmp_path))
    assert trace.is_error and "ZeroDivisionError" in trace.output and "<snippet>" in trace.output
    assert "xh-py-" not in trace.output
    slow = tool.execute({"code": "import time; time.sleep(5)", "timeout_seconds": 0.5}, ctx(tmp_path))
    assert slow.is_error and "timed out" in slow.output
    assert "print what you want" in tool.execute({"code": "x = 1"}, ctx(tmp_path)).output
    assert tool.execute({"code": "  "}, ctx(tmp_path)).is_error


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
def test_memory_ceiling(tool, tmp_path):
    result = tool.execute({"code": "b = bytearray(1024 * 1024 * 1024)\nprint('got it')"}, ctx(tmp_path))
    assert result.is_error and "MemoryError" in result.output and "512 MB" in result.output


def test_the_snippet_file_is_removed(tool, tmp_path):
    out = tool.execute({"code": "print(__file__)"}, ctx(tmp_path)).output.strip()
    assert not os.path.exists(out)


def test_interpreter_choice(monkeypatch):
    assert find_interpreter() == sys.executable
    assert find_interpreter("no-such-python-xyz") is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert find_interpreter() is not None      # the packaged app falls back to the PATH


def test_offered_by_default_and_configurable(tmp_path):
    base = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "m"}, provider_name="p")
    assert build_harness(base, no_session=True).ctx.get("tools").get("python_run") is not None
    off = ResolvedConfig(**{**base.__dict__, "python": "no-such-python-xyz"})
    assert build_harness(off, no_session=True).ctx.get("tools").get("python_run") is None
