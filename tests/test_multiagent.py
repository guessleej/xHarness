"""Separate workspaces with an integration gate, and the shared task board."""
import json
import os
import subprocess
import threading
import time

import pytest

from xharness import isolation
from xharness.agent import Agent, AgentOptions
from xharness.board import MAX_ATTEMPTS, Board, board_for, board_plugin, project_root
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.subagent import subagent_plugin
from xharness.tools import ToolContext, tools_plugin
from xharness.tools.fs import fs_tools_plugin


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "notes.txt").write_text("one\ntwo\nthree\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def yes(_summary):
    return True


# --- workspaces -------------------------------------------------------------

def test_snapshot_carries_uncommitted_and_untracked_files_and_leaves_the_index_alone(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # edited\n")
    (repo / "new.txt").write_text("not tracked yet\n")
    before = git(repo, "status", "--porcelain")
    ws = isolation.prepare(str(repo), "c1")
    try:
        assert "# edited" in (open(os.path.join(ws.path, "calc.py")).read())
        assert os.path.exists(os.path.join(ws.path, "new.txt"))
        assert not os.path.exists(os.path.join(ws.path, ".xharness", "worktrees"))
    finally:
        isolation.finish(ws, "", yes)
    assert git(repo, "status", "--porcelain") == before       # nothing staged, nothing committed
    assert git(repo, "log", "--oneline").count("\n") == 1


def test_a_change_that_passes_its_check_is_applied_uncommitted(repo):
    ws = isolation.prepare(str(repo), "fixer")
    open(os.path.join(ws.path, "calc.py"), "w").write("def add(a, b):\n    return a + b\n")
    out = isolation.finish(ws, "python3 -c 'import calc; assert calc.add(2, 3) == 5'", yes)
    assert out["status"] == "integrated" and out["check"]["exit"] == 0
    assert out["files"] == ["calc.py"]                          # the check's __pycache__ stays behind
    assert "a + b" in (repo / "calc.py").read_text()
    assert git(repo, "log", "--oneline").count("\n") == 1      # not committed for the person
    assert not os.path.exists(ws.path)


def test_a_change_that_fails_its_check_is_kept_as_a_branch_and_not_applied(repo):
    ws = isolation.prepare(str(repo), "breaker")
    open(os.path.join(ws.path, "calc.py"), "w").write("def add(a, b):\n    return a * b\n")
    out = isolation.finish(ws, "python3 -c 'import calc; assert calc.add(2, 3) == 5'", yes)
    assert out["status"] == "check-failed" and out["check"]["exit"] != 0
    assert "a - b" in (repo / "calc.py").read_text()
    assert out["branch"].startswith("xharness/breaker-")
    assert "a * b" in git(repo, "show", f"{out['branch']}:calc.py")
    assert "NOT applied" in isolation.describe(out, "breaker")


def test_two_siblings_on_the_same_line_the_second_is_a_conflict(repo):
    first, second = isolation.prepare(str(repo), "a"), isolation.prepare(str(repo), "b")
    open(os.path.join(first.path, "notes.txt"), "w").write("ONE\ntwo\nthree\n")
    open(os.path.join(second.path, "notes.txt"), "w").write("uno\ntwo\nthree\n")
    assert isolation.finish(first, "", yes)["status"] == "integrated"
    out = isolation.finish(second, "", yes)
    assert out["status"] == "conflict" and (repo / "notes.txt").read_text().startswith("ONE")


def test_declined_integration_and_no_changes(repo):
    ws = isolation.prepare(str(repo), "x")
    open(os.path.join(ws.path, "notes.txt"), "a").write("four\n")
    asked = []
    out = isolation.finish(ws, "", lambda s: asked.append(s) or False)
    assert out["status"] == "denied" and "four" not in (repo / "notes.txt").read_text()
    assert asked[0].startswith("integrate x: 1 file changed")
    assert isolation.finish(isolation.prepare(str(repo), "idle"), "", yes)["status"] == "no-changes"


def test_isolate_outside_a_repository_is_a_clear_error(tmp_path):
    with pytest.raises(isolation.IsolationError, match="git repository"):
        isolation.prepare(str(tmp_path), "c")


class WritingChild:
    """The child writes calc.py in its own working directory; the parent delegates with isolate."""

    def __init__(self, args):
        self.model = "fake"
        self.args = args
        self.child_cwd = None

    def stream(self, messages, tools, on_delta=None):
        system = messages[0]["content"]
        if "child agent" in system:
            if messages[-1]["role"] == "tool":
                return AssistantTurn(content="fixed add()", usage=Usage(5, 5))
            self.child_cwd = system.split("Working directory: ")[1].split("\n")[0]
            body = json.dumps({"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"})
            return AssistantTurn(content="", tool_calls=[{"id": "w", "name": "write", "arguments": body}], usage=Usage(5, 5))
        if messages[-1]["role"] == "tool":
            return AssistantTurn(content=messages[-1]["content"], usage=Usage(5, 5))
        return AssistantTurn(content="", tool_calls=[{"id": "s", "name": "subagent", "arguments": json.dumps(self.args)}],
                             usage=Usage(5, 5))


def test_subagent_with_isolate_works_elsewhere_and_its_change_arrives_after_the_check(repo):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("fs", fs_tools_plugin)
    harness.use("subagent", subagent_plugin())
    llm = WritingChild({"task": "fix add", "name": "fixer", "isolate": True,
                        "check": "python3 -c 'import calc; assert calc.add(1, 1) == 2'"})
    harness.ctx.provide("llm", llm)
    answer = Agent(harness.ctx, AgentOptions(approval_mode="auto", cwd=str(repo))).run("fix it")
    assert ".xharness/worktrees/fixer-" in llm.child_cwd
    assert "applied to the working directory" in answer and "exit 0" in answer
    assert "a + b" in (repo / "calc.py").read_text()


# --- the board ---------------------------------------------------------------

def test_only_one_agent_wins_a_claim(tmp_path):
    board = Board(str(tmp_path / "b.db"))
    board.add("lead", [("parse the log", "")])
    wins = []

    def grab(name):
        ok, _ = Board(str(tmp_path / "b.db")).claim(1, name, 30)
        if ok:
            wins.append(name)

    threads = [threading.Thread(target=grab, args=(f"a{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_claims_run_out_failures_are_retried_then_given_up(tmp_path):
    board = Board(str(tmp_path / "b.db"))
    board.add("lead", [("flaky job", "")])
    assert board.claim(1, "a", 30)[0]
    assert not board.heartbeat(1, "b", 30)[0]
    with board._db() as db:                                   # a's claim runs out
        db.execute("UPDATE tasks SET claim_until = ?", (time.time() - 1,))
    ok, text = board.claim(1, "b", 30)
    assert ok and "ran out" in text
    for agent in ("b", "c"):
        if agent == "c":
            assert board.claim(1, "c", 30)[0]
        assert board.release(1, agent, "failed", "timeout")[0]
    assert board.tasks()[0]["attempts"] == MAX_ATTEMPTS and board.tasks()[0]["state"] == "given-up"
    ok, text = board.claim(1, "d", 30)
    assert not ok and "leave it for the person" in text


def test_titles_are_not_posted_twice_while_open(tmp_path):
    board = Board(str(tmp_path / "b.db"))
    assert board.add("x", [("寫報表", ""), ("寫報表", "")]) == ([1], ["寫報表"])
    board.claim(1, "x", 30)
    board.release(1, "x", "done", "ok")
    assert board.add("y", [("寫報表", "")])[0] == [2]


def test_a_note_with_verify_records_what_the_system_ran(repo):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("board", board_plugin())
    tools = {t.name: t for t in harness.ctx.get("tools").list()}
    ctx = ToolContext(cwd=str(repo), approve=yes, agent="w1")
    tools["board_add"].execute({"tasks": [{"title": "fix add"}]}, ctx)
    claimed = tools["board_note"].execute({"kind": "success", "text": "tests pass", "task": 1}, ctx)
    assert "your own report" in claimed.output
    real = tools["board_note"].execute({"kind": "success", "text": "add is right", "task": 1,
                                        "verify": "python3 -c 'import calc; assert calc.add(2, 2) == 4'"}, ctx)
    assert real.is_error and "exit 1" in real.output        # the code is still wrong: the system says so
    listed = tools["board_notes"].execute({"task": 1}, ctx).output
    assert "verified by the system" in listed and "exit 1" in listed
    assert "the agent's own report (nothing was run)" in listed
    assert board_for(str(repo)).stats()["notes_verified"] == 1
    again = tools["board_note"].execute({"kind": "success", "text": "tests pass", "task": 1}, ctx)
    assert "not stored twice" in again.output


def test_children_in_worktrees_share_the_parents_board(repo):
    ws = isolation.prepare(str(repo), "c")
    try:
        assert os.path.realpath(project_root(ws.cwd)) == os.path.realpath(str(repo))
    finally:
        isolation.finish(ws, "", yes)
