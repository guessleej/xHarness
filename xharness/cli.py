"""The xharness CLI: headless one-shot, interactive REPL, session listing."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from datetime import datetime

from . import __version__
from .agent import Agent, AgentOptions, default_system_prompt
from .config import load_config
from .evals import format_report, load_cases, run_suite, write_results
from .fleet import format_table, load_nodes, poll_all
from .consolidate import apply_plan, build_plan
from .memory import MemoryStore, default_memory_dir
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
        help='the task; "sessions" lists saved sessions; "eval <path>" runs a suite; "web" starts the local UI; "memory [list|topics|show <name>|search <q>|audit|consolidate <topic|all> [--apply] [--llm]]" inspects and tidies memory; "providers [presets|probe]" lists and probes endpoints; "fleet" polls the configured nodes; empty starts a REPL',
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
    parser.add_argument("--repeat", type=int, default=1, help="eval: run each case N times")
    parser.add_argument("--json", dest="json_out", help="eval: write per-attempt results as JSONL")
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
    store = MemoryStore(str(config.memory.get("dir") or default_memory_dir()))
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
    if action == "audit":
        records = store.audit()
        for record in records:
            print(f"{record.get('ts')}  {record.get('action'):7}  {record.get('name'):32}  agent={record.get('agent')}  session={record.get('session')}")
        if not records:
            print("no audit records")
        return 0
    print("usage: xharness memory [list | topics | show <name> | search <words> | audit | consolidate <topic|all> [--apply] [--llm]]", file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.task and args.task[0] == "sessions":
        _list_sessions()
        return 0
    if args.task[:2] == ["providers", "presets"]:
        return _run_providers(args, None)  # the catalog needs no config

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
                        "content": config.system_prompt or default_system_prompt(options.cwd or "."),
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
