"""xHarness: a plugin-based AI agent harness by xCloudinfo."""

__version__ = "0.9.0"

from .agent import Agent, AgentOptions, default_system_prompt, load_project_instructions
from .config import ResolvedConfig, load_config
from .fleet import Node, format_table, load_nodes, poll_all, poll_node
from .evals import AttemptResult, CaseReport, CheckResult, format_report, load_cases, run_case, run_suite, write_results
from .consolidate import Plan, Verdict, apply_plan, apply_verdicts, build_plan, find_duplicates, verify_plan
from .context import Context, Harness, Plugin
from .llm import AssistantTurn, OpenAIAdapter, Usage, llm_plugin
from .mcp import McpClient, McpError, mcp_plugin
from .memory import Memory, MemoryStore, default_memory_dir, memory_plugin
from .presets import build_harness
from .providers import PRESETS, ProbeResult, apply_preset, probe_provider
from .sandbox import Sandbox, resolve_sandbox, sandbox_plugin
from .subagent import run_child, subagent_plugin
from .telemetry import BudgetExceeded, Telemetry, telemetry_plugin
from .session import SessionLog, harness_home, messages_from_events, session_plugin, sessions_dir
from .web import WebApp, serve
from .tools import Tool, ToolContext, ToolRegistry, ToolResult, register_tools, tools_plugin
from .tools.bash import bash_tool, bash_tool_plugin
from .tools.fs import edit_tool, fs_tools_plugin, read_tool, write_tool
from .tools.search import glob_to_regex, glob_tool, grep_tool, search_tools_plugin
from .tools.security import security_scan_tool, security_tool_plugin
from .tools.todo import todo_tool_plugin
from .tools.webfetch import webfetch_tool, webfetch_tool_plugin


__all__ = [
    "Agent",
    "AttemptResult",
    "CaseReport",
    "CheckResult",
    "AgentOptions",
    "AssistantTurn",
    "Context",
    "Harness",
    "McpClient",
    "Node",
    "Memory",
    "MemoryStore",
    "McpError",
    "OpenAIAdapter",
    "PRESETS",
    "Plan",
    "Verdict",
    "Plugin",
    "ProbeResult",
    "ResolvedConfig",
    "Sandbox",
    "SessionLog",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "Usage",
    "__version__",
    "BudgetExceeded",
    "Telemetry",
    "bash_tool",
    "bash_tool_plugin",
    "apply_plan",
    "apply_verdicts",
    "apply_preset",
    "build_plan",
    "build_harness",
    "default_system_prompt",
    "edit_tool",
    "find_duplicates",
    "format_report",
    "format_table",
    "load_nodes",
    "poll_all",
    "poll_node",
    "fs_tools_plugin",
    "glob_to_regex",
    "glob_tool",
    "grep_tool",
    "harness_home",
    "load_project_instructions",
    "llm_plugin",
    "load_cases",
    "load_config",
    "mcp_plugin",
    "default_memory_dir",
    "memory_plugin",
    "messages_from_events",
    "probe_provider",
    "read_tool",
    "register_tools",
    "run_case",
    "run_suite",
    "verify_plan",
    "run_child",
    "subagent_plugin",
    "resolve_sandbox",
    "sandbox_plugin",
    "search_tools_plugin",
    "security_scan_tool",
    "security_tool_plugin",
    "session_plugin",
    "telemetry_plugin",
    "sessions_dir",
    "todo_tool_plugin",
    "tools_plugin",
    "webfetch_tool",
    "webfetch_tool_plugin",
    "write_results",
    "write_tool",
    "WebApp",
    "serve",
]
