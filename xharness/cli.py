"""The xharness CLI: headless one-shot, interactive REPL, session listing."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any
from datetime import datetime

from . import __version__
from .agent import Agent, AgentOptions, default_system_prompt
from .config import load_config
from .evals import format_report, load_cases, run_suite, write_results
from .fleet import format_table, load_nodes, poll_all, poll_usage_all
from .usage import format_history, history
from .consolidate import apply_plan, apply_verdicts, build_plan, verify_plan
from .memory import MemoryStore, default_memory_dir
from .identity import UserStore
from .report import collect as collect_report, format_report as format_usage_report
from .providers import PRESETS, probe_provider
from .web import serve
from .presets import build_harness
from .session import SessionLog, messages_from_events
from .tools import ToolRegistry


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xharness",
        description="xHarness: a plugin-based agent harness by xCloudinfo",
        epilog=(
            "environment: XHARNESS_HOME (state dir, default ~/.xharness), "
            "XHARNESS_BASE_URL / XHARNESS_MODEL / XHARNESS_API_KEY "
            "(provider when no config file exists)"
        ),
    )
    parser.add_argument(
        "task",
        nargs="*",
        help='the task; "sessions" lists saved sessions; "users" manages accounts; "report" prints the service usage report; "eval <path>" runs a suite; "web" starts the local UI; "desktop" opens it in a native window; "memory [list|topics|show|search|audit|consolidate|stale|verify|expire]" inspects, tidies, and re-verifies memory; "providers [presets|probe]" lists and probes endpoints; "fleet" polls the configured nodes; "usage" shows token history; empty starts a REPL',
    )
    parser.add_argument("--config", dest="config_path", help="config file (default: ./xharness.toml, then $XHARNESS_HOME/config.toml)")
    parser.add_argument("--provider", help="provider from the config's [providers] table")
    parser.add_argument("--model", help="override the provider's model")
    parser.add_argument("--resume", help="continue a saved session id")
    parser.add_argument("--max-turns", type=int, dest="max_turns", help="max model turns per task (default 40)")
    parser.add_argument("--approve", choices=["prompt", "auto"], help="approval for mutating tools")
    parser.add_argument("-y", "--yes", action="store_true", help="shorthand for --approve auto")
    parser.add_argument(
        "--no-instructions",
        action="store_true",
        help="do not read AGENTS.md / CLAUDE.md from the working directory",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web: bind address (non-loopback requires --token)")
    parser.add_argument("--port", type=int, default=3080, help="web: port (default 3080)")
    parser.add_argument("--token", help="web: bearer token for the API (or XHARNESS_WEB_TOKEN / XHARNESS_WEB_TOKEN_FILE)")
    parser.add_argument("--no-open", action="store_true", dest="no_open", help="web: do not open a browser")
    parser.add_argument("--apply", action="store_true", help="memory consolidate: execute the plan (default: dry run)")
    parser.add_argument("--llm", action="store_true", dest="use_llm", help="memory consolidate: also ask the model for proposals")
    parser.add_argument("--since", default="7d", help="usage: window such as 24h, 7d, 30d (default 7d)")
    parser.add_argument("--bucket", choices=["hour", "day"], help="usage: bucket size (default by window)")
    parser.add_argument("--nodes", action="store_true", help="usage: also fetch every configured fleet node")
    parser.add_argument("--repeat", type=int, default=1, help="eval: run each case N times")
    parser.add_argument("--json", dest="json_out", help="eval: write per-attempt results as JSONL")
    parser.add_argument("--role", choices=["admin", "user"], default="user", help="users add: the new account's role")
    parser.add_argument("--display", help="users add: display name shown in the UI")
    parser.add_argument("-V", "--version", action="version", version=__version__)
    return parser


def _list_sessions() -> None:
    sessions = SessionLog.list()
    if not sessions:
        print("no sessions")
        return
    for entry in sessions:
        stamp = datetime.fromtimestamp(entry["mtime"]).astimezone().isoformat(timespec="seconds")
        print(f"{entry['id']}\t{stamp}")


def _web_token(explicit: str | None) -> str | None:
    """--token, else XHARNESS_WEB_TOKEN, else the contents of XHARNESS_WEB_TOKEN_FILE."""
    if explicit:
        return explicit
    from_env = os.environ.get("XHARNESS_WEB_TOKEN")
    if from_env:
        return from_env
    path = os.environ.get("XHARNESS_WEB_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    return None


def _run_usage(args: argparse.Namespace, config: Any) -> int:
    report = history(since=args.since, bucket=args.bucket)
    print(format_history(report, "local"))
    if args.nodes:
        for node in poll_usage_all(load_nodes(config.fleet), args.since, report["bucket"]):
            print()
            if node["ok"]:
                print(format_history(node["history"], node["name"]))
            else:
                print(f"{node['name']}: unreachable  {node['url']}  {node.get('error', '')}")
    return 0


def _run_fleet(config: Any) -> int:
    nodes = load_nodes(config.fleet)
    if not nodes:
        print("no fleet nodes configured; add [fleet.nodes.<name>] url = ... to xharness.toml", file=sys.stderr)
        return 2
    reports = poll_all(nodes)
    print(format_table(reports))
    return 0 if all(report["ok"] for report in reports) else 1


def _run_providers(args: argparse.Namespace, config: Any) -> int:
    action = args.task[1] if len(args.task) > 1 else "probe"
    if action == "presets":
        width = max(len(name) for name in PRESETS)
        for name, preset in PRESETS.items():
            scope = "local" if preset.get("local") else "hosted"
            key = preset.get("api_key_env", "-")
            print(f"{name.ljust(width)}  {scope:6}  {preset['base_url']:36}  key env: {key}")
        return 0
    if action == "probe":
        names = args.task[2:] or list(config.providers) or [config.provider_name]
        failures = 0
        for name in names:
            provider = config.providers.get(name) or (config.provider if name == config.provider_name else None)
            if provider is None:
                print(f"{name}: not in config", file=sys.stderr)
                failures += 1
                continue
            result = probe_provider(name, provider)
            if result.ok:
                shown = ", ".join(result.models[:12]) + (" ..." if len(result.models) > 12 else "")
                print(f"{name}: ok  {result.base_url}  models: {shown or '(none advertised)'}")
            else:
                failures += 1
                print(f"{name}: unreachable  {result.base_url}  {result.detail}")
        return 1 if failures else 0
    print("usage: xharness providers [presets | probe [name ...]]", file=sys.stderr)
    return 2


def _run_memory(args: argparse.Namespace, config: Any) -> int:
    store = MemoryStore(
        str(config.memory.get("dir") or default_memory_dir()),
        stale_days=int(config.memory.get("stale_days", 90)),
        expire_days=int(config.memory.get("expire_days", 0)),
    )
    words = args.task[1:]
    action = words[0] if words else "list"
    if action == "list":
        lines = store.index_lines()
        print("\n".join(lines) if lines else f"no memories in {store.directory}")
        return 0
    if action == "show" and len(words) > 1:
        memory = store.read(words[1].lower())
        if memory is None:
            print(f"xharness: no such memory: {words[1]}", file=sys.stderr)
            return 1
        print(memory.to_text())
        return 0
    if action == "search" and len(words) > 1:
        hits = store.search(" ".join(words[1:]))
        for memory, score, snippet in hits:
            print(f"{memory.name} ({memory.kind}, score {score}): {memory.description}\n    {snippet}")
        if not hits:
            print("no matching memories")
        return 0
    if action == "topics":
        topics = store.topics()
        if not topics:
            print("no memories")
            return 0
        for topic, memories in topics.items():
            print(f"{topic} ({len(memories)})")
            for memory in memories:
                print(f"    {memory.name}: {memory.description}")
        return 0
    if action == "consolidate":
        targets = words[1:2] or ["all"]
        topics = list(store.topics()) if targets[0] == "all" else [targets[0]]
        if not topics:
            print("no memories to consolidate")
            return 0
        llm = None
        if args.use_llm:
            from .llm import OpenAIAdapter

            llm = OpenAIAdapter(**config.provider)
        changed = 0
        for topic in topics:
            plan = build_plan(store, topic, llm=llm)
            print(plan.describe())
            if plan.empty:
                continue
            if args.apply:
                for line in apply_plan(store, plan, actor={"agent": "operator", "session": None}):
                    print(f"  applied: {line}")
                    changed += 1
            else:
                print("  (dry run; add --apply to execute)")
        return 0
    if action == "stale":
        stale = store.stale()
        if not stale:
            print(f"no stale memories (threshold {store.stale_days} days)")
            return 0
        for memory, age in stale:
            print(f"{memory.name:32} {age:5} days  [{memory.topic}] {memory.description}")
        return 0
    if action == "verify":
        target = words[1] if len(words) > 1 else "all"
        if target != "all" and store.read(target) is not None and not args.use_llm:
            memory = store.verify(target, actor={"agent": "operator", "session": None})
            print(f"verified {target} at {memory.verified}" if memory else f"no such memory: {target}")
            return 0 if memory else 1
        llm = None
        if args.use_llm:
            from .llm import OpenAIAdapter

            llm = OpenAIAdapter(**config.provider)
        topics = list(store.topics()) if target == "all" else [target]
        total: list[Any] = []
        for topic in topics:
            verdicts = verify_plan(store, topic, llm=llm)
            for verdict in verdicts:
                print(f"{verdict.name:32} {verdict.age_days:5} days  {verdict.verdict:12} {verdict.reason}")
            total.extend(verdicts)
        if not total:
            print("nothing stale to verify")
            return 0
        if args.apply:
            for line in apply_verdicts(store, total, actor={"agent": "operator", "session": None}):
                print(f"  applied: {line}")
        else:
            print("  (dry run; add --apply to mark 'verify' verdicts as confirmed; contradictions are never auto-deleted)")
        return 0
    if action == "expire":
        expired = store.expired()
        if store.expire_days <= 0:
            print("expire_days is 0 (off); set [memory] expire_days in xharness.toml to archive very old memories")
            return 0
        if not expired:
            print(f"nothing older than {store.expire_days} days")
            return 0
        for memory, age in expired:
            print(f"{memory.name:32} {age:5} days  [{memory.topic}] {memory.description}")
            if args.apply and store.archive(memory.name, actor={"agent": "operator", "session": None}):
                print("  archived (memory/archive/, not deleted)")
        if not args.apply:
            print("  (dry run; add --apply to move these to memory/archive/)")
        return 0
    if action == "audit":
        records = store.audit()
        for record in records:
            print(f"{record.get('ts')}  {record.get('action'):7}  {record.get('name'):32}  agent={record.get('agent')}  session={record.get('session')}")
        if not records:
            print("no audit records")
        return 0
    print("usage: xharness memory [list | topics | show <name> | search <words> | audit | consolidate <topic|all> | stale | verify <name|topic|all> | expire] [--apply] [--llm]", file=sys.stderr)
    return 2


def _run_eval(args: argparse.Namespace, config: Any) -> int:
    paths = args.task[1:]
    if not paths:
        print("xharness: eval needs a case file or directory, e.g. xharness eval evals/basic", file=sys.stderr)
        return 2
    try:
        cases = [case for path in paths for case in load_cases(path)]
    except (OSError, ValueError) as error:
        print(f"xharness: {error}", file=sys.stderr)
        return 2
    model = str(config.provider["model"])
    print(f"running {len(cases)} cases x{max(1, args.repeat)} against {model} ...", file=sys.stderr)

    def progress(case_id: str, index: int, attempt: Any) -> None:
        status = "PASS" if attempt.passed else ("ERROR" if attempt.error else "FAIL")
        print(f"  {case_id} [{index + 1}] {status} ({attempt.seconds:.1f}s)", file=sys.stderr)

    reports = run_suite(cases, config, repeat=args.repeat, on_attempt=progress)
    print(format_report(reports, model))
    if args.json_out:
        write_results(reports, model, args.json_out)
        print(f"results written to {args.json_out}", file=sys.stderr)
    return 0 if all(report.passed for report in reports) else 1


def _print_usage(harness: Any) -> None:
    telemetry = harness.ctx.optional("telemetry")
    if telemetry and telemetry.calls:
        print(telemetry.summary(), file=sys.stderr)


def _prompt_approval(summary: str) -> bool:
    try:
        answer = input(f"\nallow {summary} ? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower().startswith("y")


def _run_users(args: Any, config: Any) -> int:
    """Account administration. Passwords are typed, never passed as arguments:
    a command line ends up in shell history, `ps` output and CI logs."""
    store = UserStore(getattr(config, "auth", {}) or {})
    if not store.enabled:
        print(
            "xharness: accounts are off. Add an [auth] section to the config first, for example:\n"
            '  [auth]\n  backend = "local"\n  default_quota_tokens_per_day = 500000',
            file=sys.stderr,
        )
        return 1
    action = args.task[1] if len(args.task) > 1 else "list"
    name = args.task[2] if len(args.task) > 2 else ""

    if action == "list":
        accounts = store.accounts()
        if not accounts:
            print("no accounts yet; create one with: xharness users add <name>")
            return 0
        print(f"{'帳號':<20}{'角色':<8}{'每日上限':>12}  顯示名稱")
        for row in accounts:
            limit = f"{row['quota_tokens_per_day']:,}" if row["quota_tokens_per_day"] else "無限制"
            state = "（已停用）" if row["disabled"] else ""
            print(f"{row['name']:<20}{row['role']:<8}{limit:>12}  {row['display']}{state}")
        return 0

    if action == "audit":
        records = store.audit_tail(int(args.task[2]) if len(args.task) > 2 and args.task[2].isdigit() else 50)
        if not records:
            print("no access records yet")
            return 0
        for row in records:
            mark = "ok  " if row.get("ok") else "FAIL"
            print(f"{row.get('ts', '')[:19]}  {mark}  {row.get('action', ''):<16}{row.get('user', ''):<20}{row.get('detail', '')}")
        return 0

    if not name:
        print(f"usage: xharness users {action} <name>", file=sys.stderr)
        return 2

    try:
        if action == "add":
            if store.backend == "ldap":
                print("xharness: the LDAP backend holds the passwords; list the account in [auth.users] instead.", file=sys.stderr)
                return 1
            password = getpass.getpass(f"password for {name}: ")
            if password != getpass.getpass("repeat: "):
                print("xharness: passwords do not match", file=sys.stderr)
                return 1
            user = store.add(name, password, args.role, args.display or "")
            print(f"created {user.name} ({user.role})")
            return 0
        if action == "password":
            password = getpass.getpass(f"new password for {name}: ")
            if password != getpass.getpass("repeat: "):
                print("xharness: passwords do not match", file=sys.stderr)
                return 1
            ok = store.set_password(name, password)
            print("password changed" if ok else f"no such account: {name}", file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 1
        if action == "disable":
            ok = store.remove(name)
            # Disabling keeps the history attributable; deleting would orphan it.
            print(f"{name} disabled (history kept)" if ok else f"no such account: {name}")
            return 0 if ok else 1
    except ValueError as error:
        print(f"xharness: {error}", file=sys.stderr)
        return 1

    print("usage: xharness users [list | add <name> | password <name> | disable <name> | audit [n]]", file=sys.stderr)
    return 2


def _run_report(args: Any, config: Any) -> int:
    """The service usage report, in the terminal or as JSON for the document builder."""
    store = UserStore(getattr(config, "auth", {}) or {})
    nodes = poll_all(load_nodes(config.fleet)) if config.fleet.get("nodes") else []
    report = collect_report(config, since=args.since if args.since != "7d" else "30d", users=store, nodes=nodes)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"wrote {args.json_out}", file=sys.stderr)
        return 0
    print(format_usage_report(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.task and args.task[0] == "sessions":
        _list_sessions()
        return 0
    if args.task[:2] == ["providers", "presets"]:
        return _run_providers(args, None)  # the catalog needs no config
    if args.task and args.task[0] == "desktop":
        from .desktop import run_desktop  # the window reports a missing config itself

        return run_desktop(args.config_path, "auto" if args.yes else args.approve)

    try:
        config = load_config(args.config_path, provider_override=args.provider, model_override=args.model)
    except Exception as error:  # noqa: BLE001 - config errors are user-facing
        print(f"xharness: {error}", file=sys.stderr)
        return 1

    if args.task and args.task[0] == "eval":
        return _run_eval(args, config)
    if args.task and args.task[0] == "memory":
        return _run_memory(args, config)
    if args.task and args.task[0] == "providers":
        return _run_providers(args, config)
    if args.task and args.task[0] == "fleet":
        return _run_fleet(config)
    if args.task and args.task[0] == "usage":
        return _run_usage(args, config)
    if args.task and args.task[0] == "users":
        return _run_users(args, config)
    if args.task and args.task[0] in ("report", "usage-report"):
        return _run_report(args, config)
    if args.task and args.task[0] == "web":
        approval_mode = "auto" if args.yes else (args.approve or config.approval)
        return serve(
            config,
            host=args.host,
            port=args.port,
            token=_web_token(args.token),
            approval_mode=approval_mode,
            open_browser=not args.no_open,
        )

    task = " ".join(args.task).strip()
    interactive = not task
    harness = build_harness(config, resume=args.resume)
    session: SessionLog = harness.ctx.get("session")

    initial_messages = messages_from_events(SessionLog.load(args.resume)) if args.resume else []

    # Headless defaults to auto (nobody is watching a pipe); interactive defaults
    # to the config's approval policy. An explicit --approve always wins.
    if args.yes:
        approval_mode = "auto"
    elif args.approve:
        approval_mode = args.approve
    else:
        approval_mode = config.approval if interactive else "auto"

    options = AgentOptions(
        system_prompt=config.system_prompt,
        max_turns=args.max_turns or config.max_turns or AgentOptions.max_turns,
        approval_mode=approval_mode,
        project_instructions=config.project_instructions and not args.no_instructions,
        prompt=_prompt_approval if sys.stdin.isatty() else None,
        initial_messages=initial_messages,
        on_delta=lambda text: (sys.stdout.write(text), sys.stdout.flush()) and None,
        on_tool_start=lambda name, call_args: print(f"\n[tool] {name} {call_args[:200]}"),
        on_tool_end=lambda name, _output, is_error: print(f"[tool] {name} {'failed' if is_error else 'done'}"),
    )
    agent = Agent(harness.ctx, options)

    try:
        if not interactive:
            try:
                agent.run(task)
            except Exception as error:  # noqa: BLE001 - budget stops and LLM errors exit cleanly
                print(f"\nxharness: {error}", file=sys.stderr)
                _print_usage(harness)
                print(f"session: {session.id}", file=sys.stderr)
                return 1
            print()
            _print_usage(harness)
            print(f"session: {session.id}", file=sys.stderr)
            return 0

        sandbox = harness.ctx.optional("sandbox")
        print(f"xHarness {__version__} — session {session.id}")
        print(f"model: {config.provider['model']} @ {config.provider['base_url']}")
        print(f"sandbox: {sandbox.name if sandbox else 'off'}")
        print("commands: /tools /usage /memory /session /clear /exit")
        while True:
            try:
                line = input("\nxharness> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/usage":
                telemetry = harness.ctx.optional("telemetry")
                print(telemetry.summary() if telemetry else "telemetry not mounted")
                continue
            if line == "/session":
                print(session.id)
                continue
            if line == "/memory":
                store = harness.ctx.optional("memory")
                lines = store.index_lines() if store else []
                print("\n".join(lines) if lines else "no memories")
                continue
            if line == "/tools":
                registry: ToolRegistry = harness.ctx.get("tools")
                for tool in registry.list():
                    marker = " (mutating)" if tool.mutating else ""
                    print(f"{tool.name}{marker} — {tool.description}")
                continue
            if line == "/clear":
                agent.messages[:] = [
                    {
                        "role": "system",
                        "content": config.system_prompt or default_system_prompt(options.cwd or ".", str(config.provider["model"])),
                    }
                ]
                print("conversation cleared")
                continue
            try:
                agent.run(line)
                print()
            except Exception as error:  # noqa: BLE001 - keep the REPL alive
                print(f"\nerror: {error}", file=sys.stderr)
        return 0
    finally:
        harness.dispose()


if __name__ == "__main__":
    sys.exit(main())
