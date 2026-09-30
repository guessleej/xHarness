"""Service usage report: what an IT office, a dean or a customer's management
asks at the end of the month, computed from what the harness already wrote
down rather than from a separate analytics store.

Six blocks, every one of them answerable:
  1. how many people used it        4. whether the hardware was used
  2. what it cost                   5. whether outbound integrations are safe
  3. self-hosted vs bought-in       6. system health and governance

Everything is derived from the session transcripts on disk, the access
audit log and the resolved configuration. Estimates carry their assumption
with them, and a figure nobody collected is reported as not collected
rather than as zero. Block six deliberately reports the system's own
problems: unattributed usage, unreachable nodes, failed sign-ins.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .identity import UserStore
from .sandbox import resolve_sandbox
from .session import session_owners
from .usage import parse_since, task_records

TOP_N = 10
GOVERNANCE_RULE = (
    "帳號一律停用不刪除（離職、畢業、轉單位都是）：帳號刪掉後，歷史用量就歸不了戶，稽核也查不回是誰做的。"
)


def _session_meta(directory: str, session_id: str) -> dict[str, Any]:
    """First line of a transcript, which the session plugin writes as type=meta."""
    path = os.path.join(directory, f"{session_id}.jsonl")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("type") == "meta":
                    return record
                break  # meta is always first; anything else means an older transcript
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _count_events(directory: str, session_id: str, kinds: tuple[str, ...]) -> dict[str, int]:
    path = os.path.join(directory, f"{session_id}.jsonl")
    counts = {kind: 0 for kind in kinds}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    kind = json.loads(line).get("type")
                except json.JSONDecodeError:
                    continue
                if kind in counts:
                    counts[kind] += 1
    except OSError:
        return counts
    return counts


def collect(
    config: Any,
    since: str = "30d",
    users: UserStore | None = None,
    nodes: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the six blocks. Pure reading; safe to call from a request handler."""
    now = now or datetime.now(timezone.utc)
    start = now - parse_since(since)

    per_user: dict[str, dict[str, Any]] = {}
    hosting: dict[str, dict[str, int]] = {}
    models: dict[str, int] = {}
    errors = 0
    budget_stops = 0
    unattributed = {"tokens": 0, "tasks": 0}

    for user, directory in session_owners():
        label = user or "(未歸戶)"
        records = task_records(directory, start)
        if not records:
            continue
        bucket = per_user.setdefault(
            label, {"user": label, "attributed": user is not None, "tasks": 0, "tokens": 0, "calls": 0, "tool_calls": 0, "sessions": 0}
        )
        seen: set[str] = set()
        for record in records:
            bucket["tasks"] += 1
            bucket["tokens"] += int(record.get("total_tokens") or 0)
            bucket["calls"] += int(record.get("calls") or 0)
            bucket["tool_calls"] += int(record.get("tool_calls") or 0)
            session_id = str(record.get("session") or "")
            if session_id in seen:
                continue
            seen.add(session_id)
            meta = _session_meta(directory, session_id)
            kind = str(meta.get("hosting") or "unknown")
            slot = hosting.setdefault(kind, {"tokens": 0, "sessions": 0})
            slot["sessions"] += 1
            model = str(meta.get("model") or "unknown")
            models[model] = models.get(model, 0) + 1
            counts = _count_events(directory, session_id, ("error", "budget-stop"))
            errors += counts["error"]
            budget_stops += counts["budget-stop"]
        bucket["sessions"] = len(seen)
        # Attribute this session's tokens to its hosting kind proportionally is
        # overkill; a session uses one endpoint, so charge its whole delta there.
        for session_id in seen:
            meta = _session_meta(directory, session_id)
            kind = str(meta.get("hosting") or "unknown")
            tokens = sum(int(r.get("total_tokens") or 0) for r in records if r.get("session") == session_id)
            hosting.setdefault(kind, {"tokens": 0, "sessions": 0})["tokens"] += tokens
        if user is None:
            unattributed = {"tokens": bucket["tokens"], "tasks": bucket["tasks"]}

    people = sorted(per_user.values(), key=lambda item: item["tokens"], reverse=True)
    totals = {
        "tokens": sum(item["tokens"] for item in people),
        "tasks": sum(item["tasks"] for item in people),
        "calls": sum(item["calls"] for item in people),
        "tool_calls": sum(item["tool_calls"] for item in people),
        "sessions": sum(item["sessions"] for item in people),
    }

    accounts = users.accounts() if users and users.enabled else []
    audit = users.audit_tail(500) if users and users.enabled else []
    window_audit = [row for row in audit if str(row.get("ts", "")) >= start.isoformat()]
    failed_signins = sum(1 for row in window_audit if row.get("action") == "sign-in" and not row.get("ok"))
    lockouts = sum(1 for row in window_audit if "locked out" in str(row.get("detail", "")))

    rate = (getattr(config, "telemetry", {}) or {}).get("cost_per_1k_tokens")
    cost = {
        "collected": rate is not None,
        "rate_per_1k_tokens": rate,
        "estimate": round(totals["tokens"] / 1000 * float(rate), 2) if rate else None,
        "assumption": (
            f"以設定值 cost_per_1k_tokens = {rate} 換算，地端自建推論不另計電力與折舊"
            if rate
            else "本期未收集：設定檔未設 [telemetry] cost_per_1k_tokens，故不估算金額"
        ),
    }

    outbound = _outbound_posture(config, users)
    machines = _machines(config, nodes, totals)

    return {
        "generated": now.isoformat(),
        "since": since,
        "from": start.isoformat(),
        "to": now.isoformat(),
        "version": __version__,
        "people": {
            "active": len(people),
            "accounts": len(accounts),
            "disabled_accounts": sum(1 for row in accounts if row.get("disabled")),
            "identity": "on" if (users and users.enabled) else "off",
            "top": people[:TOP_N],
        },
        "spend": {**totals, "cost": cost, "models": sorted(models.items(), key=lambda kv: -kv[1])[:TOP_N]},
        "hosting": {
            "self_hosted": hosting.get("self-hosted", {"tokens": 0, "sessions": 0}),
            "external": hosting.get("external", {"tokens": 0, "sessions": 0}),
            "unknown": hosting.get("unknown", {"tokens": 0, "sessions": 0}),
        },
        "machines": machines,
        "outbound": outbound,
        "health": {
            "errors": errors,
            "budget_stops": budget_stops,
            "failed_signins": failed_signins,
            "lockouts": lockouts,
            "unattributed": unattributed,
            "issues": _issues(users, unattributed, outbound, machines, errors, failed_signins),
            "governance": GOVERNANCE_RULE,
        },
    }


