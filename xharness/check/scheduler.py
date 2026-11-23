"""Daily batch runs: at a time (Taipei), on chosen days, pull from a connector and run an agent.

A slot is one schedule on one date. claim_slot() is a UNIQUE insert, so with several processes on the
same database exactly one runs it. A slot missed while the server was down runs once when it comes back
the same day. Fetch failures retry three times (2, 4, 8 s); the run is marked failed with the reason and
can be re-run from the page. Folder files are archived only after the check succeeded.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

from .. import channels
from . import connectors
from .store import TPE

DAYS = {"daily": "每天", "weekdays": "週一到週五"}
WEEKDAY_NAMES = "一二三四五六日"


def due_today(days: str, now: datetime) -> bool:
    if days == "daily":
        return True
    if days == "weekdays":
        return now.isoweekday() <= 5
    try:
        return now.isoweekday() in {int(d) for d in days.split(",") if d.strip()}
    except ValueError:
        return False


def describe(days: str) -> str:
    if days in DAYS:
        return DAYS[days]
    try:
        return "每週" + "、".join(WEEKDAY_NAMES[int(d) - 1] for d in days.split(","))
    except (ValueError, IndexError):
        return days


def run_schedule(service: Any, schedule: dict[str, Any], slot: str, trigger: str = "schedule",
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    store = service.store
    row_id = store.claim_slot(schedule["id"], slot, trigger)
    if row_id is None:
        return {"status": "taken"}
    agency, agent_id = schedule["agency"], schedule["agent"]
    if agent_id not in service.agents or not service.enabled(agent_id):
        store.finish_slot(row_id, status="skipped", error="Agent 不存在或已停用")
        return {"status": "skipped"}
    connector = store.connector(schedule["connector"], agency, with_secret=True)
    if connector is None:
        store.finish_slot(row_id, status="failed", error="介接已被刪除")
        return {"status": "failed"}
    error, records, note = "", None, {}
    for attempt in range(3):
        try:
            secret = channels.decrypt(connector["secret"]) if connector.get("secret") else ""
            records, note = connectors.fetch(connector["config"], service.settings, secret)
            break
        except (connectors.ConnectorError, channels.ChannelError) as failure:
            error = str(failure)
            if attempt < 2:
                sleep(2 * (2 ** attempt))
    if records is None:
        store.finish_slot(row_id, status="failed", error=error)
        store.audit(f"schedule:{schedule['id']}", "schedule.failed", agency=agency, target=agent_id, detail={"slot": slot, "error": error[:200]})
        return {"status": "failed", "error": error}
    warning = ("讀不了、已移到 failed/：" + "、".join(note["failed_files"])) if note.get("failed_files") else ""
    status = "partial" if warning else "done"
    if not records:
        store.finish_slot(row_id, status=status, records=0, snapshot=note, error=warning)
        return {"status": status, "records": 0}
    try:
        result = service.engine.check(agent_id, agency=agency, records=records, trigger="batch", caller=f"schedule:{schedule['id']}",
                                      params={"schedule": schedule["id"], "slot": slot, "snapshot": note})
    except Exception as failure:  # noqa: BLE001 -- recorded on the slot; files stay in the inbox for the retry
        store.finish_slot(row_id, status="failed", records=len(records), snapshot=note, error=f"{type(failure).__name__}: {failure}")
        return {"status": "failed"}
    connectors.archive(connector["config"], service.settings, note)
    store.finish_slot(row_id, status=status, run_id=result["run_id"], records=len(records), snapshot=note, error=warning)
    return {"status": status, "run_id": result["run_id"], "records": len(records)}


def tick(service: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(TPE)
    out = []
    for schedule in service.store.schedules(None):
        if not schedule["enabled"] or not due_today(schedule["days"], now) or now.strftime("%H:%M") < schedule["at"]:
            continue
        slot = now.strftime("%Y-%m-%d")
        if schedule.get("last") and schedule["last"]["slot"] == slot:
            continue
        out.append(run_schedule(service, schedule, slot))
    return out


class Scheduler(threading.Thread):
    def __init__(self, service: Any, every: float = 30.0):
        super().__init__(name="check-scheduler", daemon=True)
        self.service, self.every, self.stop = service, every, threading.Event()

    def run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                tick(self.service)
            except Exception as error:  # noqa: BLE001 -- the loop must survive one bad schedule
                print(f"[xharness check] 排程錯誤：{type(error).__name__}: {error}")
