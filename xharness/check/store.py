"""Check platform storage (SQLite, standard library).

Tables, by what may change:
  append-only (triggers refuse UPDATE and DELETE): audit, results, case_events, table_versions
  updated in place: runs (finish), cases (status/assignee), tasks (state)

The audit table is a hash chain: each row stores sha256(prev_hash + its own
canonical JSON), so editing or removing any row -- even with direct database
access and the triggers dropped -- breaks verify_audit() from that row on.

Every business row carries an agency code; callers pass the agency resolved
from the signed-in identity or API client, never one sent by the browser.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

TPE = timezone(timedelta(hours=8))
GENESIS = "0" * 64
CASE_STATES = {"open": "待處理", "in_progress": "處理中", "resolved": "已處理"}
CASE_ACTIONS = {
    # action: (allowed from, to)
    "take": ({"open"}, "in_progress"),
    "resolve": ({"open", "in_progress"}, "resolved"),
    "reopen": ({"resolved"}, "open"),
    "assign": (set(CASE_STATES), None),
    "note": (set(CASE_STATES), None),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  agency TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, agency TEXT NOT NULL, agent TEXT NOT NULL, agent_version TEXT NOT NULL,
  agent_digest TEXT NOT NULL, table_versions TEXT NOT NULL, data_basis TEXT NOT NULL, trigger TEXT NOT NULL,
  caller TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', received TEXT NOT NULL, finished TEXT,
  elapsed_ms REAL, status TEXT NOT NULL, records INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, agency TEXT NOT NULL, agent TEXT NOT NULL,
  business_key TEXT NOT NULL, rule_id TEXT NOT NULL, rule_title TEXT NOT NULL, level TEXT NOT NULL,
  message TEXT NOT NULL, basis TEXT NOT NULL, fields TEXT NOT NULL, evidence TEXT NOT NULL,
  input_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, case_id TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS results_key ON results(agency, business_key);
CREATE INDEX IF NOT EXISTS results_run ON results(run_id);
CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, agency TEXT NOT NULL, agent TEXT NOT NULL, business_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_open ON cases(agency, fingerprint, status);
CREATE TABLE IF NOT EXISTS case_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS table_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agency TEXT NOT NULL, agent TEXT NOT NULL, name TEXT NOT NULL,
  version INTEGER NOT NULL, effective_from TEXT NOT NULL, rows TEXT NOT NULL, approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', UNIQUE(agency, agent, name, version));
CREATE TABLE IF NOT EXISTS agent_drafts (
  agent TEXT PRIMARY KEY, definition TEXT NOT NULL, author TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL,
  published_by TEXT NOT NULL, published_at TEXT NOT NULL, metrics TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
  UNIQUE(agent, version));
CREATE TABLE IF NOT EXISTS agent_state (
  agent TEXT PRIMARY KEY, enabled INTEGER NOT NULL, actor TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, agency TEXT NOT NULL, agent TEXT NOT NULL, caller TEXT NOT NULL,
  state TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '');
"""
APPEND_ONLY = ("audit", "results", "case_events", "table_versions", "agent_versions")


