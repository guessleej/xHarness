"""Sandbox: confine the bash tool with OS facilities that are already installed.

macOS uses sandbox-exec (Seatbelt), Linux uses bubblewrap (bwrap) when present.
Writes are confined to the working directory, temp dirs, and /dev; network can
be cut with `allow_network = false`. Mode "auto" falls back to bare execution
when no backend exists; mode "require" refuses to start instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from typing import Any, Callable

from .context import Context, Plugin

Wrap = Callable[[list[str], str], list[str]]


class Sandbox:
    def __init__(self, name: str, wrap: Wrap) -> None:
        self.name = name
        #: Build the argv that runs `argv` confined to `cwd`.
        self.wrap = wrap


def _seatbelt_path(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'(subpath "{escaped}")'


def _seatbelt_wrap(allow_network: bool) -> Wrap:
    def wrap(argv: list[str], cwd: str) -> list[str]:
        writable = [
            os.path.realpath(cwd),
            "/private/tmp",
            "/private/var/tmp",
            os.path.realpath(tempfile.gettempdir()),
            "/dev",
        ]
        rules = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            "(allow file-write* " + " ".join(_seatbelt_path(p) for p in writable) + ")",
        ]
        if not allow_network:
            rules.append("(deny network*)")
        return ["sandbox-exec", "-p", "\n".join(rules)] + argv

    return wrap


def _bwrap_wrap(allow_network: bool) -> Wrap:
    def wrap(argv: list[str], cwd: str) -> list[str]:
        real_cwd = os.path.realpath(cwd)
        wrapped = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--bind", real_cwd, real_cwd,
            # A bind mount target, not a temp file we create: commands need a
            # writable /tmp inside the sandbox.
            "--bind", "/tmp", "/tmp",  # nosec B108
            "--die-with-parent",
        ]
        if not allow_network:
            wrapped.append("--unshare-net")
        return wrapped + argv

    return wrap


def _probe(argv: list[str]) -> bool:
    """Functionally probe a backend: an installed binary is not a working one.

    Real case: bwrap exists but the kernel forbids unprivileged user
    namespaces ("setting up uid map: Permission denied"), so every wrapped
    command would fail. Selecting on `which` alone breaks the bash tool.
    """
    try:
        return (
            # Fixed probe argv, no user input.
            subprocess.run(  # nosec B603
                argv, capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_seatbelt() -> bool:
    return _probe(["sandbox-exec", "-p", "(version 1)(allow default)", "true"])


def _probe_bwrap() -> bool:
    return _probe(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"]
    )


def resolve_sandbox(config: dict[str, Any] | None) -> Sandbox | None:
    config = config or {}
    mode = str(config.get("mode", "auto"))
    if mode not in ("auto", "require", "off"):
        raise ValueError("sandbox mode must be auto, require, or off")
    if mode == "off":
        return None
    allow_network = bool(config.get("allow_network", True))
    broken: list[str] = []
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        if _probe_seatbelt():
            return Sandbox("sandbox-exec", _seatbelt_wrap(allow_network))
        broken.append("sandbox-exec (installed but probe failed)")
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        if _probe_bwrap():
            return Sandbox("bwrap", _bwrap_wrap(allow_network))
        broken.append("bwrap (installed but probe failed, e.g. user namespaces disabled)")
    if mode == "require":
        detail = "; ".join(broken) if broken else "need sandbox-exec (macOS) or bwrap (Linux)"
        raise RuntimeError(f"sandbox required but no working backend: {detail}")
    return None


def sandbox_plugin(config: dict[str, Any] | None = None) -> Plugin:
    def _apply(ctx: Context, _config: Any) -> None:
        sandbox = resolve_sandbox(config)
        if sandbox is not None:
            ctx.provide("sandbox", sandbox)

    return Plugin("sandbox", _apply)
