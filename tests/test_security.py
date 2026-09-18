import subprocess

import xharness.tools.security as security
from xharness.tools import ToolContext


def make_ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), approve=lambda _s: True)


def test_all_scanners_missing_reports_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    result = security.security_scan_tool().execute({"path": str(tmp_path)}, make_ctx(tmp_path))
    assert result.is_error
    assert "no scanners available" in result.output
    assert result.output.count("skipped (not installed)") == 3


def test_findings_from_available_scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(
        security.shutil, "which", lambda name: "/usr/bin/bandit" if name == "bandit" else None
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=">> Issue: [B602] shell=True", stderr="")

    monkeypatch.setattr(security.subprocess, "run", fake_run)
    result = security.security_scan_tool().execute({"path": str(tmp_path)}, make_ctx(tmp_path))
    assert not result.is_error
    assert "== bandit (Python SAST) == findings" in result.output
    assert "B602" in result.output
    assert "gitleaks" in result.output and "skipped" in result.output


def test_clean_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(
        security.shutil, "which", lambda name: "/usr/bin/" + name if name == "gitleaks" else None
    )
    monkeypatch.setattr(
        security.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    result = security.security_scan_tool().execute(
        {"path": str(tmp_path), "scanners": ["gitleaks"]}, make_ctx(tmp_path)
    )
    assert not result.is_error
    assert "== gitleaks (secret scan) == clean" in result.output


def test_bad_inputs(tmp_path):
    ctx = make_ctx(tmp_path)
    missing = security.security_scan_tool().execute({"path": str(tmp_path / "nope")}, ctx)
    assert missing.is_error
    unknown = security.security_scan_tool().execute({"scanners": ["nmap"]}, ctx)
    assert unknown.is_error and "unknown scanners" in unknown.output
