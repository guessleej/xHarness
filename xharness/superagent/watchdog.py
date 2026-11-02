"""The Watchdog: a control loop outside the agents.

A process that is alive is not an agent that is working: many failures are a
model looping on one tool call or drifting from its ticket. So besides the
infrastructure (Sources failing, stored work failing again and again, a worker
whose conversation died), it looks at each running ticket's progress:

  stalled   a running assignment with no change for `stall_minutes`
  looping   the worker's last calls are the same tool with the same arguments
  dead      the assignment runs but its worker's thread is gone

It heals what it safely can -- a dead worker is launched again; a stalled one
is asked for its status -- and tells a person (the node's notification
channels), once per problem per hour. With `judge` on, a separate watchdog
conversation is asked whether a stalled ticket still makes real progress, and
its verdict is recorded beside the problem; it can read, never act.
"""

from __future__ import annotations

import time
from typing import Any

from .model import Doc, live, now_iso, running_assignment

CHECK_SECONDS = 60
NOTIFY_EVERY_SECONDS = 3600
DEFAULTS = {"stall_minutes": 30, "loop_calls": 6, "judge": False}


def _age_minutes(iso: str) -> float:
    from datetime import datetime
    try:
        return (time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) / 60
    except (ValueError, AttributeError):
        return 0.0


class Watchdog:
    def __init__(self, sa: Any) -> None:
        self.sa = sa
        self.last = 0.0
        self.findings: list[Doc] = []

    @property
    def settings(self) -> Doc:
        return {**DEFAULTS, **(self.sa.settings.get("watchdog") or {})}

    def due(self) -> bool:
        return time.time() - self.last >= CHECK_SECONDS

    def check(self) -> list[Doc]:
        self.last = time.time()
        found: list[Doc] = []
        settings = self.settings
        for sid, source in self.sa.sources.items():
            health = getattr(source, "health", {}) or {}
            if health.get("state") == "error":
                found.append({"key": f"source:{sid}", "kind": "source", "text": f"來源 {sid} 連不上：{health.get('detail', '')}"})
        for effect in self.sa.store.effects():
            if effect["attempts"] >= 3:
                found.append({"key": f"effect:{effect['kind']}:{effect['key'][:12]}", "kind": "work",
                              "text": f"待辦「{effect['kind']}」已失敗 {effect['attempts']} 次：{effect['error'][:200]}"})
        for ticket in self.sa.store.tickets_open():
            running = running_assignment(ticket)
            if not running:
                continue
            worker = self.sa.workers.get(ticket["id"])
            title = ticket["title"][:60]
            if worker is None or worker.stopped or not worker.thread.is_alive():
                self.sa.store.effect_add("launch", ticket["id"], {"ticketId": ticket["id"], "assignmentId": running["id"],
                                                                  "reopen": True})
                found.append({"key": f"dead:{ticket['id']}", "kind": "healed", "ticketId": ticket["id"],
                              "text": f"工單「{title}」的工作代理不見了，已重新開啟。"})
                continue
            calls = self._recent_calls(worker)
            if len(calls) >= settings["loop_calls"] and len(set(calls[-settings["loop_calls"]:])) == 1:
                found.append({"key": f"loop:{ticket['id']}", "kind": "loop", "ticketId": ticket["id"],
                              "text": f"工單「{title}」的工作代理連續 {settings['loop_calls']} 次做同一個動作，可能卡在迴圈。"})
            busy = worker.busy
            idle_since = max(ticket["updatedAt"], running.get("idleAt") or "")
            if busy and _age_minutes(idle_since) >= settings["stall_minutes"]:
                verdict = self._judge(ticket) if settings["judge"] else None
                found.append({"key": f"stall:{ticket['id']}", "kind": "stall", "ticketId": ticket["id"], "verdict": verdict,
                              "text": f"工單「{title}」已經 {int(_age_minutes(idle_since))} 分鐘沒有進展。"
                                      + (f"判斷：{verdict}" if verdict else "")})
        self.findings = found
        self._notify(found)
        return found

    @staticmethod
    def _recent_calls(worker: Any) -> list[str]:
        recent = getattr(worker.handle, "recent_calls", None)
        try:
            return list(recent()) if callable(recent) else []
        except Exception:  # noqa: BLE001
            return []

    def _judge(self, ticket: Doc) -> str | None:
        """A separate, read-only conversation says whether the ticket still makes real progress."""
        judge = getattr(self.sa.host, "judge", None)
        if not callable(judge):
            return None
        report = (running_assignment(ticket) or {}).get("report") or {}
        transcript = getattr(self.sa.workers.get(ticket["id"]).handle, "transcript", lambda: "")()
        try:
            return str(judge(f"Ticket: {ticket['title']}\nScope: {ticket['mandate']['scope']}\n"
                             f"Latest report: {report.get('state', 'none')} {report.get('summary', '')}\n"
                             f"Recent work:\n{transcript[-6000:]}\n\n"
                             "Answer in one line, in Traditional Chinese: is this worker making real progress on its scope, "
                             "stuck, or drifting away from it? Start with 進展中, 卡住 or 偏離, then one short reason."))[:200]
        except Exception:  # noqa: BLE001 - a judgement is advice; its failure is not a problem of the ticket
            return None

    def _notify(self, found: list[Doc]) -> None:
        sent = self.sa.store.kv("watchdog:sent", {}) or {}
        now = time.time()
        fresh = [f for f in found if now - float(sent.get(f["key"], 0)) >= NOTIFY_EVERY_SECONDS]
        for finding in fresh:
            sent[finding["key"]] = now
            if finding["kind"] == "stall" and finding.get("ticketId"):
                self.sa.engine.request_status(finding["ticketId"])
            self.sa.store.log_event("watchdog." + finding["kind"], now_iso(), {"key": finding["key"], "text": finding["text"]})
        if fresh:
            self.sa.host.notify("external", "常駐代理看門狗：\n" + "\n".join(f"・{f['text']}" for f in fresh))
        self.sa.store.set_kv("watchdog:sent", {k: v for k, v in sent.items() if now - float(v) < 86400})


def open_problems(watchdog: Watchdog) -> list[Doc]:
    return [{"kind": f"watchdog {f['kind']}", "error": f["text"], "at": now_iso()} for f in watchdog.findings
            if f["kind"] != "healed"]


__all__ = ["Watchdog", "open_problems", "live"]
