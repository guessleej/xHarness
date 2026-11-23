"""`xharness check serve`: the check API and its management page.

Source systems (BAF, IGSS ...) call with an API client token:
  POST /api/check/v1/agents/<id>/check   {"records": [...]} or {"record": {...}, "documents": [...]}
       200 with findings (rules only, milliseconds), or 202 with a task id when documents need reading
  GET  /api/check/v1/tasks/<task_id>

People sign in with their xHarness account and act within their agency:
  reviewer  cases: take, assign, note, resolve, reopen
  approver  decision tables: publish a new version with an effective date (the vendor never does)
  operator  try a check from the page (trigger "manual")
  auditor   audit trail, chain verification and hand-over export
A platform administrator without a [check.people] entry sees every agency read-only
plus the audit trail, and can never publish decision values.

The agency of every request comes from the token (client entry or [check.people]),
never from the request body. Bound to loopback it may speak plain http behind a TLS
front end; bound to any other address it speaks https only (a temporary self-signed
certificate until [check] tls_cert/tls_key point at the agency's own).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess  # nosec B404 -- only openssl, for the temporary self-signed certificate
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..identity import UserStore
from ..session import harness_home
from .engine import CheckEngine
from .spec import AgentSpec, discover
from .store import CASE_STATES, CheckStore, now_iso

DEFAULT_PORT = 3095
MAX_BODY = 2_000_000
MAX_RECORDS = 500
ROLES = {"reviewer", "approver", "operator", "auditor"}
PREFIX = "/api/check/v1"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")


def check_home(settings: dict[str, Any]) -> str:
    return str(settings.get("home") or os.path.join(harness_home(), "check"))


def _private_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def load_secret(path: str) -> bytes:
    if os.path.exists(path):
        with open(path, "rb") as handle:
            data = handle.read().strip()
        if len(data) >= 32:
            return data
    data = secrets.token_hex(32).encode()
    _private_file(path, data)
    return data


class Clients:
    """Source-system API clients. Only the token's SHA-256 is stored."""

    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    def add(self, name: str, agency: str, agents: list[str]) -> str:
        if not re.match(r"^[A-Za-z0-9_.-]{2,40}$", name) or not agency:
            raise ValueError("用戶端名稱限英數與 _.-，2～40 字；agency 必填")
        token = "xck_" + secrets.token_urlsafe(32)
        data = self._read()
        data[name] = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "agency": agency,
                      "agents": agents, "created": now_iso()}
        _private_file(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode())
        return token

    def remove(self, name: str) -> bool:
        data = self._read()
        if data.pop(name, None) is None:
            return False
        _private_file(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode())
        return True

    def resolve(self, token: str) -> tuple[str, dict[str, Any]] | None:
        if not token.startswith("xck_"):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for name, entry in self._read().items():
            if hmac.compare_digest(str(entry.get("token_sha256", "")), digest):
                return name, entry
        return None

    def listing(self) -> list[dict[str, Any]]:
        return [{"name": name, "agency": e.get("agency"), "agents": e.get("agents", []), "created": e.get("created")}
                for name, e in self._read().items()]


class Caller:
    def __init__(self, kind: str, name: str, agency: str | None, roles: set[str], agents: list[str] | None = None):
        self.kind, self.name, self.agency, self.roles = kind, name, agency, roles
        self.agents = agents or []

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "agency": self.agency, "roles": sorted(self.roles),
                "all_agencies": self.agency is None}


