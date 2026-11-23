"""`xharness check ...` -- the check platform from the command line.

  check serve                                         API + management page (loopback, port 3095)
  check agents                                        list agents and their rules
  check eval [agent]                                  validation cases -> coverage / consistency / traceability
  check run <agent> <agency> <file>                   daily batch over a .json list or .jsonl file
  check tables import <agent> <table> <agency> <file> <effective_from> <approver>
  check client add <name> <agency> [agent,agent]      issue a source-system API token (shown once)
  check client list | check client remove <name>
  check audit verify                                  walk the hash chain
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .api import DATE_RE, CheckService, serve


def _records(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def _pct(metric: dict[str, Any]) -> str:
    return "—" if metric["value"] is None else f"{metric['value']:.0%}（{metric['num']}/{metric['den']}）"


def main(settings: dict[str, Any], auth: dict[str, Any], argv: list[str], host: str = "127.0.0.1", port: int | None = None) -> int:
    sub = argv[0] if argv else "help"
    if sub == "serve":
        return serve(settings, auth, host=host, port=port)
    if sub not in {"agents", "eval", "run", "tables", "client", "audit"}:
        print(__doc__)
        return 0 if sub == "help" else 2
    service = CheckService(settings, auth)
    try:
        return _run(service, sub, argv[1:])
    except (LookupError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"xharness check: {error}", file=sys.stderr)
        return 1
    finally:
        service.engine.shutdown()


def _run(service: CheckService, sub: str, args: list[str]) -> int:
    store, engine = service.store, service.engine
    if sub == "agents":
        for agent in service.agents.values():
            print(f"{agent.id}  {agent.name}  v{agent.version}  風險 {agent.risk}  抽審 {agent.sample_rate:.0%}  {agent.digest[:12]}")
            for rule in agent.rules:
                print(f"    {rule.id}  {rule.title}  （{rule.basis}）")
        return 0
    if sub == "eval":
        failed = False
        for agent_id in args or list(service.agents):
            report = engine.evaluate_cases(agent_id)
            print(f"{agent_id}  規則覆蓋率 {_pct(report['rule_coverage'])}  回歸一致率 {_pct(report['consistency'])}  "
                  f"可追溯率 {_pct(report['traceability'])}")
            for case in report["cases"]:
                if not case["ok"]:
                    print(f"    不一致：{case['case']}  應為 {case['expect']}  實得 {case['got']}")
            if report["uncovered_rules"]:
                print(f"    沒有案例的規則：{', '.join(report['uncovered_rules'])}")
            failed |= any(report[k]["value"] not in (None, 1.0) for k in ("rule_coverage", "consistency", "traceability"))
        return 1 if failed else 0
    if sub == "run" and len(args) == 3:
        agent_id, agency, path = args
        result = engine.check(agent_id, agency=agency, records=_records(path), trigger="batch", caller="cli:batch",
                              params={"file": path})
        hits = sum(len(r["findings"]) for r in result["results"])
        print(f"{result['run_id']}  {len(result['results'])} 筆、{hits} 則提示、{result['elapsed_ms']:.0f} ms、"
              f"資料基準 {result['data_basis']}、判定值版本 {result['table_versions']}")
        return 0
    if sub == "tables" and len(args) == 7 and args[0] == "import":
        _, agent_id, name, agency, path, effective, approver = args
        agent = engine.agent(agent_id)
        if name not in agent.tables or not DATE_RE.match(effective):
            raise ValueError("表名不對，或生效日不是 YYYY-MM-DD")
        with open(path, encoding="utf-8") as handle:
            rows = service.clean_rows(agent, name, json.load(handle))
        version = store.put_table(agency=agency, agent=agent_id, name=name, rows=rows, effective_from=effective,
                                  approved_by=approver, note=f"CLI 匯入 {path}")
        store.audit(f"cli:{approver}", "table.publish", agency=agency, target=f"{agent_id}/{name}",
                    detail={"version": version, "effective_from": effective, "rows": len(rows), "source": "cli"})
        print(f"{agent_id}/{name} 第 {version} 版，{len(rows)} 列，{effective} 生效，核定人 {approver}")
        return 0
    if sub == "client" and args[:1] == ["add"] and len(args) in (3, 4):
        agents = [a for a in (args[3].split(",") if len(args) == 4 else []) if a]
        unknown = [a for a in agents if a not in service.agents]
        if unknown:
            raise ValueError(f"沒有這些 Agent：{', '.join(unknown)}")
        token = service.clients.add(args[1], args[2], agents)
        store.audit("cli", "client.add", agency=args[2], target=args[1], detail={"agents": agents})
        print(f"{args[1]}（{args[2]}）的金鑰只顯示這一次，請交給來源系統存進它的機密設定：\n{token}")
        return 0
    if sub == "client" and args[:1] == ["list"]:
        for client in service.clients.listing():
            print(f"{client['name']}  {client['agency']}  {','.join(client['agents']) or '全部 Agent'}  {client['created']}")
        return 0
    if sub == "client" and args[:1] == ["remove"] and len(args) == 2:
        ok = service.clients.remove(args[1])
        if ok:
            store.audit("cli", "client.remove", target=args[1])
        print("已撤銷" if ok else "沒有這個用戶端")
        return 0 if ok else 1
    if sub == "audit" and args == ["verify"]:
        result = store.verify_audit()
        print(f"稽核鏈 {'完整' if result['ok'] else '在第 ' + str(result['broken_at']) + ' 筆斷開'}，共 {result['count']} 筆通過")
        return 0 if result["ok"] else 1
    print(__doc__)
    return 2
