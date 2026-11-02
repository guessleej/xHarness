"""The resident agent's HTTP surface (admin only, mounted by web.py under /api/superagent).

GET  /api/superagent                 overview: what waits for a person first, problems, counts
GET  /api/superagent/tickets?state=  open | ended | all
GET  /api/superagent/ticket/<id>     one ticket with its inputs, threads and decision
GET  /api/superagent/threads         threads with recent activity and where each message went
GET  /api/superagent/decisions?kind=
GET  /api/superagent/settings        settings (tokens never returned, only whether one is set) + versions
POST /api/superagent                 {action: save | pause | resume | stop | retry | requeue | unsubscribe | dismiss, ...}
"""

from __future__ import annotations

from typing import Any

from .model import UserError, reason_text, ticket_stage
from .policy import SettingsError

Doc = dict[str, Any]


def get(sa: Any, parts: list[str], query: dict[str, list[str]]) -> tuple[int, Doc]:
    arg = lambda name, default="": (query.get(name) or [default])[0]  # noqa: E731
    if not parts:
        return 200, sa.overview()
    if parts == ["tickets"]:
        state = arg("state", "open")
        rows = sa.store.tickets(limit=200, ended=None if state == "all" else state == "ended")
        return 200, {"tickets": [sa.ticket_view(t) for t in rows]}
    if len(parts) == 2 and parts[0] == "ticket":
        try:
            ticket = sa._find_ticket(parts[1])
        except UserError:
            return 404, {"error": "no such ticket"}
        decision = sa.store.decision(ticket["decisionId"]) or {}
        threads = [t["key"] for t in sa.store.threads(limit=500, subscribed=True) if ticket["id"] in t["subscriptions"]]
        inputs = []
        for item in ticket["inputs"][-60:]:
            row = {"kind": item["kind"], "at": item["at"], "delivered": item["id"] not in ticket["undelivered"]}
            if item["kind"] == "trigger":
                trigger = sa.store.trigger(item["triggerId"]) or {}
                row.update({"actor": (trigger.get("actor") or {}).get("id", ""), "text": (trigger.get("text") or "")[:500],
                            "url": trigger.get("url", ""), "context": bool(item.get("context"))})
            else:
                row["text"] = str(item.get("text") or item.get("effect") or "")[:500]
            inputs.append(row)
        return 200, {**sa.ticket_view(ticket), "mandate": ticket["mandate"], "threads": threads, "inputs": inputs,
                     "assignments": ticket["assignments"], "follows": ticket.get("follows", []),
                     "decision": {"kind": decision.get("kind"), "reason": reason_text(decision.get("reason")),
                                  "audit": decision.get("audit")}}
    if parts == ["threads"]:
        rows = []
        for thread in sa.store.threads(limit=int(arg("limit", "60") or 60)):
            triggers = sa.store.triggers_on(thread["key"], 8)
            rows.append({"key": thread["key"], "lastAt": thread.get("lastAt"), "subscriptions": thread["subscriptions"],
                         "evaluating": bool(thread.get("evaluating")), "pending": len(thread.get("pending", [])),
                         "triggers": [_trigger_row(sa, t) for t in triggers]})
        return 200, {"threads": rows}
    if parts == ["decisions"]:
        rows = sa.store.decisions(200, arg("kind"))
        return 200, {"decisions": [{"id": d["id"], "at": d["at"], "kind": d["kind"], "thread": d["threadKey"],
                                    "strength": d["strength"], "reason": reason_text(d["reason"]), "ticketId": d.get("ticketId"),
                                    "wouldAccept": bool((d.get("audit") or {}).get("would_accept")),
                                    "rules": [m["rule_id"] for m in (d.get("audit") or {}).get("matches", [])]} for d in rows]}
    if parts == ["settings"]:
        revision = int(sa.store.kv("settings-revision", 0))
        versions = []
        for number in range(revision, max(0, revision - 10), -1):
            saved = sa.store.kv(f"settings-version:{number}") or {}
            versions.append({"revision": number, "at": saved.get("at"), "origin": saved.get("origin"), "user": saved.get("user")})
        return 200, {"settings": sa.public_settings(), "versions": versions}
    return 404, {"error": "not found"}


def _trigger_row(sa: Any, trigger: Doc) -> Doc:
    record = sa.store.disposition(trigger["id"]) or {}
    decision = sa.store.decision(record["decisionId"]) if record.get("decisionId") else None
    where = "filtered" if record.get("filtered") else ("ticket" if record.get("fanout") else
                                                       (decision["kind"] if decision else ("evaluating" if record.get("evalId") else "queued")))
    return {"id": trigger["id"], "at": trigger["at"], "actor": (trigger.get("actor") or {}).get("id", ""),
            "human": bool(trigger.get("human")), "addressed": trigger.get("addressed"), "text": (trigger.get("text") or "")[:300],
            "url": trigger.get("url", ""), "where": where, "tickets": record.get("fanout", []),
            "reason": reason_text(decision["reason"]) if decision else reason_text(record.get("filtered"))}


def post(sa: Any, body: Doc, who: str) -> tuple[int, Doc]:
    action = str(body.get("action") or "")
    try:
        if action == "save":
            settings = body.get("settings")
            if not isinstance(settings, dict):
                return 400, {"error": "settings must be an object"}
            sa.save_settings(settings, origin="page", user=who)
            return 200, {"settings": sa.public_settings()}
        if action in ("pause", "resume"):
            sa.save_settings({**sa.settings, "paused": action == "pause"}, origin="page", user=who)
            return 200, {"paused": sa.settings["paused"]}
        if action == "stop":
            ticket = sa._find_ticket(str(body.get("ticket", "")))
            sa.engine.stop_ticket(ticket["id"], str(body.get("reason") or f"stopped by {who} on the page")[:300])
            return 200, {"ok": True}
        if action == "retry":
            sa.engine.retry_ticket(sa._find_ticket(str(body.get("ticket", "")))["id"])
            return 200, {"ok": True}
        if action == "requeue":
            return 200, {"ok": sa.engine.reevaluate(sa._find_trigger(str(body.get("trigger", "")))["id"])}
        if action == "unsubscribe":
            return 200, {"ok": sa.engine.unsubscribe(str(body.get("thread", "")))}
        if action == "dismiss":
            sa.engine.dismiss(str(body.get("decision", "")))
            return 200, {"ok": True}
        if action == "clear-problem":
            sa.store.kv_delete(f"problem:{body.get('kind', '')}")
            return 200, {"ok": True}
    except (UserError, SettingsError, ValueError) as error:
        return 400, {"error": str(error)}
    return 400, {"error": "action: save, pause, resume, stop, retry, requeue, unsubscribe, dismiss or clear-problem"}


__all__ = ["get", "post", "ticket_stage"]