def _outbound_posture(config: Any, users: UserStore | None) -> dict[str, Any]:
    """Block five: every way this install can reach something other than its model."""
    mode = str((getattr(config, "sandbox", {}) or {}).get("mode", "auto"))
    # `auto` is a promise only where a backend actually probes clean. Asking the
    # configuration is not enough: bwrap is often installed but unusable (Ubuntu
    # 24.04 restricts unprivileged user namespaces), and a report that called
    # that "safe" would be telling the customer the opposite of the truth.
    try:
        resolved = resolve_sandbox(getattr(config, "sandbox", None))
        active = resolved.name if resolved else None
    except (RuntimeError, ValueError):
        active = None
    sandbox_detail = {
        "require": f"沒有後端就拒絕啟動，目前生效的是 {active}。",
        "auto": (f"目前生效的是 {active}。" if active
                 else "這台機器沒有可用的沙箱後端（常見原因：未裝 bwrap，或 Ubuntu 24.04 停用了未授權的 user namespaces），bash 工具的指令並未被圈住。"),
        "off": "沙箱關閉，bash 工具可以寫到工作目錄以外的任何地方。",
    }.get(mode, "設定值無法辨識。")
    items = [
        {
            "name": "模型端點",
            "state": "on",
            "detail": str((getattr(config, "provider", {}) or {}).get("base_url", "")).split("/v1")[0],
            "safe": True,
        },
        {"name": "webfetch 對外抓取", "state": "on" if getattr(config, "webfetch", False) else "off",
         "detail": "每次抓取都走審批", "safe": True},
        {"name": "真瀏覽器搜尋", "state": "on" if getattr(config, "browser", None) else "off",
         "detail": "地端 camofox，不經第三方搜尋 API", "safe": True},
        {"name": "MCP 外部工具", "state": "on" if getattr(config, "mcp_servers", None) else "off",
         "detail": f"{len(getattr(config, 'mcp_servers', None) or {})} 個 server，未標唯讀者一律走審批", "safe": True},
        {"name": "Telegram 通道", "state": "on" if (getattr(config, "channels", {}) or {}).get("telegram") else "off",
         "detail": "只服務 allowed_chats 名單", "safe": True},
        {"name": "沙箱", "state": f"{mode}（{active}）" if active else f"{mode}（無後端）",
         "detail": sandbox_detail, "safe": bool(active)},
        {"name": "使用者身分", "state": "on" if (users and users.enabled) else "off",
         "detail": "關閉時用量與稽核歸不到人", "safe": bool(users and users.enabled)},
        {"name": "遙測外傳", "state": "off", "detail": "xHarness 不對外回傳任何使用資料", "safe": True},
    ]
    return {"items": items, "unsafe": [item["name"] for item in items if not item["safe"]]}


def _machines(config: Any, nodes: list[dict[str, Any]] | None, totals: dict[str, int]) -> dict[str, Any]:
    """Block four: is the hardware anyone bought actually being used?"""
    configured = list((getattr(config, "fleet", {}) or {}).get("nodes", {}) or {})
    reported = nodes or []
    rows = [{"node": "本機", "reachable": True, "tokens": totals["tokens"], "conversations": None}]
    for item in reported:
        rows.append(
            {
                "node": str(item.get("node") or item.get("name") or "?"),
                "reachable": not item.get("error"),
                "tokens": ((item.get("summary") or {}).get("total_tokens")
                           if isinstance(item.get("summary"), dict) else None),
                "conversations": ((item.get("summary") or {}).get("conversations")
                                  if isinstance(item.get("summary"), dict) else None),
            }
        )
    return {
        "configured_nodes": len(configured),
        "rows": rows,
        "unreachable": [row["node"] for row in rows if not row["reachable"]],
        "idle": [row["node"] for row in rows if row["tokens"] == 0],
    }


