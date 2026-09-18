"""The xharness CLI: headless one-shot, interactive REPL, session listing."""

from __future__ import annotations

import argparse
import sys
from typing import Any
from datetime import datetime

from . import __version__
from .agent import Agent, AgentOptions, default_system_prompt
from .config import load_config
from .evals import format_report, load_cases, run_suite, write_results
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
        help='the task; "sessions" lists saved sessions; "eval <path>" runs a suite; empty starts a REPL',
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

    try:
        config = load_config(args.config_path, provider_override=args.provider, model_override=args.model)
    except Exception as error:  # noqa: BLE001 - config errors are user-facing
        print(f"xharness: {error}", file=sys.stderr)
        return 1

    if args.task and args.task[0] == "eval":
        return _run_eval(args, config)

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
        print("commands: /tools /usage /session /clear /exit")
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
