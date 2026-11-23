"""Run check agents: rules decide, documents and models only assist.

The sync path (check) does validation + decision tables + deterministic rules
and nothing else, so it answers in milliseconds. An agent that declares
``needs = ["ocr"]`` goes through submit(): the caller gets a task id at once
and a bounded per-agent worker (bulkhead) does the extraction, then the same
rules. When extraction is down or not configured, the record is marked
"cannot read, send to a person" -- the source system's own flow is never held.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

from . import rulekit
from .spec import AgentSpec
from .store import TPE, CheckStore, canonical, now_iso, sha256

LEVELS = {"notice", "manual", "sample"}
CHUNK = 200  # records per commit: one fsync per chunk, and the write lock is never held for a whole batch
Extractor = Callable[[AgentSpec, dict[str, Any], list[dict[str, Any]]], tuple[dict[str, Any], list[dict[str, Any]]]]


class Breaker:
    """Open after ``limit`` consecutive failures; stay open ``cool`` seconds."""

    def __init__(self, limit: int = 3, cool: float = 60.0):
        self.limit, self.cool = limit, cool
        self.failures, self.opened = 0, 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return self.failures < self.limit or time.time() - self.opened >= self.cool

    def ok(self) -> None:
        with self._lock:
            self.failures = 0

    def fail(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.limit:
                self.opened = time.time()

    @property
    def state(self) -> str:
        return "open" if not self.allow() else "closed"


def _manual(rule_id: str, title: str, message: str, fields: list[str] | None = None) -> dict[str, Any]:
    return {"rule_id": rule_id, "title": title, "basis": "平臺", "level": "manual", "message": message,
            "fields": fields or [], "evidence": [], "fingerprint_values": None}


def evaluate(agent: AgentSpec, record: dict[str, Any], tables: rulekit.Tables) -> list[dict[str, Any]]:
    """Pure: the findings for one record. A rule that raises becomes a manual finding, never a pass."""
    missing = [name for name in agent.required if rulekit.dig(record, name) in (None, "", [])]
    if missing:
        return [_manual("INPUT", "必要欄位缺漏", "缺少欄位：" + "、".join(missing) + "，無法檢核，轉人工", missing)]
    findings = []
    for rule in agent.rules:
        try:
            out = rule.func(record, tables)
        except Exception as error:  # noqa: BLE001 -- a broken rule must not look like a pass
            findings.append(_manual(rule.id, rule.title, f"規則執行錯誤（{type(error).__name__}），轉人工"))
            continue
        if not out:
            continue
        for item in out if isinstance(out, list) else [out]:
            values = dict(item)
            try:
                message = rule.message.format(**values)
            except (KeyError, IndexError, ValueError):
                message = rule.message
            level = str(values.get("level", "notice"))
            findings.append({
                "rule_id": rule.id, "title": rule.title, "basis": rule.basis,
                "level": level if level in LEVELS else "notice", "message": message,
                "fields": list(values.get("fields", [])), "evidence": list(values.get("evidence", [])),
                "fingerprint_values": values.get("fingerprint"),
            })
    return findings


class CheckEngine:
    def __init__(self, store: CheckStore, agents: dict[str, AgentSpec], *, secret: bytes,
                 extractor: Extractor | None = None, max_parallel: int = 2):
        self.store = store
        self.agents = agents
        self.secret = secret
        self.extractor = extractor
        self.breaker = Breaker()
        self._pools = {agent_id: ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix=f"check-{agent_id}")
                       for agent_id, spec in agents.items() if spec.is_async}

    # --- helpers ---------------------------------------------------------
    def agent(self, agent_id: str) -> AgentSpec:
        if agent_id not in self.agents:
            raise LookupError(f"沒有這支檢核 Agent：{agent_id}")
        return self.agents[agent_id]

    def pseudonym(self, value: Any) -> str:
        """Stable de-identified id for exports (HMAC, not reversible without the server secret)."""
        return hmac.new(self.secret, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def tables(self, agent: AgentSpec, agency: str, basis: str) -> rulekit.Tables:
        tables = rulekit.Tables()
        for name in agent.tables:
            version, rows = self.store.table_at(agency, agent.id, name, basis)
            tables.rows[name], tables.versions[name] = rows, version
        return tables

    def _basis(self, agent: AgentSpec, records: list[dict[str, Any]], basis: str) -> str:
        if basis:
            return basis
        if agent.date_field and records:
            value = rulekit.dig(records[0], agent.date_field)
            if isinstance(value, str) and len(value) >= 10:
                return value[:10]
        return datetime.now(TPE).date().isoformat()

    def _fingerprint(self, agent: AgentSpec, record: dict[str, Any], finding: dict[str, Any]) -> str:
        values = finding.get("fingerprint_values")
        if values:
            return sha256(canonical(values))
        if agent.person_field and agent.date_field:
            return sha256(canonical({"person": rulekit.dig(record, agent.person_field), "date": rulekit.dig(record, agent.date_field)}))
        return sha256(canonical({"agent": agent.id, "key": rulekit.dig(record, agent.business_key)}))

    def _sampled(self, agent: AgentSpec, key: str) -> bool:
        if agent.sample_rate <= 0:
            return False
        bucket = int(hmac.new(self.secret, f"sample:{agent.id}:{key}".encode(), hashlib.sha256).hexdigest()[:8], 16)
        return bucket / 0xFFFFFFFF < agent.sample_rate

    # --- sync ------------------------------------------------------------
    def check(self, agent_id: str, *, agency: str, records: list[dict[str, Any]], trigger: str, caller: str,
              basis: str = "", received: str = "", params: dict[str, Any] | None = None,
              prepared: Callable[[dict[str, Any]], tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]] | None]] | None = None) -> dict[str, Any]:
        agent = self.agent(agent_id)
        started = time.perf_counter()
        basis = self._basis(agent, records, basis)
        tables = self.tables(agent, agency, basis)
        run_id = self.store.start_run(agency=agency, agent=agent.id, agent_version=agent.version, agent_digest=agent.digest,
                                      table_versions=tables.versions, data_basis=basis, trigger=trigger, caller=caller,
                                      params=params, received=received)
        out = []
        try:
            for start in range(0, len(records), CHUNK):
                with self.store.transaction():
                    for record in records[start:start + CHUNK]:
                        out.append(self._one(agent, run_id, agency, caller, tables, dict(record), prepared))
        except Exception as error:
            elapsed = (time.perf_counter() - started) * 1000
            self.store.finish_run(run_id, status="error", records=len(out), elapsed_ms=elapsed, error=f"{type(error).__name__}: {error}")
            self.store.audit(caller, "check.run", agency=agency, target=run_id, detail={"agent": agent.id, "status": "error"})
            raise
        elapsed = (time.perf_counter() - started) * 1000
        self.store.finish_run(run_id, status="done", records=len(out), elapsed_ms=elapsed)
        self.store.audit(caller, "check.run", agency=agency, target=run_id,
                         detail={"agent": agent.id, "version": agent.version, "trigger": trigger, "records": len(out),
                                 "findings": sum(len(r["findings"]) for r in out), "tables": tables.versions, "basis": basis})
        return {"run_id": run_id, "agent": agent.id, "agent_version": agent.version, "data_basis": basis,
                "table_versions": tables.versions, "elapsed_ms": round(elapsed, 2), "results": out}


    def _one(self, agent: AgentSpec, run_id: str, agency: str, caller: str, tables: rulekit.Tables,
             record: dict[str, Any], prepared: Any) -> dict[str, Any]:
        extra_evidence: list[dict[str, Any]] = []
        override = None
        if prepared is not None:
            record, extra_evidence, override = prepared(record)
        key = str(rulekit.dig(record, agent.business_key, ""))
        findings = override if override is not None else evaluate(agent, record, tables)
        if not findings and self._sampled(agent, key):
            findings = [{"rule_id": "SAMPLE", "title": "人工抽審", "basis": f"AI風險抽審 {agent.sample_rate:.0%}",
                         "level": "sample", "message": "本筆無提示，依抽審比例列入人工抽審", "fields": [],
                         "evidence": [], "fingerprint_values": {"sample": agent.id, "key": key}}]
        input_hash = sha256(canonical(record))
        shown = []
        for finding in findings:
            finding["evidence"] = [*finding["evidence"], *extra_evidence]
            finding["fingerprint"] = self._fingerprint(agent, record, finding)
            case_id = self.store.case_for(agency=agency, agent=agent.id, business_key=key,
                                          fingerprint=finding["fingerprint"], reason=finding["message"], actor=caller)
            self.store.add_result(run_id=run_id, agency=agency, agent=agent.id, business_key=key,
                                  finding=finding, input_hash=input_hash, case_id=case_id)
            shown.append({k: finding[k] for k in ("rule_id", "title", "level", "message", "basis", "fields", "evidence")}
                         | {"case_id": case_id})
        return {"business_key": key, "findings": shown}

    # --- async (documents) -----------------------------------------------
    def submit(self, agent_id: str, *, agency: str, record: dict[str, Any], documents: list[dict[str, Any]],
               caller: str, received: str = "") -> str:
        agent = self.agent(agent_id)
        task_id = self.store.create_task(agency=agency, agent=agent.id, caller=caller)
        self.store.audit(caller, "check.submit", agency=agency, target=task_id, detail={"agent": agent.id, "documents": len(documents)})
        self._pools[agent.id].submit(self._work, agent, task_id, agency, record, documents, caller, received or now_iso())
        return task_id

    def _extract(self, agent: AgentSpec, record: dict[str, Any], documents: list[dict[str, Any]]
                 ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]] | None]:
        """(record with extracted fields, evidence, findings that replace the rules or None)."""
        if not documents:
            return record, [], None
        if self.extractor is None:
            return record, [], [_manual("DOC", "附件無法判讀", "附件辨識服務未設定，轉人工")]
        if not self.breaker.allow():
            return record, [], [_manual("DOC", "附件無法判讀", "附件辨識服務暫停（連續失敗），轉人工")]
        last: Exception | None = None
        for attempt in range(agent.retry_attempts):
            try:
                updates, evidence = self.extractor(agent, record, documents)
                self.breaker.ok()
                return {**record, **updates}, evidence, None
            except Exception as error:  # noqa: BLE001 -- any extractor failure degrades to manual review
                last = error
                self.breaker.fail()
                if attempt + 1 < agent.retry_attempts and self.breaker.allow():
                    time.sleep(min(agent.retry_backoff * (2 ** attempt), 60.0))
        return record, [], [_manual("DOC", "附件無法判讀", f"附件辨識失敗（{type(last).__name__}），轉人工")]

    def _work(self, agent: AgentSpec, task_id: str, agency: str, record: dict[str, Any],
              documents: list[dict[str, Any]], caller: str, received: str) -> None:
        self.store.set_task(task_id, state="running")
        try:
            result = self.check(agent.id, agency=agency, records=[record], trigger="async", caller=caller, received=received,
                                params={"task_id": task_id, "documents": len(documents)},
                                prepared=lambda rec: self._extract(agent, rec, documents))
            self.store.set_task(task_id, state="done", run_id=result["run_id"])
        except Exception as error:  # noqa: BLE001 -- recorded on the task, source system keeps going
            self.store.set_task(task_id, state="error", error=f"{type(error).__name__}: {error}")

    def shutdown(self) -> None:
        for pool in self._pools.values():
            pool.shutdown(wait=True)

    # --- validation cases (品質指標) ---------------------------------------
    def evaluate_cases(self, agent_id: str) -> dict[str, Any]:
        """Run the agent's validation cases against its test tables, without touching the store.

        rule coverage   rules hit by at least one case / all rules
        consistency     cases whose rule ids equal the agency's expected answer / all cases
        traceability    findings carrying rule id, basis and fields-or-evidence / all findings
        """
        agent = self.agent(agent_id)
        tables = rulekit.Tables(rows={name: list(agent.test_tables.get(name, [])) for name in agent.tables})
        hit: set[str] = set()
        details, agree, findings_total, traced = [], 0, 0, 0
        for case in agent.cases:
            got = evaluate(agent, case.record, tables)
            ids = sorted({f["rule_id"] for f in got})
            hit.update(ids)
            same = ids == sorted(set(case.expect))
            agree += same
            for finding in got:
                findings_total += 1
                traced += bool(finding["rule_id"] and finding["basis"] and (finding["fields"] or finding["evidence"]))
            details.append({"case": case.name, "expect": sorted(set(case.expect)), "got": ids, "ok": same})
        rule_ids = [r.id for r in agent.rules]

        def ratio(num: int, den: int) -> dict[str, Any]:
            return {"num": num, "den": den, "value": round(num / den, 4) if den else None}

        return {
            "agent": agent.id, "version": agent.version,
            "rule_coverage": ratio(len(hit & set(rule_ids)), len(rule_ids)),
            "consistency": ratio(agree, len(agent.cases)),
            "traceability": ratio(traced, findings_total),
            "uncovered_rules": [r for r in rule_ids if r not in hit],
            "cases": details,
        }