def _issues(
    users: UserStore | None,
    unattributed: dict[str, int],
    outbound: dict[str, Any],
    machines: dict[str, Any],
    errors: int,
    failed_signins: int,
) -> list[dict[str, str]]:
    """The 'we are not only reporting good news' list."""
    issues: list[dict[str, str]] = []
    if not (users and users.enabled):
        issues.append({
            "item": "用量歸不到人",
            "detail": "尚未啟用 [auth]，本期所有用量都記在共用帳上，無法回答「誰用掉的」。",
            "action": "在設定檔加入 [auth] 並建立帳號（xharness users add）。",
        })
    elif unattributed["tokens"]:
        issues.append({
            "item": "仍有未歸戶用量",
            "detail": f"本期有 {unattributed['tokens']} tokens／{unattributed['tasks']} 個任務落在共用目錄，多半來自直接執行 CLI 的人。",
            "action": "請這些使用者改由 Web UI 登入後操作，或為排程任務建立專用帳號。",
        })
    for item in outbound["items"]:
        if item["safe"]:
            continue
        issues.append({
            "item": f"{item['name']} 未達建議設定",
            "detail": item["detail"],
            "action": ("裝上 bwrap 並確認 user namespaces 可用，或依機關政策改寫 AppArmor profile；"
                       "見交付指南「沙箱」一節。" if item["name"] == "沙箱"
                       else "見「對外串接」一節的說明欄。"),
        })
    for node in machines["unreachable"]:
        issues.append({
            "item": f"節點無法連線：{node}",
            "detail": "hub 這次彙整時連不上這個節點，它的用量沒有計入。",
            "action": "確認該機器的 xharness-node 服務是否在跑。",
        })
    for node in machines["idle"]:
        if node != "本機":
            issues.append({
                "item": f"節點本期零用量：{node}",
                "detail": "設備在線但整個期間沒有任何任務，可能是沒人知道它可以用。",
                "action": "確認該節點的用途，或回收算力給其他單位。",
            })
    if errors:
        issues.append({
            "item": f"本期有 {errors} 次任務失敗",
            "detail": "任務在執行中丟出例外而中止。",
            "action": "查對應 session 記錄的 error 事件。",
        })
    if failed_signins:
        issues.append({
            "item": f"本期有 {failed_signins} 次登入失敗",
            "detail": "少量屬正常打錯密碼；短時間大量失敗要視為嘗試入侵。",
            "action": "查 access-audit.jsonl，必要時封鎖來源。",
        })
    return issues


def format_report(report: dict[str, Any]) -> str:
    """Terminal rendering, same numbers as the Word version."""
    people, spend, hosting = report["people"], report["spend"], report["hosting"]
    health = report["health"]
    lines = [
        f"服務使用報告 — 近 {report['since']}（產生於 {report['generated'][:19].replace('T', ' ')} UTC）",
        "",
        f"一、多少人在用：{people['active']} 人有實際用量，開通 {people['accounts']} 個帳號"
        f"（停用 {people['disabled_accounts']} 個），身分驗證 {people['identity']}",
    ]
    for row in people["top"][:5]:
        lines.append(f"      {row['user']:<16} {row['tokens']:>10,} tokens  {row['tasks']:>4} 任務")
    lines += [
        "",
        f"二、花多少：{spend['tokens']:,} tokens、{spend['calls']:,} 次模型呼叫、{spend['tool_calls']:,} 次工具呼叫",
        f"      {spend['cost']['assumption']}"
        + (f"，估算 {spend['cost']['estimate']:,}" if spend["cost"]["estimate"] else ""),
        "",
        f"三、自建 vs 外購：地端自建 {hosting['self_hosted']['tokens']:,} tokens／"
        f"外購 API {hosting['external']['tokens']:,} tokens"
        + (f"／來源未記錄 {hosting['unknown']['tokens']:,} tokens（1.4.0 之前建立的工作階段沒有記端點）"
           if hosting["unknown"]["tokens"] else ""),
        "",
        f"四、設備用到沒：設定 {report['machines']['configured_nodes']} 個節點"
        + (f"，連不上 {len(report['machines']['unreachable'])} 個" if report["machines"]["unreachable"] else "，全部可連線"),
        "",
        "五、對外串接：" + "、".join(f"{item['name']}={item['state']}" for item in report["outbound"]["items"][:6]),
        "",
        f"六、健康與治理：{health['errors']} 次任務失敗、{health['budget_stops']} 次預算中斷、"
        f"{health['failed_signins']} 次登入失敗",
    ]
    if health["issues"]:
        for issue in health["issues"]:
            lines.append(f"      待處理：{issue['item']} — {issue['action']}")
    else:
        lines.append("      本期無異常")
    lines.append(f"      治理：{health['governance']}")
    return "\n".join(lines)
