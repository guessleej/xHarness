"""xHarness: a plugin-based AI agent harness by xCloudinfo."""

from .agent import Agent, AgentOptions, default_system_prompt, load_project_instructions
from .config import ResolvedConfig, load_config
from .context import Context, Harness, Plugin
from .llm import AssistantTurn, OpenAIAdapter, Usage, llm_plugin
from .mcp import McpClient, McpError, mcp_plugin
from .presets import build_harness
from .sandbox import Sandbox, resolve_sandbox, sandbox_plugin
from .telemetry import BudgetExceeded, Telemetry, telemetry_plugin
from .session import SessionLog, harness_home, messages_from_events, session_plugin, sessions_dir
from .tools import Tool, ToolContext, ToolRegistry, ToolResult, register_tools, tools_plugin
from .tools.bash import bash_tool, bash_tool_plugin
from .tools.fs import edit_tool, fs_tools_plugin, read_tool, write_tool
from .tools.search import glob_to_regex, glob_tool, grep_tool, search_tools_plugin
from .tools.security import security_scan_tool, security_tool_plugin
from .tools.todo import todo_tool_plugin
from .tools.webfetch import webfetch_tool, webfetch_tool_plugin

__version__ = "0.2.0"

__all__ = [
    "Agent",
    "AgentOptions",
    "AssistantTurn",
    "Context",
    "Harness",
    "McpClient",
    "McpError",
    "OpenAIAdapter",
    "Plugin",
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
    "build_harness",
    "default_system_prompt",
    "edit_tool",
    "fs_tools_plugin",
    "glob_to_regex",
    "glob_tool",
    "grep_tool",
    "harness_home",
    "load_project_instructions",
    "llm_plugin",
    "load_config",
    "mcp_plugin",
    "messages_from_events",
    "read_tool",
    "register_tools",
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
    "write_tool",
]