def now_iso() -> str:
    return datetime.now(TPE).isoformat(timespec="milliseconds")


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CheckStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            os.chmod(path, 0o600)
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                self._db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table} "  # nosec B608 -- fixed names
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")

    # --- helpers ---------------------------------------------------------
    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one commit (one fsync). Nested calls join the outer one."""
        with self._lock:
            if self._db.in_transaction:
                yield
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- audit hash chain ------------------------------------------------
    def audit(self, actor: str, action: str, *, agency: str = "", target: str = "", detail: dict[str, Any] | None = None) -> int:
        with self._lock:
            last = self._db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            ts = now_iso()
            body = {"ts": ts, "actor": actor, "action": action, "agency": agency, "target": target, "detail": detail or {}}
            digest = sha256(prev + canonical(body))
            cursor = self._db.execute(
                "INSERT INTO audit(ts, actor, action, agency, target, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                (ts, actor, action, agency, target, canonical(detail or {}), prev, digest))
            return int(cursor.lastrowid or 0)

    def verify_audit(self) -> dict[str, Any]:
        prev = GENESIS
        count = 0
        for row in self._rows("SELECT * FROM audit ORDER BY seq"):
            body = {"ts": row["ts"], "actor": row["actor"], "action": row["action"], "agency": row["agency"],
                    "target": row["target"], "detail": json.loads(row["detail"])}
            if row["prev_hash"] != prev or row["hash"] != sha256(prev + canonical(body)):
                return {"ok": False, "count": count, "broken_at": row["seq"], "head": prev}
            prev = row["hash"]
            count += 1
        return {"ok": True, "count": count, "broken_at": None, "head": prev}

    def audit_list(self, agency: str | None, limit: int = 200) -> list[dict[str, Any]]:
        if agency is None:
            return self._rows("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return self._rows("SELECT * FROM audit WHERE agency=? ORDER BY seq DESC LIMIT ?", (agency, limit))

    def audit_export(self) -> list[dict[str, Any]]:
        """Whole chain for hand-over at contract end (履約期間不得刪除，結案移交)."""
        return self._rows("SELECT * FROM audit ORDER BY seq")

    # --- runs and results ------------------------------------------------
    def start_run(self, *, agency: str, agent: str, agent_version: str, agent_digest: str,
                  table_versions: dict[str, int], data_basis: str, trigger: str, caller: str,
                  params: dict[str, Any] | None = None, received: str = "") -> str:
        run_id = "run_" + uuid.uuid4().hex[:20]
        with self._lock:
            self._db.execute(
                "INSERT INTO runs(run_id, agency, agent, agent_version, agent_digest, table_versions, data_basis, "
                "trigger, caller, params, received, status) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'running')",
                (run_id, agency, agent, agent_version, agent_digest, canonical(table_versions), data_basis,
                 trigger, caller, canonical(params or {}), received or now_iso()))
        return run_id

    def finish_run(self, run_id: str, *, status: str, records: int, elapsed_ms: float, error: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET status=?, records=?, elapsed_ms=?, finished=?, error=? WHERE run_id=?",
                             (status, records, round(elapsed_ms, 2), now_iso(), error[:500], run_id))

    def add_result(self, *, run_id: str, agency: str, agent: str, business_key: str, finding: dict[str, Any],
                   input_hash: str, case_id: str) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO results(run_id, agency, agent, business_key, rule_id, rule_title, level, message, basis, "
                "fields, evidence, input_hash, fingerprint, case_id, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, agency, agent, business_key, finding["rule_id"], finding["title"], finding["level"],
                 finding["message"], finding["basis"], canonical(finding["fields"]), canonical(finding["evidence"]),
                 input_hash, finding["fingerprint"], case_id, now_iso()))
            return int(cursor.lastrowid or 0)

    def run(self, run_id: str, agency: str | None) -> dict[str, Any] | None:
        run = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if run is None or (agency is not None and run["agency"] != agency):
            return None
        run["table_versions"] = json.loads(run["table_versions"])
        run["params"] = json.loads(run["params"])
        run["results"] = [self._result(r) for r in self._rows("SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,))]
        return run

    def runs(self, agency: str | None, limit: int = 100) -> list[dict[str, Any]]:
        if agency is None:
            return self._rows("SELECT * FROM runs ORDER BY received DESC LIMIT ?", (limit,))
        return self._rows("SELECT * FROM runs WHERE agency=? ORDER BY received DESC LIMIT ?", (agency, limit))

    @staticmethod
    def _result(row: dict[str, Any]) -> dict[str, Any]:
        row["fields"] = json.loads(row["fields"])
        row["evidence"] = json.loads(row["evidence"])
        return row

    def history(self, agency: str, business_key: str) -> list[dict[str, Any]]:
        """Every result for one business key across runs and agents (同案歷次結果)."""
        return [self._result(r) for r in self._rows(
            "SELECT * FROM results WHERE agency=? AND business_key=? ORDER BY id", (agency, business_key))]

    # --- cases -----------------------------------------------------------
    def case_for(self, *, agency: str, agent: str, business_key: str, fingerprint: str, reason: str, actor: str) -> str:
        """Open case with the same fingerprint (any agent) or a new one -- cross-agent merge."""
        with self._lock:
            row = self._db.execute(
                "SELECT case_id FROM cases WHERE agency=? AND fingerprint=? AND status!='resolved' ORDER BY created LIMIT 1",
                (agency, fingerprint)).fetchone()
            if row:
                return str(row["case_id"])
            case_id = "case_" + uuid.uuid4().hex[:16]
            ts = now_iso()
            self._db.execute(
                "INSERT INTO cases(case_id, agency, agent, business_key, fingerprint, reason, status, created, updated) "
                "VALUES (?,?,?,?,?,?, 'open', ?, ?)", (case_id, agency, agent, business_key, fingerprint, reason, ts, ts))
            self._db.execute("INSERT INTO case_events(case_id, ts, actor, action, from_status, to_status, note) "
                             "VALUES (?,?,?, 'create', '', 'open', ?)", (case_id, ts, actor, reason))
            return case_id

    def cases(self, agency: str | None, status: str = "", assignee: str = "", limit: int = 100, offset: int = 0,
              agent: str = "") -> dict[str, Any]:
        where, args = [], []
        if agent:
            where.append("agent=?")
            args.append(agent)
        if agency is not None:
            where.append("agency=?")
            args.append(agency)
        if status:
            where.append("status=?")
            args.append(status)
        if assignee:
            where.append("assignee=?")
            args.append(assignee)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._one(f"SELECT COUNT(*) AS n FROM cases{clause}", args) or {"n": 0}  # nosec B608 -- clause from fixed strings
        rows = self._rows(f"SELECT * FROM cases{clause} ORDER BY updated DESC LIMIT ? OFFSET ?", [*args, limit, offset])  # nosec B608
        for row in rows:
            row["status_label"] = CASE_STATES.get(row["status"], row["status"])
            row["results"] = self._one("SELECT COUNT(*) AS n FROM results WHERE case_id=?", (row["case_id"],))["n"]  # type: ignore[index]
        return {"total": total["n"], "cases": rows}

    def case(self, case_id: str, agency: str | None) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if row is None or (agency is not None and row["agency"] != agency):
            return None
        row["status_label"] = CASE_STATES.get(row["status"], row["status"])
        row["events"] = self._rows("SELECT * FROM case_events WHERE case_id=? ORDER BY id", (case_id,))
        row["results"] = [self._result(r) for r in self._rows("SELECT * FROM results WHERE case_id=? ORDER BY id", (case_id,))]
        return row

    def case_action(self, case_id: str, agency: str | None, *, actor: str, action: str,
                    assignee: str = "", note: str = "") -> dict[str, Any]:
        if action not in CASE_ACTIONS:
            raise ValueError(f"不認得的動作 {action}")
        with self._lock:
            row = self.case(case_id, agency)
            if row is None:
                raise LookupError("找不到案件")
            allowed, to_status = CASE_ACTIONS[action]
            if row["status"] not in allowed:
                raise ValueError(f"案件狀態「{row['status_label']}」不能做 {action}")
            if action == "note" and not note.strip():
                raise ValueError("附註不能空白")
            new_status = to_status or row["status"]
            new_assignee = assignee if action == "assign" else (actor if action == "take" else row["assignee"])
            ts = now_iso()
            self._db.execute("UPDATE cases SET status=?, assignee=?, updated=? WHERE case_id=?",
                             (new_status, new_assignee, ts, case_id))
            event_note = note.strip()[:2000] or (f"指派給 {assignee}" if action == "assign" else "")
            self._db.execute("INSERT INTO case_events(case_id, ts, actor, action, from_status, to_status, note) "
                             "VALUES (?,?,?,?,?,?,?)", (case_id, ts, actor, action, row["status"], new_status, event_note))
        return self.case(case_id, agency) or {}

    # --- decision tables -------------------------------------------------
    def put_table(self, *, agency: str, agent: str, name: str, rows: list[dict[str, Any]], effective_from: str,
                  approved_by: str, note: str = "") -> int:
        with self._lock:
            last = self._db.execute("SELECT MAX(version) AS v FROM table_versions WHERE agency=? AND agent=? AND name=?",
                                    (agency, agent, name)).fetchone()
            version = int(last["v"] or 0) + 1
            self._db.execute(
                "INSERT INTO table_versions(agency, agent, name, version, effective_from, rows, approved_by, approved_at, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (agency, agent, name, version, effective_from, canonical(rows), approved_by, now_iso(), note[:500]))
            return version

    def table_at(self, agency: str, agent: str, name: str, basis_date: str) -> tuple[int, list[dict[str, Any]]]:
        """The newest version in effect on basis_date (YYYY-MM-DD); (0, []) when none."""
        row = self._one("SELECT version, rows FROM table_versions WHERE agency=? AND agent=? AND name=? AND effective_from<=? "
                        "ORDER BY effective_from DESC, version DESC LIMIT 1", (agency, agent, name, basis_date[:10]))
        return (int(row["version"]), json.loads(row["rows"])) if row else (0, [])

    def table_history(self, agency: str, agent: str, name: str) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM table_versions WHERE agency=? AND agent=? AND name=? ORDER BY version DESC",
                          (agency, agent, name))
        for row in rows:
            row["rows"] = json.loads(row["rows"])
        return rows

    # --- agents designed on the web page -----------------------------------
    def save_draft(self, agent: str, definition: dict[str, Any], author: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO agent_drafts(agent, definition, author, updated) VALUES (?,?,?,?) "
                             "ON CONFLICT(agent) DO UPDATE SET definition=excluded.definition, author=excluded.author, "
                             "updated=excluded.updated", (agent, canonical(definition), author, now_iso()))

    def draft(self, agent: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM agent_drafts WHERE agent=?", (agent,))
        if row:
            row["definition"] = json.loads(row["definition"])
        return row

    def drafts(self) -> list[dict[str, Any]]:
        return self._rows("SELECT agent, author, updated FROM agent_drafts ORDER BY updated DESC")

    def publish_agent(self, agent: str, definition: dict[str, Any], by: str, metrics: dict[str, Any], note: str = "") -> int:
        with self._lock:
            last = self._db.execute("SELECT MAX(version) AS v FROM agent_versions WHERE agent=?", (agent,)).fetchone()
            version = int(last["v"] or 0) + 1
            self._db.execute("INSERT INTO agent_versions(agent, version, definition, published_by, published_at, metrics, note) "
                             "VALUES (?,?,?,?,?,?,?)", (agent, version, canonical(definition), by, now_iso(), canonical(metrics), note[:500]))
            return version

    def agent_versions(self, agent: str) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM agent_versions WHERE agent=? ORDER BY version DESC", (agent,))
        for row in rows:
            row["definition"] = json.loads(row["definition"])
            row["metrics"] = json.loads(row["metrics"])
        return rows

    def published_agents(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT v.* FROM agent_versions v JOIN (SELECT agent, MAX(version) AS m FROM agent_versions GROUP BY agent) l "
                          "ON v.agent=l.agent AND v.version=l.m ORDER BY v.agent")
        for row in rows:
            row["definition"] = json.loads(row["definition"])
        return rows

    def agents_stamp(self) -> int:
        """Changes whenever any process publishes an agent version (lets every instance on the same database reload)."""
        row = self._one("SELECT MAX(id) AS m FROM agent_versions")
        return int((row or {}).get("m") or 0)

    def set_enabled(self, agent: str, enabled: bool, actor: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO agent_state(agent, enabled, actor, updated) VALUES (?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                             "enabled=excluded.enabled, actor=excluded.actor, updated=excluded.updated", (agent, int(enabled), actor, now_iso()))

    def enabled_map(self) -> dict[str, bool]:
        return {r["agent"]: bool(r["enabled"]) for r in self._rows("SELECT agent, enabled FROM agent_state")}

    # --- async tasks -----------------------------------------------------
    def create_task(self, *, agency: str, agent: str, caller: str) -> str:
        task_id = "task_" + uuid.uuid4().hex[:20]
        ts = now_iso()
        with self._lock:
            self._db.execute("INSERT INTO tasks(task_id, agency, agent, caller, state, created, updated) VALUES (?,?,?,?, 'queued', ?, ?)",
                             (task_id, agency, agent, caller, ts, ts))
        return task_id

    def set_task(self, task_id: str, *, state: str, run_id: str = "", error: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE tasks SET state=?, run_id=COALESCE(NULLIF(?, ''), run_id), error=?, updated=? WHERE task_id=?",
                             (state, run_id, error[:500], now_iso(), task_id))

    def task(self, task_id: str, agency: str | None) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None or (agency is not None and row["agency"] != agency):
            return None
        return row

    # --- statistics ------------------------------------------------------
    def stats(self, agency: str | None, since: str = "", until: str = "") -> dict[str, Any]:
        where, args = ["1=1"], []
        if agency is not None:
            where.append("agency=?")
            args.append(agency)
        if since:
            where.append("created>=?")
            args.append(since)
        if until:
            where.append("created<?")
            args.append(until)
        clause = " AND ".join(where)
        by_rule = self._rows(f"SELECT agency, agent, rule_id, rule_title, level, COUNT(*) AS n FROM results WHERE {clause} "  # nosec B608
                             "GROUP BY agency, agent, rule_id, level ORDER BY n DESC", args)
        run_where = clause.replace("created", "received")
        runs = self._rows(f"SELECT agent, status, COUNT(*) AS n, SUM(records) AS records, AVG(elapsed_ms) AS avg_ms, "  # nosec B608
                          f"MAX(elapsed_ms) AS max_ms FROM runs WHERE {run_where} GROUP BY agent, status", args)
        cases = self._rows(f"SELECT status, COUNT(*) AS n FROM cases WHERE {clause} GROUP BY status", args)  # nosec B608
        return {"by_rule": by_rule, "runs": runs, "cases": {r["status"]: r["n"] for r in cases}}

    def results_between(self, agency: str | None, since: str = "", until: str = "") -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if agency is not None:
            where.append("agency=?")
            args.append(agency)
        if since:
            where.append("created>=?")
            args.append(since)
        if until:
            where.append("created<?")
            args.append(until)
        return self._rows(f"SELECT * FROM results WHERE {' AND '.join(where)} ORDER BY id", args)  # nosec B608

