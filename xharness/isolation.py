"""Separate workspaces for children that change code, and the gate their work
passes before it reaches the person's files.

With `isolate: true` a child works in a git worktree of its own, made from a
snapshot of the working directory as it is right now -- uncommitted and
untracked (not ignored) files included, taken through a scratch index so the
person's own index is never touched. Generated files (__pycache__, *.pyc,
node_modules, .pytest_cache) are left out both ways. When the child finishes:

1. its changes are committed inside the worktree (nothing outside it);
2. the `check` command, when given, runs in the worktree (approved like bash,
   sandboxed like bash) -- the system runs it, the child's word is not enough;
3. the change is applied to the working directory as uncommitted edits, after
   `git apply --check` shows it applies cleanly and the person approves.

A change that fails its check or no longer applies (a sibling changed the same
lines first) is not applied: it is kept as a branch `xharness/<name>-<id>` and
the result says how to look at it. Nothing is ever committed to the person's
branch; integrated work shows up in `git status` for them to review.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - git with fixed argv, and the approved check command
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable

GIT_TIMEOUT = 120
DEFAULT_CHECK_TIMEOUT = 600
MAX_CHECK_OUTPUT = 4000
IDENTITY = ["-c", "user.name=xharness", "-c", "user.email=xharness@localhost", "-c", "commit.gpgsign=false"]
#: Generated files are neither copied into a worktree nor carried back: running a test writes
#: __pycache__, and a change that "adds" bytecode is noise in the person's working directory.
GENERATED = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/.pytest_cache/**",
             ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.DS_Store"]
#: One integration at a time per process: siblings finishing together must apply in turn.
INTEGRATE_LOCK = threading.Lock()


class IsolationError(RuntimeError):
    pass


def _git(args: list[str], cwd: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(  # nosec B603 B607 - git, fixed argv, no shell
        ["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True, errors="replace",
        timeout=GIT_TIMEOUT, env={**os.environ, **(env or {})},
    )
    if check and done.returncode != 0:
        raise IsolationError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done


def repo_root(cwd: str) -> str | None:
    done = _git(["rev-parse", "--show-toplevel"], cwd, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def _ensure_ignored(root: str) -> None:
    directory = os.path.join(root, ".xharness")
    os.makedirs(directory, exist_ok=True)
    ignore = os.path.join(directory, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("*\n")


def snapshot(root: str) -> str:
    """A commit of the working tree as it is (tracked edits and untracked, not ignored, files)."""
    handle, scratch = tempfile.mkstemp(prefix="xh-index-")
    os.close(handle)
    os.unlink(scratch)          # git wants to create the index itself
    env = {"GIT_INDEX_FILE": scratch}
    try:
        head = _git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).stdout.strip()
        if head:
            _git(["read-tree", head], root, env)
        _git(["add", "-A", "--", ".", *GENERATED], root, env)
        tree = _git(["write-tree"], root, env).stdout.strip()
        parents = ["-p", head] if head else []
        return _git(["commit-tree", tree, *parents, "-m", "xharness snapshot"], root).stdout.strip()
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


@dataclass
class Workspace:
    root: str           # the person's repository (top level)
    cwd: str            # where the child works: the worktree, at the same relative place as the parent's cwd
    path: str           # the worktree itself
    base: str           # the snapshot it started from
    name: str
    tag: str

    @property
    def branch(self) -> str:
        return f"xharness/{self.name}-{self.tag}"


def prepare(cwd: str, name: str) -> Workspace:
    root = repo_root(cwd)
    if root is None:
        raise IsolationError("isolate needs a git repository, and the working directory is not in one")
    _ensure_ignored(root)
    base = snapshot(root)
    tag = secrets.token_hex(3)
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:40] or "child"
    path = os.path.join(root, ".xharness", "worktrees", f"{safe}-{tag}")
    _git(["worktree", "add", "--detach", path, base], root)
    relative = os.path.relpath(os.path.realpath(cwd), os.path.realpath(root))
    child_cwd = os.path.normpath(os.path.join(path, relative)) if relative != "." else path
    os.makedirs(child_cwd, exist_ok=True)
    return Workspace(root, child_cwd, path, base, safe, tag)


def _remove(ws: Workspace) -> None:
    _git(["worktree", "remove", "--force", ws.path], ws.root, check=False)
    if os.path.isdir(ws.path):
        shutil.rmtree(ws.path, ignore_errors=True)
    _git(["worktree", "prune"], ws.root, check=False)


def _keep(ws: Workspace, commit: str) -> str:
    _git(["branch", "-f", ws.branch, commit], ws.root)
    return ws.branch


def finish(ws: Workspace, check: str, approve: Callable[[str], bool], sandbox: Any = None,
           check_timeout: float = DEFAULT_CHECK_TIMEOUT) -> dict[str, Any]:
    """Commit the child's work, run the check, apply it. Returns what happened, for the parent and the log.

    status: "no-changes" | "integrated" | "check-failed" | "conflict" | "denied"
    """
    try:
        _git(["add", "-A", "--", ".", *GENERATED], ws.path)
        if not _git(["diff", "--cached", "--name-only"], ws.path).stdout.strip():
            return {"status": "no-changes"}
        _git(["commit", "-q", "-m", f"xharness: {ws.name}"], ws.path)
        commit = _git(["rev-parse", "HEAD"], ws.path).stdout.strip()
        stat = _git(["diff", "--shortstat", ws.base, commit], ws.root).stdout.strip()
        files = _git(["diff", "--name-only", ws.base, commit], ws.root).stdout.split()
        out: dict[str, Any] = {"commit": commit[:12], "stat": stat, "files": files[:50]}
        if check:
            out["check"] = run_check(ws.path, check, approve, sandbox, check_timeout, ws.name)
            if out["check"]["exit"] != 0:
                out["status"], out["branch"] = "check-failed", _keep(ws, commit)
                return out
        patch = _git(["diff", "--binary", ws.base, commit], ws.root).stdout
        with INTEGRATE_LOCK:
            probe = subprocess.run(  # nosec B603 B607 - git apply --check, fixed argv
                ["git", "apply", "--check", "-"], cwd=ws.root, input=patch, capture_output=True, text=True,
                timeout=GIT_TIMEOUT)
            if probe.returncode != 0:
                out["status"], out["branch"] = "conflict", _keep(ws, commit)
                out["detail"] = probe.stderr.strip()[:600]
                return out
            if not approve(f"integrate {ws.name}: {stat} ({', '.join(files[:6])}{' …' if len(files) > 6 else ''}) "
                           "into the working directory, uncommitted"):
                out["status"], out["branch"] = "denied", _keep(ws, commit)
                return out
            applied = subprocess.run(  # nosec B603 B607 - git apply, fixed argv
                ["git", "apply", "-"], cwd=ws.root, input=patch, capture_output=True, text=True, timeout=GIT_TIMEOUT)
            if applied.returncode != 0:
                out["status"], out["branch"] = "conflict", _keep(ws, commit)
                out["detail"] = applied.stderr.strip()[:600]
                return out
        out["status"] = "integrated"
        return out
    finally:
        _remove(ws)


def run_check(cwd: str, command: str, approve: Callable[[str], bool], sandbox: Any = None,
              timeout: float = DEFAULT_CHECK_TIMEOUT, label: str = "") -> dict[str, Any]:
    """Run a verification command the way bash runs one; the exit code is the system's, not an agent's claim."""
    if not approve(f"check{' ' + label if label else ''}: {command}"):
        return {"command": command, "exit": -1, "output": "denied by approval policy", "ran": False}
    argv = ["bash", "-c", command]
    if sandbox is not None:
        argv = sandbox.wrap(argv, cwd)
    try:
        done = subprocess.run(  # nosec B603 B607 - an approved command, sandboxed when available
            argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
        output, code = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip(), done.returncode
    except subprocess.TimeoutExpired:
        output, code = f"timed out after {timeout:g}s", 124
    except OSError as error:
        output, code = f"cannot run: {error}", 127
    if len(output) > MAX_CHECK_OUTPUT:
        output = "…" + output[-MAX_CHECK_OUTPUT:]
    return {"command": command, "exit": code, "output": output, "ran": True}


def describe(result: dict[str, Any], name: str) -> str:
    """The lines the parent reads after the child's answer."""
    status = result.get("status")
    check = result.get("check")
    checked = (f" check `{check['command']}` exit {check['exit']}" if check and check.get("ran")
               else (" check not run (denied)" if check else ""))
    if status == "no-changes":
        return f"[workspace {name}] no file changes."
    if status == "integrated":
        return f"[workspace {name}] applied to the working directory (uncommitted): {result['stat']}.{checked}"
    hint = f" Kept as branch {result.get('branch')}; see it with `git diff HEAD...{result.get('branch')}`."
    if status == "check-failed":
        tail = (check or {}).get("output", "")[-800:]
        return f"[workspace {name}] NOT applied: the check failed ({checked.strip()}).{hint}\n{tail}"
    if status == "conflict":
        return f"[workspace {name}] NOT applied: it no longer applies cleanly (another change touched the same lines).{hint}\n{result.get('detail', '')}"
    if status == "denied":
        return f"[workspace {name}] NOT applied: the person declined.{hint}"
    return f"[workspace {name}] {result}"