class CheckService:
    def __init__(self, settings: dict[str, Any], auth: dict[str, Any], *, agents: dict[str, AgentSpec] | None = None,
                 engine: CheckEngine | None = None):
        self.settings = settings
        home = check_home(settings)
        self.store = engine.store if engine else CheckStore(str(settings.get("db") or os.path.join(home, "check.db")))
        self.agents = agents if agents is not None else discover([str(d) for d in settings.get("agents_dirs", [])])
        secret = load_secret(str(settings.get("secret_file") or os.path.join(home, "secret")))
        self.engine = engine or CheckEngine(self.store, self.agents, secret=secret,
                                            max_parallel=int(settings.get("max_parallel", 2)))
        self.clients = Clients(str(settings.get("clients_file") or os.path.join(home, "clients.json")))
        self.users = UserStore(auth)
        self.people = {str(k): v for k, v in (settings.get("people") or {}).items() if isinstance(v, dict)}

    # --- identity --------------------------------------------------------
    def caller(self, token: str) -> Caller | None:
        client = self.clients.resolve(token)
        if client:
            name, entry = client
            return Caller("client", name, str(entry["agency"]), {"client"}, list(entry.get("agents", [])))
        user = self.users.resolve(token) if self.users.enabled else None
        if user is None:
            return None
        entry = self.people.get(user.name)
        if entry and entry.get("agency"):
            return Caller("person", user.name, str(entry["agency"]), {str(r) for r in entry.get("roles", [])} & ROLES)
        if user.is_admin:
            return Caller("person", user.name, None, {"auditor"})
        return None

    def login(self, name: str, password: str) -> tuple[str, Caller] | None:
        if not self.users.enabled:
            return None
        user, _detail = self.users.authenticate(name, password)
        if user is None:
            self.store.audit(name or "-", "login", detail={"ok": False})
            return None
        token = self.users.issue(user)
        caller = self.caller(token)
        if caller is None:
            self.users.revoke(token)
            self.store.audit(name, "login", detail={"ok": False, "reason": "未設定機關與角色"})
            return None
        self.store.audit(name, "login", agency=caller.agency or "", detail={"ok": True})
        return token, caller

    # --- setup track -----------------------------------------------------
    def setup(self, caller: Caller) -> list[dict[str, Any]]:
        agency = caller.agency
        missing, first_agent = [], ""
        for agent in self.agents.values():
            for name in agent.tables:
                if agency is not None and self.store.table_at(agency, agent.id, name, "9999-12-31")[0] == 0:
                    missing.append(f"{agent.name}／{agent.tables[name].title}")
                    first_agent = first_agent or agent.id
        clients = [c for c in self.clients.listing() if agency is None or c["agency"] == agency]
        runs = self.store.runs(agency, limit=1)
        cases = self.store.cases(agency, status="resolved", limit=1)["total"]
        steps = [
            ("填判定值", "規則用到的上限、名單、額度都由機關核定；沒填，規則一律轉人工。", not missing,
             f"還缺：{'、'.join(missing[:3])}{'…' if len(missing) > 3 else ''}" if missing else "全部判定值表都已核定", "tables"),
            ("發來源系統金鑰", "BAF、IGSS 等系統要用自己的金鑰呼叫檢核 API。", bool(clients),
             f"已發 {len(clients)} 組" if clients else "由平臺管理者以 xharness check client add 發給", ""),
            ("試打一次檢核", "用一筆樣本確認提示內容與依據條文正確。", bool(runs), "已有執行紀錄" if runs else "到「試打檢核」送一筆", "try"),
            ("覆核第一件案件", "提示會成為待處理案件，權責人員處理後留下歷程。", cases > 0,
             f"已處理 {cases} 件" if cases else "到「案件覆核」接手一件", "cases"),
        ]
        needs = {3: [1], 4: [3]}  # try needs the tables; review needs a run
        out, current = [], False
        for index, (title, why, done, detail, tab) in enumerate(steps, 1):
            waiting = [n for n in needs.get(index, []) if not steps[n - 1][2]]
            if done:
                state = "done"
            elif waiting:
                state, detail = "blocked", f"要先完成第 {waiting[0]} 步"
            elif not current:
                state, current = "current", True
            else:
                state = "todo"
            out.append({"step": index, "title": title, "why": why, "state": state, "detail": detail, "tab": tab,
                        "agent": first_agent if index == 1 else ""})
        return out

    # --- decision tables -------------------------------------------------
    @staticmethod
    def clean_rows(agent: AgentSpec, name: str, rows: Any) -> list[dict[str, Any]]:
        table = agent.tables[name]
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("rows 要是陣列，最多 5000 列")
        cleaned, keys = [], set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"第 {index} 列格式錯誤")
            out: dict[str, Any] = {}
            for column in table.columns:
                value = row.get(column.name, "")
                if column.type == "number":
                    try:
                        value = float(str(value).replace(",", ""))
                    except ValueError:
                        raise ValueError(f"第 {index} 列「{column.title or column.name}」要是數字") from None
                    value = int(value) if value == int(value) else value
                elif column.type == "date":
                    if not DATE_RE.match(str(value)):
                        raise ValueError(f"第 {index} 列「{column.title or column.name}」要是 YYYY-MM-DD")
                elif column.type == "bool":
                    value = str(value).lower() in {"1", "true", "是", "y", "yes"}
                else:
                    value = str(value).strip()[:200]
                out[column.name] = value
            key = tuple(str(out[k]) for k in table.key)
            if any(not part for part in key):
                raise ValueError(f"第 {index} 列的鍵值（{'、'.join(table.key)}）不能空白")
            if key in keys:
                raise ValueError(f"第 {index} 列與前面重複：{'／'.join(key)}")
            keys.add(key)
            cleaned.append(out)
        return cleaned


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(service: CheckService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "xharness-check"
        timeout = 30
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # keep tokens and bodies out of logs
            print(f"[check] {self.address_string()} {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}",
                  file=sys.stderr)

        # --- plumbing ----------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any, extra: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8", extra)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "請求太大")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ApiError(400, "內容不是 JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "內容要是 JSON 物件")
            return data

        def _caller(self) -> Caller:
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            caller = service.caller(token) if token else None
            if caller is None:
                raise ApiError(401, "未登入或金鑰無效")
            return caller

        @staticmethod
        def _need(caller: Caller, role: str) -> None:
            if role not in caller.roles:
                raise ApiError(403, f"需要「{role}」角色")

        def _agent(self, agent_id: str, caller: Caller) -> AgentSpec:
            agent = service.agents.get(agent_id)
            if agent is None or (caller.kind == "client" and caller.agents and agent_id not in caller.agents):
                raise ApiError(404, "沒有這支檢核 Agent")
            return agent

        def _dispatch(self, method: str) -> None:
            received = now_iso()
            url = urlparse(self.path)
            path, query = url.path.rstrip("/") or "/", {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and path in {"/", "/index.html"}:
                    with open(UI_PATH, "rb") as handle:
                        return self._send(200, handle.read(), "text/html; charset=utf-8")
                if method == "GET" and path == "/healthz":
                    return self._json(200, {"ok": True, "agents": len(service.agents), "extractor": service.engine.breaker.state})
                if not path.startswith(PREFIX):
                    raise ApiError(404, "找不到")
                route = path[len(PREFIX):]
                if method == "POST" and route == "/login":
                    body = self._body()
                    result = service.login(str(body.get("user", "")), str(body.get("password", "")))
                    if result is None:
                        raise ApiError(401, "帳號或密碼錯誤，或這個帳號沒有檢核平臺的機關與角色")
                    return self._json(200, {"token": result[0], "me": result[1].public()})
                caller = self._caller()
                return self._route(method, route, query, caller, received)
            except ApiError as error:
                return self._json(error.status, {"error": str(error)})
            except (LookupError, ValueError) as error:
                return self._json(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 -- never leak a traceback to the caller
                print(f"[check] {type(error).__name__}: {error}", file=sys.stderr)
                return self._json(500, {"error": "伺服器錯誤，已記錄"})

        def _route(self, method: str, route: str, query: dict[str, str], caller: Caller, received: str) -> None:
            agency = caller.agency
            parts = [p for p in route.split("/") if p]
            if method == "POST" and route == "/logout":
                auth = self.headers.get("Authorization", "")
                service.users.revoke(auth[7:].strip())
                service.store.audit(caller.name, "logout", agency=agency or "")
                return self._json(200, {"ok": True})
            if method == "GET" and route == "/me":
                return self._json(200, caller.public())
            if method == "GET" and route == "/setup":
                return self._json(200, {"steps": service.setup(caller)})
            if method == "GET" and route == "/agents":
                return self._json(200, {"agents": [a.public() for a in service.agents.values()
                                                   if caller.kind != "client" or not caller.agents or a.id in caller.agents]})
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "check" and method == "POST":
                return self._check(parts[1], caller, received)
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "samples" and method == "GET":
                agent = self._agent(parts[1], caller)
                return self._json(200, {"samples": [{"name": c.name, "record": c.record} for c in agent.cases]})
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "eval" and method == "GET":
                self._agent(parts[1], caller)
                return self._json(200, service.engine.evaluate_cases(parts[1]))
            if len(parts) == 2 and parts[0] == "tasks" and method == "GET":
                task = service.store.task(parts[1], agency)
                if task is None:
                    raise ApiError(404, "找不到任務")
                if task["state"] == "done":
                    task["result"] = service.store.run(task["run_id"], agency)
                return self._json(200, task)
            if caller.kind == "client":
                raise ApiError(403, "來源系統金鑰只能呼叫檢核與任務查詢")
            if method == "GET" and route == "/runs":
                return self._json(200, {"runs": service.store.runs(agency, limit=min(int(query.get("limit", 100)), 500))})
            if len(parts) == 2 and parts[0] == "runs" and method == "GET":
                run = service.store.run(parts[1], agency)
                if run is None:
                    raise ApiError(404, "找不到執行紀錄")
                return self._json(200, run)
            if method == "GET" and route == "/history":
                if agency is None:
                    raise ApiError(400, "跨機關帳號查歷次結果請帶機關的帳號")
                return self._json(200, {"results": service.store.history(agency, query.get("key", ""))})
            if method == "GET" and route == "/cases":
                assignee = caller.name if query.get("mine") == "1" else ""
                status = query.get("status", "")
                if status and status not in CASE_STATES:
                    raise ApiError(400, "狀態只能是 " + "、".join(CASE_STATES))
                return self._json(200, service.store.cases(agency, status=status, assignee=assignee, agent=query.get("agent", ""),
                                                           limit=min(int(query.get("limit", 200)), 500)) | {"states": CASE_STATES})
            if len(parts) == 2 and parts[0] == "cases":
                if method == "GET":
                    case = service.store.case(parts[1], agency)
                    if case is None:
                        raise ApiError(404, "找不到案件")
                    return self._json(200, case)
                if method == "POST":
                    self._need(caller, "reviewer")
                    body = self._body()
                    action = str(body.get("action", ""))
                    case = service.store.case_action(parts[1], agency, actor=caller.name, action=action,
                                                     assignee=str(body.get("assignee", ""))[:60], note=str(body.get("note", "")))
                    service.store.audit(caller.name, f"case.{action}", agency=agency or "", target=parts[1],
                                        detail={"assignee": body.get("assignee", ""), "note_chars": len(str(body.get("note", "")))})
                    return self._json(200, case)
            if len(parts) == 3 and parts[0] == "tables":
                agent = self._agent(parts[1], caller)
                name = parts[2]
                if name not in agent.tables:
                    raise ApiError(404, "沒有這張判定值表")
                if method == "GET":
                    if agency is None:
                        return self._json(200, {"history": [], "note": "跨機關帳號沒有判定值表"})
                    return self._json(200, {"table": agent.public()["tables"][name],
                                            "history": service.store.table_history(agency, agent.id, name)})
                if method == "POST":
                    self._need(caller, "approver")
                    if agency is None:
                        raise ApiError(403, "判定值只能由機關核定")
                    body = self._body()
                    effective = str(body.get("effective_from", ""))
                    if not DATE_RE.match(effective):
                        raise ApiError(400, "生效日要是 YYYY-MM-DD")
                    date.fromisoformat(effective)
                    rows = service.clean_rows(agent, name, body.get("rows"))
                    before = service.store.table_at(agency, agent.id, name, "9999-12-31")
                    version = service.store.put_table(agency=agency, agent=agent.id, name=name, rows=rows, effective_from=effective,
                                                      approved_by=caller.name, note=str(body.get("note", "")))
                    service.store.audit(caller.name, "table.publish", agency=agency, target=f"{agent.id}/{name}",
                                        detail={"version": version, "effective_from": effective, "rows": len(rows),
                                                "previous_version": before[0], "previous_rows": len(before[1])})
                    return self._json(200, {"version": version, "rows": len(rows)})
            if method == "GET" and route == "/stats":
                return self._json(200, service.store.stats(agency, query.get("since", ""), query.get("until", "")))
            if method == "GET" and route == "/export.csv":
                return self._export(caller, query)
            if method == "GET" and route == "/clients":
                self._need(caller, "auditor")
                return self._json(200, {"clients": [c for c in service.clients.listing() if agency is None or c["agency"] == agency]})
            if route.startswith("/audit"):
                self._need(caller, "auditor")
                if method == "GET" and route == "/audit":
                    return self._json(200, {"audit": service.store.audit_list(agency, limit=min(int(query.get("limit", 200)), 1000))})
                if method == "GET" and route == "/audit/verify":
                    result = service.store.verify_audit()
                    service.store.audit(caller.name, "audit.verify", agency=agency or "", detail={"ok": result["ok"], "count": result["count"]})
                    return self._json(200, result)
                if method == "GET" and route == "/audit/export":
                    if agency is not None:
                        raise ApiError(403, "完整稽核鏈移交由平臺管理者匯出")
                    service.store.audit(caller.name, "audit.export")
                    return self._json(200, {"exported": now_iso(), "chain": service.store.audit_export(), "verify": service.store.verify_audit()},
                                      {"Content-Disposition": 'attachment; filename="check-audit.json"'})
            raise ApiError(404, "找不到")

        def _check(self, agent_id: str, caller: Caller, received: str) -> None:
            agent = self._agent(agent_id, caller)
            if caller.kind == "person":
                self._need(caller, "operator")
            if caller.agency is None:
                raise ApiError(403, "跨機關帳號不能送檢核")
            body = self._body()
            trigger = "sync" if caller.kind == "client" else "manual"
            documents = body.get("documents") or []
            if documents:
                if not isinstance(documents, list) or not isinstance(body.get("record"), dict):
                    raise ApiError(400, "附件請求要帶 record（物件）與 documents（陣列）")
                if not agent.is_async:
                    raise ApiError(400, "這支 Agent 不收附件")
                task_id = service.engine.submit(agent.id, agency=caller.agency, record=body["record"], documents=documents,
                                                caller=caller.name, received=received)
                return self._json(202, {"task_id": task_id, "state": "queued", "poll": f"{PREFIX}/tasks/{task_id}"})
            records = body.get("records")
            if records is None and isinstance(body.get("record"), dict):
                records = [body["record"]]
            if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
                raise ApiError(400, "要帶 records（物件陣列）或 record（物件）")
            if len(records) > MAX_RECORDS:
                raise ApiError(413, f"一次最多 {MAX_RECORDS} 筆；更多請用批次")
            basis = str(body.get("data_basis", ""))
            if basis and not DATE_RE.match(basis):
                raise ApiError(400, "data_basis 要是 YYYY-MM-DD")
            result = service.engine.check(agent.id, agency=caller.agency, records=records, trigger=trigger, caller=caller.name,
                                          basis=basis, received=received)
            result["received"] = received
            result["sent"] = now_iso()
            return self._json(200, result, {"X-Check-Elapsed-Ms": str(result["elapsed_ms"])})

        def _export(self, caller: Caller, query: dict[str, str]) -> None:
            """De-identified CSV: business keys become HMAC pseudonyms; no names, no record contents."""
            rows = service.store.results_between(caller.agency, query.get("since", ""), query.get("until", ""))
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["建立時間", "機關", "Agent", "案件代號（去識別）", "規則", "規則名稱", "等級", "依據", "執行識別"])
            for row in rows:
                writer.writerow([row["created"], row["agency"], row["agent"], service.engine.pseudonym(row["business_key"]),
                                 row["rule_id"], row["rule_title"], row["level"], row["basis"], row["run_id"]])
            service.store.audit(caller.name, "export.csv", agency=caller.agency or "",
                                detail={"rows": len(rows), "since": query.get("since", ""), "until": query.get("until", "")})
            self._send(200, ("﻿" + buffer.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="check-{time.strftime("%Y%m%d")}.csv"'})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def tls_files(settings: dict[str, Any], host: str) -> tuple[str, str, bool]:
    """(cert, key, self_signed). A configured pair wins; otherwise a temporary
    self-signed certificate is made once under <home>/tls so the first start is
    already https. Replace it with the agency's certificate via [check] tls_cert/tls_key."""
    cert, key = str(settings.get("tls_cert") or ""), str(settings.get("tls_key") or "")
    if cert and key:
        return cert, key, False
    folder = os.path.join(check_home(settings), "tls")
    cert, key = os.path.join(folder, "self-signed.crt"), os.path.join(folder, "self-signed.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        names = {"localhost", socket.gethostname(), *[str(n) for n in settings.get("tls_names", [])]}
        ips = {"127.0.0.1"} | ({host} if host not in {"0.0.0.0", "::"} else set())  # nosec B104 -- comparing, not binding
        try:
            ips |= set(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        ips |= {str(n) for n in settings.get("tls_names", []) if re.match(r"^[0-9.]+$", str(n))}
        names = {n for n in names if n and not re.match(r"^[0-9.]+$", n)}
        san = ",".join([f"DNS:{n}" for n in sorted(names)] + [f"IP:{ip}" for ip in sorted(ips)])
        subprocess.run(  # nosec B603 B607 -- fixed argv, no shell, timeout; openssl only makes the temporary certificate
            ["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "397", "-subj",
             "/CN=xHarness check (temporary self-signed)", "-addext", f"subjectAltName={san}",
             "-keyout", key, "-out", cert], check=True, capture_output=True, timeout=60)
        os.chmod(key, 0o600)
    return cert, key, True


def serve(settings: dict[str, Any], auth: dict[str, Any], host: str = "127.0.0.1", port: int | None = None) -> int:
    loopback = _loopback(host)
    tls = str(settings.get("tls", "auto")).lower()
    # Off this machine the platform speaks https only; plain http stays on loopback (behind a TLS front end).
    use_tls = tls == "on" or (tls == "auto" and (not loopback or bool(settings.get("tls_cert"))))
    if not loopback and not use_tls:
        print("xharness check: 綁本機以外的位址一定要 HTTPS（[check] tls 不可為 off）", file=sys.stderr)
        return 2
    service = CheckService(settings, auth)
    if not service.users.enabled:
        print("xharness check: 沒有 [auth]，管理頁無法登入；來源系統金鑰仍可呼叫檢核 API", file=sys.stderr)
    port = port or int(settings.get("port") or DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), make_handler(service))
    scheme = "http"
    if use_tls:
        try:
            cert, key, self_signed = tls_files(settings, host)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"xharness check: 產生暫用憑證失敗（{error}）；請設定 [check] tls_cert 與 tls_key", file=sys.stderr)
            server.server_close()
            return 2
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        # Handshake in the request thread, not in accept(): a slow client cannot stall the listener.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
        if self_signed:
            print(f"xharness check: 使用暫用自簽憑證 {cert}（瀏覽器會警告；正式上線請換成機關憑證）", file=sys.stderr)
    print(f"xharness check: {scheme}://{host}:{port}/  agents: {', '.join(service.agents) or '（無）'}  db: {service.store.path}",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.engine.shutdown()
    return 0
