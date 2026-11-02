"""One SQLite file holds the resident agent's state: Triggers and where each
went, Threads, Tickets, Decisions, the outbox, the stored work queue and small
records (Source read positions, settings versions).

Aggregates are JSON documents; the columns beside them (`active`, `open`, the
Thread a Trigger is on) exist only so a pass reads the live rows without
scanning everything. `atomic()` is one transaction (BEGIN IMMEDIATE, so a
second process waits instead of interleaving) and nests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .model import Doc, latest_assignment, live, thread_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger (id TEXT PRIMARY KEY, at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS disposition (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_thread (thread TEXT NOT NULL, at TEXT NOT NULL, id TEXT NOT NULL, human INTEGER NOT NULL,
  PRIMARY KEY (thread, id));
CREATE INDEX IF NOT EXISTS trigger_thread_at ON trigger_thread (thread, at, id);
CREATE TABLE IF NOT EXISTS thread (key TEXT PRIMARY KEY, doc TEXT NOT NULL, active INTEGER NOT NULL, subscribed INTEGER NOT NULL,
  last_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS thread_active ON thread (active) WHERE active = 1;
CREATE TABLE IF NOT EXISTS ticket (id TEXT PRIMARY KEY, doc TEXT NOT NULL, open INTEGER NOT NULL, active INTEGER NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ticket_open ON ticket (open) WHERE open = 1;
CREATE INDEX IF NOT EXISTS ticket_active ON ticket (active) WHERE active = 1;
CREATE TABLE IF NOT EXISTS decision (id TEXT PRIMARY KEY, thread TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decision_thread ON decision (thread, at);
CREATE INDEX IF NOT EXISTS decision_at ON decision (at);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, ticket TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, doc TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox (status);
CREATE TABLE IF NOT EXISTS effect (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
  due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
  UNIQUE (kind, key));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _dump(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._db.execute("COMMIT")

    def _one(self, sql: str, args: tuple = ()) -> Any:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return json.loads(row[0]) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[Any]:
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute(sql, args).fetchall()]

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # Triggers

    def trigger(self, trigger_id: str) -> Doc | None:
        return self._one("SELECT doc FROM trigger WHERE id = ?", (trigger_id,))

    def save_trigger(self, trigger: Doc) -> None:
        with self.atomic():
            self._run("INSERT OR REPLACE INTO trigger (id, at, doc) VALUES (?,?,?)", (trigger["id"], trigger["at"], _dump(trigger)))
            for ref in trigger["threads"]:
                self._run("INSERT OR REPLACE INTO trigger_thread (thread, at, id, human) VALUES (?,?,?,?)",
                          (thread_key(ref), trigger["at"], trigger["id"], 1 if trigger.get("human") else 0))

    def disposition(self, trigger_id: str) -> Doc | None:
        return self._one("SELECT doc FROM disposition WHERE id = ?", (trigger_id,))

    def save_disposition(self, trigger_id: str, doc: Doc) -> None:
        self._run("INSERT OR REPLACE INTO disposition (id, doc) VALUES (?,?)", (trigger_id, _dump(doc)))

    def triggers_on(self, key: str, limit: int = 50, before: str = "") -> list[Doc]:
        """A Thread's Triggers, newest first; `before` is an `<at>|<id>` cursor from the previous page."""
        if before:
            at, _, tid = before.partition("|")
            rows = self._all("SELECT t.doc FROM trigger_thread x JOIN trigger t ON t.id = x.id WHERE x.thread = ? AND "
                             "(x.at < ? OR (x.at = ? AND x.id < ?)) ORDER BY x.at DESC, x.id DESC LIMIT ?",
                             (key, at, at, tid, limit))
        else:
            rows = self._all("SELECT t.doc FROM trigger_thread x JOIN trigger t ON t.id = x.id WHERE x.thread = ? "
                             "ORDER BY x.at DESC, x.id DESC LIMIT ?", (key, limit))
        return rows

    def recent_triggers(self, since: str, limit: int = 500) -> list[Doc]:
        return self._all("SELECT doc FROM trigger WHERE at >= ? ORDER BY at DESC LIMIT ?", (since, limit))

    # Threads

    def thread(self, key: str) -> Doc | None:
        return self._one("SELECT doc FROM thread WHERE key = ?", (key,))

    def save_thread(self, thread: Doc) -> None:
        active = 1 if thread.get("pending") or thread.get("evaluating") else 0
        self._run("INSERT OR REPLACE INTO thread (key, doc, active, subscribed, last_at) VALUES (?,?,?,?,?)",
                  (thread["key"], _dump(thread), active, 1 if thread.get("subscriptions") else 0, thread.get("lastAt") or ""))

    def threads_active(self) -> list[Doc]:
        return self._all("SELECT doc FROM thread WHERE active = 1")

    def threads(self, limit: int = 200, subscribed: bool = False) -> list[Doc]:
        where = "WHERE subscribed = 1 " if subscribed else ""
        return self._all(f"SELECT doc FROM thread {where}ORDER BY last_at DESC LIMIT ?", (limit,))  # nosec B608 - fixed text

    # Tickets

    def ticket(self, ticket_id: str) -> Doc | None:
        return self._one("SELECT doc FROM ticket WHERE id = ?", (ticket_id,))

    def save_ticket(self, ticket: Doc) -> None:
        latest = latest_assignment(ticket)
        active = 1 if ticket.get("undelivered") or (latest and latest["state"] in ("pending", "running")) else 0
        self._run("INSERT OR REPLACE INTO ticket (id, doc, open, active, created, updated) VALUES (?,?,?,?,?,?)",
                  (ticket["id"], _dump(ticket), 1 if live(ticket) else 0, active, ticket["createdAt"], ticket["updatedAt"]))

    def tickets_open(self) -> list[Doc]:
        return self._all("SELECT doc FROM ticket WHERE open = 1 ORDER BY created")

    def tickets_active(self) -> list[Doc]:
        return self._all("SELECT doc FROM ticket WHERE active = 1 ORDER BY created")

    def tickets(self, limit: int = 200, ended: bool | None = None) -> list[Doc]:
        if ended is None:
            return self._all("SELECT doc FROM ticket ORDER BY updated DESC LIMIT ?", (limit,))
        return self._all("SELECT doc FROM ticket WHERE open = ? ORDER BY updated DESC LIMIT ?", (0 if ended else 1, limit))

    # Decisions

    def decision(self, decision_id: str) -> Doc | None:
        return self._one("SELECT doc FROM decision WHERE id = ?", (decision_id,))

    def save_decision(self, decision: Doc) -> None:
        self._run("INSERT OR REPLACE INTO decision (id, thread, at, kind, doc) VALUES (?,?,?,?,?)",
                  (decision["id"], decision["threadKey"], decision["at"], decision["kind"], _dump(decision)))

    def decisions_on(self, key: str) -> list[Doc]:
        return self._all("SELECT doc FROM decision WHERE thread = ? ORDER BY at", (key,))

    def decisions(self, limit: int = 200, kind: str = "") -> list[Doc]:
        if kind:
            return self._all("SELECT doc FROM decision WHERE kind = ? ORDER BY at DESC LIMIT ?", (kind, limit))
        return self._all("SELECT doc FROM decision ORDER BY at DESC LIMIT ?", (limit,))

    # Outbox: one row per message id, so a message is never queued twice.

    def outbox_status(self, message_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM outbox WHERE id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def outbox_add(self, message: Doc) -> bool:
        now = time.time()
        cursor = self._run("INSERT OR IGNORE INTO outbox (id, ticket, status, doc, created, updated) VALUES (?,?,?,?,?,?)",
                           (message["id"], message.get("ticketId", ""), "pending", _dump(message), now, now))
        return cursor.rowcount == 1

    def outbox_get(self, message_id: str) -> Doc | None:
        with self._lock:
            row = self._db.execute("SELECT doc, status FROM outbox WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row[0]), "status": row[1]}

    def outbox_set(self, message_id: str, status: str, **extra: Any) -> None:
        current = self.outbox_get(message_id)
        if current is None:
            return
        current.pop("status", None)
        current.update(extra)
        self._run("UPDATE outbox SET status = ?, doc = ?, updated = ? WHERE id = ?", (status, _dump(current), time.time(), message_id))

    def outbox(self, status: str = "", limit: int = 100) -> list[Doc]:
        with self._lock:
            if status:
                rows = self._db.execute("SELECT doc, status FROM outbox WHERE status = ? ORDER BY created DESC LIMIT ?",
                                        (status, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT doc, status FROM outbox ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(r[0]), "status": r[1]} for r in rows]

    # Stored work: what follows a decision is written first and done afterwards.

    def effect_add(self, kind: str, key: str, payload: Doc, delay: float = 0) -> None:
        now = time.time()
        self._run("INSERT INTO effect (kind, key, payload, due, created) VALUES (?,?,?,?,?) "
                  "ON CONFLICT (kind, key) DO UPDATE SET payload = excluded.payload, due = MIN(effect.due, excluded.due)",
                  (kind, key, _dump(payload), now + delay, now))

    def effects_due(self, now: float | None = None, limit: int = 50) -> list[Doc]:
        with self._lock:
            rows = self._db.execute("SELECT id, kind, key, payload, attempts, error FROM effect WHERE due <= ? ORDER BY id LIMIT ?",
                                    (time.time() if now is None else now, limit)).fetchall()
        return [{"id": r[0], "kind": r[1], "key": r[2], "payload": json.loads(r[3]), "attempts": r[4], "error": r[5]} for r in rows]

    def effects(self, kind: str = "") -> list[Doc]:
        with self._lock:
            rows = self._db.execute("SELECT id, kind, key, payload, attempts, error, due FROM effect " +
                                    ("WHERE kind = ? " if kind else "") + "ORDER BY id", (kind,) if kind else ()).fetchall()
        return [{"id": r[0], "kind": r[1], "key": r[2], "payload": json.loads(r[3]), "attempts": r[4], "error": r[5], "due": r[6]}
                for r in rows]

    def effect_done(self, effect_id: int) -> None:
        self._run("DELETE FROM effect WHERE id = ?", (effect_id,))

    def effect_failed(self, effect_id: int, error: str, delay: float) -> None:
        self._run("UPDATE effect SET attempts = attempts + 1, error = ?, due = ? WHERE id = ?",
                  (error[:500], time.time() + delay, effect_id))

    # Small records

    def kv(self, key: str, default: Any = None) -> Any:
        value = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if value is None else value

    def set_kv(self, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, _dump(value)))

    def kv_prefix(self, prefix: str) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM kv WHERE key >= ? AND key < ?", (prefix, prefix + "￿")).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}
