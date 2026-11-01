"""The task board: who is doing what, what was found, and what was verified --
shared by every agent working on the same project.

Several agents on one goal waste most of their effort redoing each other's
work: two take the same task, one walks a dead end another already found.
The board gives them a common record:

- tasks: `board_add` posts them; `board_claim` takes one, for a limited time
  (default 30 minutes, kept alive with `board_heartbeat`); a claim nobody
  renews runs out and the task is open again; `board_release` ends it as done,
  failed (another agent may retry, up to 3 attempts) or back to open.
- notes: `board_note` records a finding, a success or a failure, with its
  evidence and the project version it applies to. A note that names a `verify`
  command gets it run by the system (approved and sandboxed like bash) and
  carries the real exit code -- "verified by the system", as opposed to
  "the agent says so". That distinction is the point: an agent's report that
  the tests pass is a claim until something runs them.

One SQLite file per project, `.xharness/board.db` at the top of the
repository (children in worktrees share their parent's), so agents in several
conversations, or several xharness processes, on the same machine see one
board. ponytail: one machine; agents on other fleet nodes need a board server.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess  # nosec B404 - git rev-parse with a fixed argv
import time
from typing import Any

from . import isolation
from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

DEFAULT_CLAIM_MINUTES = 30
MAX_CLAIM_MINUTES = 240
MAX_ATTEMPTS = 3
MAX_TASKS_PER_ADD = 30
MAX_TITLE = 200
MAX_TEXT = 1000
MAX_LISTED = 60
NOTE_KINDS = ("finding", "success", "failure")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', claimed_by TEXT NOT NULL DEFAULT '', claim_until REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, last_claimed_by TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, author TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '', exit INTEGER,
  output TEXT NOT NULL DEFAULT '', verified TEXT NOT NULL DEFAULT 'agent', version TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL);
"""


