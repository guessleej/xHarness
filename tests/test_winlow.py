"""Windows command protection: selection everywhere, behaviour on Windows itself."""
import os
import subprocess
import sys

import pytest

from xharness import sandbox as sb
from xharness.config import ResolvedConfig
from xharness.desktop import personal_defaults
from xharness.winlow import launcher_argv, main

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_windows_protection_is_opt_in_and_refuses_cutting_the_network(monkeypatch):
    monkeypatch.setattr(sb.sys, "platform", "win32")
    monkeypatch.setattr(sb, "_probe_winlow", lambda: True)
    assert sb.resolve_sandbox({"mode": "auto"}) is None                 # personal machines run unconfined
    chosen = sb.resolve_sandbox({"mode": "require"})
    assert chosen.name == "Windows Low integrity"
    assert chosen.wrap(["cmd.exe", "/c", "dir"], ".")[-3:] == ["cmd.exe", "/c", "dir"]
    with pytest.raises(RuntimeError, match="network"):
        sb.resolve_sandbox({"mode": "require", "allow_network": False})
    monkeypatch.setattr(sb, "_probe_winlow", lambda: False)
    with pytest.raises(RuntimeError, match="probe failed"):
        sb.resolve_sandbox({"mode": "require"})


def test_the_launcher_command_line(monkeypatch):
    assert launcher_argv("C:/w")[1:] == ["-m", "xharness.winlow", "--cwd", "C:/w", "--"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launcher_argv("C:/w") == [sys.executable, "--sandbox-launch", "--cwd", "C:/w", "--"]
    assert main(["--cwd", "x"]) == 2


def test_the_desktop_app_runs_unconfined_unless_the_config_says_otherwise():
    base = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "m"}, provider_name="p")
    assert personal_defaults(base).sandbox == {"mode": "off"}
    school = ResolvedConfig(**{**base.__dict__, "sandbox": {"mode": "require"}})
    assert personal_defaults(school).sandbox == {"mode": "require"}


def _run(tmp_path, *command):
    return subprocess.run(launcher_argv(str(tmp_path)) + list(command), capture_output=True, text=True,
                          cwd=str(tmp_path), timeout=60, env={**os.environ, "PYTHONPATH": os.getcwd()})


@pytest.fixture()
def workdir():
    """A folder made the ordinary way. pytest's tmp_path is created with mode 0o700, which on
    Python 3.13+ for Windows is an owner-only ACL -- and in an administrator's session the owner
    is Administrators, the very group the protected command loses."""
    import shutil
    import uuid
    path = os.path.join(os.path.expanduser("~"), f"xh-winlow-{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    yield __import__("pathlib").Path(path)
    shutil.rmtree(path, ignore_errors=True)


@on_windows
def test_runs_at_low_integrity_and_keeps_the_exit_code(workdir):
    tmp_path = workdir
    groups = _run(tmp_path, "whoami.exe", "/groups")
    assert groups.returncode == 0 and "S-1-16-4096" in groups.stdout          # Low Mandatory Level
    assert _run(tmp_path, "cmd.exe", "/c", "exit", "3").returncode == 3


@on_windows
def test_writes_land_in_the_working_directory_and_nowhere_else(workdir):
    tmp_path = workdir
    inside = _run(tmp_path, "cmd.exe", "/c", "echo inside> made.txt")
    assert inside.returncode == 0 and (tmp_path / "made.txt").read_text().strip() == "inside"
    target = os.path.join(os.path.expanduser("~"), "xh-low-should-not-exist.txt")
    if os.path.exists(target):
        os.remove(target)
    assert " " not in target, "the check below writes without quotes"
    # Without quotes: list2cmdline escapes inner quotes in a way cmd.exe misreads, and a command
    # that fails to parse would pass this test for the wrong reason.
    outside = _run(tmp_path, "cmd.exe", "/c", f"echo outside>{target}")
    assert outside.returncode != 0 and not os.path.exists(target)
    control = _run(tmp_path, "cmd.exe", "/c", f"echo inside>{tmp_path / 'same-syntax.txt'}")
    assert control.returncode == 0                                # the same syntax works where it may
