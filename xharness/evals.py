"""Eval subsystem: run scored task suites against a model + harness config.

A suite is a directory of TOML case files. Each case gives the agent a task
in a fresh temp workspace and scores the outcome with deterministic checks
(files, answers, commands, tool usage) plus an optional LLM judge. Every
case gets a fresh harness, so telemetry numbers are per case.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import Agent, AgentOptions
from .config import ResolvedConfig
from .context import Harness
from .presets import build_harness

DEFAULT_MAX_TURNS = 15
COMMAND_TIMEOUT_SECONDS = 120

HarnessFactory = Callable[[], Harness]


@dataclass
class CheckResult:
    kind: str
    ok: bool
    detail: str = ""


@dataclass
class AttemptResult:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0
    usage: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class CaseReport:
    case_id: str
    attempts: list[AttemptResult]

    @property
    def passed(self) -> bool:
        return all(attempt.passed for attempt in self.attempts)


def load_cases(path: str) -> list[dict[str, Any]]:
    """Load one .toml case file or every *.toml in a directory (sorted)."""
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, name) for name in os.listdir(path) if name.endswith(".toml")
        )
        if not files:
            raise ValueError(f"no .toml case files in {path}")
    elif path.endswith(".toml"):
        files = [path]
    else:
        raise ValueError(f"eval path must be a .toml file or a directory: {path}")

    cases: list[dict[str, Any]] = []
    for file in files:
        with open(file, "rb") as handle:
            case = tomllib.load(handle)
        case.setdefault("id", os.path.splitext(os.path.basename(file))[0])
        if not case.get("prompt"):
            raise ValueError(f"case {case['id']} has no prompt")
        checks = case.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"case {case['id']} has no checks")
        cases.append(case)
    return cases


def _read_workspace_file(workspace: str, rel: str) -> str | None:
    try:
        with open(os.path.join(workspace, rel), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _judge(harness: Harness, prompt: str, answer: str, rubric: str) -> tuple[bool, str]:
    llm = harness.ctx.get("llm")
    question = (
        f"任務：{prompt}\n\n回答：{answer}\n\n評分標準：{rubric}\n\n"
        "若回答符合評分標準，只回覆 PASS；否則只回覆 FAIL。"
    )
    turn = llm.stream([{"role": "user", "content": question}], [])
    verdict = turn.content.strip().upper()
    ok = "PASS" in verdict and "FAIL" not in verdict
    return ok, turn.content.strip()[:200]


def _run_check(
    check: dict[str, Any],
    case: dict[str, Any],
    answer: str,
    workspace: str,
    harness: Harness,
    agent: Agent,
) -> CheckResult:
    kind = str(check.get("kind") or "")
    absent = bool(check.get("absent"))
    flags = re.IGNORECASE if check.get("ignore_case") else 0
    try:
        if kind == "answer_contains":
            found = str(check["text"]) in answer
            return CheckResult(kind, found != absent, f"text={check['text']!r}")
        if kind == "answer_regex":
            found = re.search(str(check["pattern"]), answer, flags) is not None
            return CheckResult(kind, found != absent, f"pattern={check['pattern']!r}")
        if kind == "file_exists":
            found = os.path.exists(os.path.join(workspace, str(check["path"])))
            return CheckResult(kind, found != absent, f"path={check['path']}")
        if kind == "file_contains":
            text = _read_workspace_file(workspace, str(check["path"]))
            if text is None:
                return CheckResult(kind, absent, f"path={check['path']} (missing)")
            found = re.search(str(check["pattern"]), text, flags) is not None
            return CheckResult(kind, found != absent, f"path={check['path']} pattern={check['pattern']!r}")
        if kind == "command":
            # Case files are author-trusted, like test code; runs in the workspace.
            completed = subprocess.run(  # nosec B603 B607
                ["bash", "-c", str(check["run"])],
                cwd=workspace,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                text=True,
                errors="replace",
            )
            detail = f"run={check['run']!r} exit={completed.returncode}"
            if completed.returncode != 0:
                detail += f" stderr={completed.stderr.strip()[:200]!r}"
            return CheckResult(kind, (completed.returncode == 0) != absent, detail)
        if kind == "tool_called":
            called = {
                call["name"]
                for message in agent.messages
                if message.get("role") == "assistant"
                for call in message.get("tool_calls") or []
            }
            found = str(check["name"]) in called
            return CheckResult(kind, found != absent, f"name={check['name']} called={sorted(called)}")
        if kind == "judge":
            ok, verdict = _judge(harness, str(case["prompt"]), answer, str(check["rubric"]))
            return CheckResult(kind, ok, f"verdict={verdict!r}")
        return CheckResult(kind or "?", False, "unknown check kind")
    except KeyError as error:
        return CheckResult(kind, False, f"check missing field {error}")
    except (re.error, subprocess.TimeoutExpired, OSError) as error:
        return CheckResult(kind, False, f"check failed: {error}")


def run_case(
    case: dict[str, Any],
    config: ResolvedConfig,
    harness_factory: HarnessFactory | None = None,
) -> AttemptResult:
    workspace = tempfile.mkdtemp(prefix="xharness-eval-")
    try:
        for rel, content in (case.get("setup") or {}).items():
            target = os.path.join(workspace, rel)
            os.makedirs(os.path.dirname(target) or workspace, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(str(content))
        harness = harness_factory() if harness_factory else build_harness(config, no_session=True)
        try:
            agent = Agent(
                harness.ctx,
                AgentOptions(
                    approval_mode="auto",
                    cwd=workspace,
                    max_turns=int(case.get("max_turns", DEFAULT_MAX_TURNS)),
                    project_instructions=False,
                ),
            )
            start = time.monotonic()
            answer = agent.run(str(case["prompt"]))
            seconds = time.monotonic() - start
            checks = [
                _run_check(check, case, answer, workspace, harness, agent)
                for check in case["checks"]
            ]
            telemetry = harness.ctx.optional("telemetry")
            usage = telemetry.report() if telemetry else None
            return AttemptResult(
                passed=all(check.ok for check in checks),
                checks=checks,
                answer=answer,
                seconds=round(seconds, 2),
                usage=usage,
            )
        finally:
            harness.dispose()
    except Exception as error:  # noqa: BLE001 - one broken case must not kill the suite
        return AttemptResult(passed=False, error=f"{type(error).__name__}: {error}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_suite(
    cases: list[dict[str, Any]],
    config: ResolvedConfig,
    repeat: int = 1,
    harness_factory: HarnessFactory | None = None,
    on_attempt: Callable[[str, int, AttemptResult], None] | None = None,
) -> list[CaseReport]:
    reports: list[CaseReport] = []
    for case in cases:
        attempts: list[AttemptResult] = []
        for index in range(max(1, repeat)):
            attempt = run_case(case, config, harness_factory)
            attempts.append(attempt)
            if on_attempt:
                on_attempt(str(case["id"]), index, attempt)
        reports.append(CaseReport(case_id=str(case["id"]), attempts=attempts))
    return reports


def format_report(reports: list[CaseReport], model: str) -> str:
    lines = [f"eval report — model: {model}", ""]
    width = max([len(report.case_id) for report in reports] + [4])
    lines.append(f"{'case'.ljust(width)}  result  checks  tokens  time")
    for report in reports:
        ok_attempts = sum(1 for attempt in report.attempts if attempt.passed)
        total = len(report.attempts)
        result = "PASS" if report.passed else "FAIL"
        if total > 1:
            result += f" {ok_attempts}/{total}"
        first = report.attempts[0]
        checks = f"{sum(1 for c in first.checks if c.ok)}/{len(first.checks)}" if first.checks else "-"
        tokens = str(first.usage.get("total_tokens", "-")) if first.usage else "-"
        seconds = f"{first.seconds:.1f}s" if not first.error else "error"
        lines.append(f"{report.case_id.ljust(width)}  {result.ljust(6)}  {checks.ljust(6)}  {tokens.ljust(6)}  {seconds}")
        for attempt in report.attempts:
            if attempt.error:
                lines.append(f"    error: {attempt.error[:160]}")
            for check in attempt.checks:
                if not check.ok:
                    lines.append(f"    failed {check.kind}: {check.detail[:160]}")
    passed = sum(1 for report in reports if report.passed)
    lines.append("")
    lines.append(f"total: {passed}/{len(reports)} cases passed ({passed * 100 // max(1, len(reports))}%)")
    return "\n".join(lines)


def write_results(reports: list[CaseReport], model: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for report in reports:
            for index, attempt in enumerate(report.attempts):
                handle.write(
                    json.dumps(
                        {
                            "case": report.case_id,
                            "attempt": index,
                            "model": model,
                            "passed": attempt.passed,
                            "checks": [check.__dict__ for check in attempt.checks],
                            "seconds": attempt.seconds,
                            "usage": attempt.usage,
                            "error": attempt.error,
                            "answer": attempt.answer[:2000],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