def project_root(cwd: str) -> str:
    """The main repository's top level (also from inside one of its worktrees), else cwd."""
    try:
        done = subprocess.run(  # nosec B603 B607 - git, fixed argv
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return cwd
    common = done.stdout.strip()
    if done.returncode == 0 and os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return cwd


def project_version(cwd: str) -> str:
    try:
        done = subprocess.run(  # nosec B603 B607 - git, fixed argv
            ["git", "describe", "--always", "--dirty"], cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


class Board:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def add(self, author: str, items: list[tuple[str, str]]) -> tuple[list[int], list[str]]:
        now, added, skipped = time.time(), [], []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            open_titles = {r["title"].casefold() for r in db.execute("SELECT title FROM tasks WHERE status != 'done'")}
            for title, detail in items:
                if title.casefold() in open_titles:
                    skipped.append(title)
                    continue
                cursor = db.execute("INSERT INTO tasks (title, detail, created_by, created, updated) VALUES (?,?,?,?,?)",
                                    (title, detail, author, now, now))
                added.append(int(cursor.lastrowid))
                open_titles.add(title.casefold())
            db.execute("COMMIT")
        return added, skipped

    def claim(self, task_id: int, agent: str, minutes: float) -> tuple[bool, str]:
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")      # one claimant wins, across processes too
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return False, f"no task {task_id}"
            state = self._state(row, now)
            if state == "claimed" and row["claimed_by"] != agent:
                db.execute("ROLLBACK")
                left = max(1, round((row["claim_until"] - now) / 60))
                return False, f"task {task_id} is being worked on by {row['claimed_by']} ({left} more minutes); pick another"
            if state == "done":
                db.execute("ROLLBACK")
                return False, f"task {task_id} is already done"
            if state == "given-up":
                db.execute("ROLLBACK")
                return False, f"task {task_id} failed {row['attempts']} times; leave it for the person"
            attempts = row["attempts"] + (0 if row["claimed_by"] == agent and state == "claimed" else 1)
            db.execute("UPDATE tasks SET status='claimed', claimed_by=?, last_claimed_by=?, claim_until=?, attempts=?, "
                       "updated=? WHERE id=?", (agent, agent, now + minutes * 60, attempts, now, task_id))
            db.execute("COMMIT")
        before = ""
        if row["status"] == "failed":
            before = f" (attempt {attempts} of {MAX_ATTEMPTS}; the last attempt by {row['last_claimed_by']} failed: " \
                     f"{row['result'][:200] or 'no reason given'} -- see board_notes for what was tried)"
        elif row["status"] == "claimed" and row["last_claimed_by"] != agent:
            before = f" (previously claimed by {row['last_claimed_by']}, whose claim ran out)"
        return True, f"claimed task {task_id} for {minutes:g} minutes{before}; renew with board_heartbeat if it takes longer"

    def heartbeat(self, task_id: int, agent: str, minutes: float) -> tuple[bool, str]:
        now = time.time()
        with self._db() as db:
            cursor = db.execute("UPDATE tasks SET claim_until=?, updated=? WHERE id=? AND status='claimed' AND claimed_by=?",
                                (now + minutes * 60, now, task_id, agent))
        if cursor.rowcount:
            return True, f"task {task_id} is yours for another {minutes:g} minutes"
        return False, f"task {task_id} is not claimed by you (it may have run out; claim it again)"

    def release(self, task_id: int, agent: str, status: str, result: str) -> tuple[bool, str]:
        now = time.time()
        new = {"done": "done", "failed": "failed", "open": "open"}.get(status)
        if new is None:
            return False, "status is done, failed or open"
        with self._db() as db:
            cursor = db.execute("UPDATE tasks SET status=?, claimed_by='', claim_until=0, result=?, updated=? "
                                "WHERE id=? AND status='claimed' AND claimed_by=?", (new, result, now, task_id, agent))
        if not cursor.rowcount:
            return False, f"task {task_id} is not claimed by you"
        return True, f"task {task_id} is {new}"

    @staticmethod
    def _state(row: sqlite3.Row, now: float) -> str:
        status = row["status"]
        if status == "claimed" and row["claim_until"] < now:
            return "given-up" if row["attempts"] >= MAX_ATTEMPTS else "open"
        if status == "failed":
            return "given-up" if row["attempts"] >= MAX_ATTEMPTS else "open"
        return status

    def tasks(self, status: str = "") -> list[dict[str, Any]]:
        now = time.time()
        with self._db() as db:
            rows = db.execute("SELECT t.*, (SELECT COUNT(*) FROM notes n WHERE n.task_id = t.id) AS notes "
                              "FROM tasks t ORDER BY t.id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["state"] = self._state(row, now)
            item["minutes_left"] = max(0, round((row["claim_until"] - now) / 60)) if item["state"] == "claimed" else 0
            if not status or item["state"] == status:
                out.append(item)
        return out

    def note(self, author: str, kind: str, text: str, task_id: int | None, evidence: str, version: str,
             check: dict[str, Any] | None) -> tuple[int, bool]:
        """(note id, was it new). The same author repeating the same note on the same task is stored once."""
        with self._db() as db:
            same = db.execute("SELECT id FROM notes WHERE author=? AND kind=? AND text=? AND task_id IS ? AND command=?",
                              (author, kind, text, task_id, (check or {}).get("command", ""))).fetchone()
            if same is not None and check is None:
                return int(same["id"]), False
            cursor = db.execute(
                "INSERT INTO notes (task_id, author, kind, text, evidence, command, exit, output, verified, version, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, author, kind, text, evidence, (check or {}).get("command", ""),
                 (check or {}).get("exit") if check and check.get("ran") else None,
                 (check or {}).get("output", "")[-2000:],
                 "system" if check and check.get("ran") else "agent", version, time.time()))
            return int(cursor.lastrowid), True

    def notes(self, task_id: int | None = None, kind: str = "", query: str = "", limit: int = 30) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM notes WHERE 1=1", []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if query:
            sql += " AND (text LIKE ? OR evidence LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._db() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def stats(self) -> dict[str, int]:
        tasks = self.tasks()
        with self._db() as db:
            verified = db.execute("SELECT COUNT(*) FROM notes WHERE verified='system'").fetchone()[0]
            passed = db.execute("SELECT COUNT(*) FROM notes WHERE verified='system' AND exit=0").fetchone()[0]
            claimed_only = db.execute("SELECT COUNT(*) FROM notes WHERE verified='agent'").fetchone()[0]
        count = lambda state: sum(1 for t in tasks if t["state"] == state)  # noqa: E731
        return {"tasks": len(tasks), "done": count("done"), "open": count("open"), "claimed": count("claimed"),
                "given_up": count("given-up"), "notes_verified": verified, "notes_verified_ok": passed,
                "notes_claimed": claimed_only}


def board_snapshot(cwd: str) -> dict[str, Any]:
    """What the Web UI shows; reads an existing board only, never creates one."""
    path = os.path.join(project_root(cwd), ".xharness", "board.db")
    if not os.path.exists(path):
        return {"tasks": [], "notes": [], "stats": {}}
    board = Board(path)
    keep = ("id", "title", "state", "claimed_by", "minutes_left", "attempts", "result", "notes")
    notes = ("id", "task_id", "author", "kind", "text", "command", "exit", "verified", "version")
    return {"tasks": [{k: t[k] for k in keep} for t in board.tasks()][:MAX_LISTED],
            "notes": [{k: n[k] for k in notes} for n in board.notes(limit=20)], "stats": board.stats()}


def board_for(cwd: str) -> Board:
    return Board(os.path.join(project_root(cwd), ".xharness", "board.db"))


def _identity(ctx: Context, tool_ctx: ToolContext) -> str:
    session = ctx.optional("session")
    return f"{tool_ctx.agent}@{session.id[-8:]}" if session is not None and getattr(session, "id", "") else tool_ctx.agent


def _task_line(t: dict[str, Any]) -> str:
    state = t["state"]
    extra = {"claimed": f" — {t['claimed_by']}, {t['minutes_left']} min left",
             "open": f" — attempt {t['attempts'] + 1} of {MAX_ATTEMPTS}" if t["attempts"] else "",
             "given-up": f" — failed {t['attempts']} times",
             "done": f" — {t['result'][:120]}" if t["result"] else ""}.get(state, "")
    notes = f" [{t['notes']} notes]" if t["notes"] else ""
    return f"#{t['id']} [{state}] {t['title']}{extra}{notes}"


def _note_line(n: dict[str, Any]) -> str:
    if n["verified"] == "system":
        proof = f"verified by the system: `{n['command']}` exit {n['exit']}"
    else:
        proof = "the agent's own report (nothing was run)"
    where = f" task #{n['task_id']}" if n["task_id"] else ""
    version = f" @{n['version']}" if n["version"] else ""
    evidence = f"\n    evidence: {n['evidence'][:300]}" if n["evidence"] else ""
    return f"N{n['id']} {n['kind']}{where}{version} by {n['author']}: {n['text']}\n    {proof}{evidence}"


def _make_tools(ctx: Context) -> list[Tool]:
    def number(value: Any, name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a task number, e.g. 3") from error

    def minutes(args: dict[str, Any]) -> float:
        return max(1.0, min(float(args.get("minutes") or DEFAULT_CLAIM_MINUTES), MAX_CLAIM_MINUTES))

    def guarded(run: Any) -> Any:
        def execute(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            try:
                return run(args, tool_ctx, board_for(tool_ctx.cwd), _identity(ctx, tool_ctx))
            except ValueError as error:
                return ToolResult(str(error), is_error=True)
            except sqlite3.Error as error:
                return ToolResult(f"board unavailable: {error}", is_error=True)
        return execute

    def add(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, me: str) -> ToolResult:
        raw = args.get("tasks")
        if not isinstance(raw, list) or not raw:
            return ToolResult('tasks must be a list, e.g. [{"title": "Write the parser"}]', is_error=True)
        items = []
        for item in raw[:MAX_TASKS_PER_ADD]:
            item = {"title": item} if isinstance(item, str) else item
            title = str((item or {}).get("title") or "").strip()[:MAX_TITLE] if isinstance(item, dict) else ""
            if not title:
                return ToolResult("every task needs a title", is_error=True)
            items.append((title, str(item.get("detail") or "")[:MAX_TEXT]))
        added, skipped = board.add(me, items)
        lines = [f"added {', '.join('#' + str(i) for i in added) or 'nothing'}"]
        if skipped:
            lines.append("already on the board (not added again): " + "; ".join(skipped))
        return ToolResult("\n".join(lines))

    def listing(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, _me: str) -> ToolResult:
        status = str(args.get("status") or "").strip()
        tasks = board.tasks(status)
        if not tasks:
            return ToolResult("the board has no tasks" + (f" that are {status}" if status else ""))
        shown = tasks[:MAX_LISTED]
        more = f"\n({len(tasks) - len(shown)} more)" if len(tasks) > len(shown) else ""
        return ToolResult("\n".join(_task_line(t) for t in shown) + more)

    def claim(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, me: str) -> ToolResult:
        ok, text = board.claim(number(args.get("task"), "task"), me, minutes(args))
        return ToolResult(text, is_error=not ok)

    def heartbeat(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, me: str) -> ToolResult:
        ok, text = board.heartbeat(number(args.get("task"), "task"), me, minutes(args))
        return ToolResult(text, is_error=not ok)

    def release(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, me: str) -> ToolResult:
        ok, text = board.release(number(args.get("task"), "task"), me, str(args.get("status") or ""),
                                 str(args.get("result") or "")[:MAX_TEXT])
        return ToolResult(text, is_error=not ok)

    def note(args: dict[str, Any], tool_ctx: ToolContext, board: Board, me: str) -> ToolResult:
        kind = str(args.get("kind") or "finding")
        if kind not in NOTE_KINDS:
            return ToolResult(f"kind is one of: {', '.join(NOTE_KINDS)}", is_error=True)
        text = str(args.get("text") or "").strip()[:MAX_TEXT]
        if not text:
            return ToolResult("text is required: what was found, done or failed", is_error=True)
        task_id = number(args["task"], "task") if args.get("task") not in (None, "") else None
        command = str(args.get("verify") or "").strip()
        check = isolation.run_check(tool_ctx.cwd, command, tool_ctx.approve, ctx.optional("sandbox"), label="board") \
            if command else None
        note_id, new = board.note(me, kind, text, task_id, str(args.get("evidence") or "")[:MAX_TEXT],
                                  project_version(tool_ctx.cwd), check)
        if not new:
            return ToolResult(f"N{note_id} already says this; not stored twice")
        if check is None:
            return ToolResult(f"noted N{note_id} (recorded as your own report; add verify to have the system run a check)")
        if not check["ran"]:
            return ToolResult(f"noted N{note_id}; the verify command was not run (denied)")
        return ToolResult(f"noted N{note_id}; the system ran `{command}`: exit {check['exit']}\n{check['output'][-1500:]}",
                          is_error=check["exit"] != 0 and kind == "success")

    def notes(args: dict[str, Any], _tool_ctx: ToolContext, board: Board, _me: str) -> ToolResult:
        task_id = number(args["task"], "task") if args.get("task") not in (None, "") else None
        found = board.notes(task_id, str(args.get("kind") or ""), str(args.get("query") or "").strip())
        if not found:
            return ToolResult("no notes match")
        return ToolResult("\n".join(_note_line(n) for n in found))

    task_param = {"type": "integer", "description": "Task number from board_list"}
    return [
        Tool("board_list", "List the tasks on the project's shared board: open, claimed (by whom, minutes left), "
             "done, given-up. Look here before starting work, so you do not redo what another agent is doing.",
             {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "claimed", "done", "given-up"]}}},
             guarded(listing)),
        Tool("board_add", "Post tasks to the shared board for agents to claim. Titles already open are not added twice.",
             {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "object", "properties": {
                 "title": {"type": "string"}, "detail": {"type": "string"}}, "required": ["title"]}}},
              "required": ["tasks"]}, guarded(add)),
        Tool("board_claim", "Take a task before working on it, so no other agent takes it too. The claim runs out after "
             f"minutes (default {DEFAULT_CLAIM_MINUTES}) unless renewed with board_heartbeat.",
             {"type": "object", "properties": {"task": task_param, "minutes": {"type": "number"}}, "required": ["task"]},
             guarded(claim)),
        Tool("board_heartbeat", "Renew your claim on a task you are still working on.",
             {"type": "object", "properties": {"task": task_param, "minutes": {"type": "number"}}, "required": ["task"]},
             guarded(heartbeat)),
        Tool("board_release", "Finish your claim: status done (with a short result), failed (another agent may retry, "
             f"up to {MAX_ATTEMPTS} attempts in all) or open (give it back).",
             {"type": "object", "properties": {"task": task_param, "status": {"type": "string", "enum": ["done", "failed", "open"]},
                                               "result": {"type": "string"}}, "required": ["task", "status"]},
             guarded(release)),
        Tool("board_note", "Record a finding, success or failure for the other agents, with evidence. Give verify (a "
             "shell command such as a test run) to have the system run it and record the real exit code; without it "
             "the note counts only as your own report.",
             {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": list(NOTE_KINDS)}, "text": {"type": "string"},
                 "task": task_param, "evidence": {"type": "string", "description": "What shows it: output, file, line"},
                 "verify": {"type": "string", "description": "Command the system runs to check it"}},
              "required": ["kind", "text"]}, guarded(note)),
        Tool("board_notes", "Read what agents have found, done and failed (newest first), each marked as verified by "
             "the system or only reported by an agent. Check before trying an approach someone may already have tried.",
             {"type": "object", "properties": {"task": task_param, "kind": {"type": "string", "enum": list(NOTE_KINDS)},
                                               "query": {"type": "string", "description": "Text to look for"}}},
             guarded(notes)),
    ]


def board_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        register_tools(ctx, _make_tools(ctx))

    return Plugin("board", apply)
