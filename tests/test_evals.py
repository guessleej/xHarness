import copy
import os

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.evals import format_report, load_cases, run_case, run_suite, write_results
from xharness.llm import AssistantTurn, Usage
from xharness.telemetry import telemetry_plugin
from xharness.tools import tools_plugin
from xharness.tools.bash import bash_tool_plugin
from xharness.tools.fs import fs_tools_plugin

CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")


class ScriptedAdapter:
    """Emits scripted turns; the write tool call targets whatever cwd the agent uses."""

    def __init__(self, turns):
        self.model = "fake"
        self.turns = copy.deepcopy(turns)

    def stream(self, messages, tools, on_delta=None):
        if not self.turns:
            return AssistantTurn(content="PASS")
        return self.turns.pop(0)


def factory_for(turns):
    def factory():
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.use("tool-fs", fs_tools_plugin)
        harness.use("tool-bash", bash_tool_plugin)
        harness.use("telemetry", telemetry_plugin())
        harness.ctx.provide("llm", ScriptedAdapter(turns))
        return harness

    return factory


def write_case(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_load_cases_from_directory_and_validation(tmp_path):
    write_case(tmp_path, "b.toml", 'prompt = "x"\n[[checks]]\nkind = "answer_contains"\ntext = "y"\n')
    write_case(tmp_path, "a.toml", 'id = "custom"\nprompt = "x"\n[[checks]]\nkind = "answer_contains"\ntext = "y"\n')
    cases = load_cases(str(tmp_path))
    assert [case["id"] for case in cases] == ["custom", "b"]
    write_case(tmp_path, "bad.toml", 'prompt = "no checks"\n')
    with pytest.raises(ValueError, match="no checks"):
        load_cases(str(tmp_path / "bad.toml"))
    with pytest.raises(ValueError):
        load_cases(str(tmp_path / "nope.txt"))


def test_case_passes_with_file_tool_and_command_checks():
    case = {
        "id": "write",
        "prompt": "create hello.txt",
        "setup": {"data/in.txt": "seed"},
        "checks": [
            {"kind": "file_contains", "path": "hello.txt", "pattern": "hello"},
            {"kind": "file_exists", "path": "data/in.txt"},
            {"kind": "tool_called", "name": "write"},
            {"kind": "command", "run": "test -f hello.txt && grep -q hello hello.txt"},
            {"kind": "answer_contains", "text": "done"},
        ],
    }
    turns = [
        AssistantTurn(
            content="",
            tool_calls=[{"id": "c1", "name": "write", "arguments": '{"path": "hello.txt", "content": "hello"}'}],
            usage=Usage(50, 10),
        ),
        AssistantTurn(content="done", usage=Usage(60, 5)),
    ]
    attempt = run_case(case, CONFIG, harness_factory=factory_for(turns))
    assert attempt.error is None
    assert attempt.passed, [c for c in attempt.checks if not c.ok]
    assert attempt.usage["total_tokens"] == 125
    assert attempt.usage["calls"] == 2


def test_failed_checks_are_reported_with_detail():
    case = {
        "id": "fail",
        "prompt": "x",
        "checks": [
            {"kind": "answer_regex", "pattern": "^never$"},
            {"kind": "file_exists", "path": "missing.txt"},
            {"kind": "file_exists", "path": "missing.txt", "absent": True},
            {"kind": "bogus"},
        ],
    }
    attempt = run_case(case, CONFIG, harness_factory=factory_for([AssistantTurn(content="hello")]))
    assert not attempt.passed
    results = {(c.kind, c.ok) for c in attempt.checks}
    assert ("answer_regex", False) in results
    assert ("file_exists", False) in results and ("file_exists", True) in results
    assert ("bogus", False) in results


def test_judge_check_uses_the_model():
    case = {"id": "judge", "prompt": "explain", "checks": [{"kind": "judge", "rubric": "mentions tests"}]}
    turns = [AssistantTurn(content="unit tests check units"), AssistantTurn(content="PASS")]
    attempt = run_case(case, CONFIG, harness_factory=factory_for(turns))
    assert attempt.passed
    turns = [AssistantTurn(content="nothing"), AssistantTurn(content="FAIL: irrelevant")]
    attempt = run_case(case, CONFIG, harness_factory=factory_for(turns))
    assert not attempt.passed


def test_broken_harness_becomes_error_not_crash():
    def exploding_factory():
        raise RuntimeError("cannot reach model")

    attempt = run_case({"id": "x", "prompt": "x", "checks": [{"kind": "answer_contains", "text": "y"}]}, CONFIG, harness_factory=exploding_factory)
    assert not attempt.passed and "cannot reach model" in attempt.error


def test_suite_report_and_jsonl(tmp_path):
    cases = [
        {"id": "ok", "prompt": "x", "checks": [{"kind": "answer_contains", "text": "hello"}]},
        {"id": "bad", "prompt": "x", "checks": [{"kind": "answer_contains", "text": "zzz"}]},
    ]
    reports = run_suite(cases, CONFIG, repeat=2, harness_factory=factory_for([AssistantTurn(content="hello")]))
    assert reports[0].passed and not reports[1].passed
    assert len(reports[0].attempts) == 2
    text = format_report(reports, "fake")
    assert "total: 1/2 cases passed (50%)" in text
    assert "failed answer_contains" in text
    out = tmp_path / "results.jsonl"
    write_results(reports, "fake", str(out))
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 4


def test_bundled_basic_suite_loads():
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "evals", "basic")
    cases = load_cases(root)
    assert len(cases) == 6
    assert all(case["checks"] for case in cases)
