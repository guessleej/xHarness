import os
import shutil
import subprocess
import sys

import pytest

from xharness.sandbox import _bwrap_wrap, _seatbelt_wrap, resolve_sandbox
from xharness.tools import ToolContext
from xharness.tools.bash import bash_tool


def test_mode_off_and_unknown_mode():
    assert resolve_sandbox({"mode": "off"}) is None
    with pytest.raises(ValueError):
        resolve_sandbox({"mode": "chroot"})


def test_mode_require_without_backend(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="sandbox required"):
        resolve_sandbox({"mode": "require"})
    assert resolve_sandbox({"mode": "auto"}) is None


def test_wrappers_build_expected_argv(tmp_path):
    cwd = str(tmp_path)
    seatbelt = _seatbelt_wrap(allow_network=False)(["bash", "-c", "true"], cwd)
    assert seatbelt[0] == "sandbox-exec" and seatbelt[-3:] == ["bash", "-c", "true"]
    assert "(deny network*)" in seatbelt[2]
    assert os.path.realpath(cwd) in seatbelt[2]

    bwrap = _bwrap_wrap(allow_network=False)(["bash", "-c", "true"], cwd)
    assert bwrap[0] == "bwrap" and "--unshare-net" in bwrap
    assert os.path.realpath(cwd) in bwrap


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)
def test_seatbelt_confines_writes_for_real(tmp_path):
    sandbox = resolve_sandbox({})
    assert sandbox is not None and sandbox.name == "sandbox-exec"
    ctx = ToolContext(cwd=str(tmp_path), approve=lambda _s: True)
    tool = bash_tool(sandbox=sandbox)

    inside = tool.execute({"command": "echo ok > inside.txt && cat inside.txt"}, ctx)
    assert not inside.is_error and "ok" in inside.output

    outside_path = os.path.expanduser("~/.xharness-sandbox-test-should-not-exist")
    outside = tool.execute({"command": f"echo leak > {outside_path}"}, ctx)
    leaked = os.path.exists(outside_path)
    if leaked:
        os.unlink(outside_path)
    assert outside.is_error and not leaked


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)
def test_seatbelt_network_cut(tmp_path):
    sandbox = resolve_sandbox({"allow_network": False})
    argv = sandbox.wrap(["bash", "-c", "curl -s --max-time 5 http://example.com"], str(tmp_path))
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert completed.returncode != 0
