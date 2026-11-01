"""Compose the default harness: registry, built-in tools, session, adapter, user plugins."""

from __future__ import annotations

import importlib.util
import os
from typing import Any

from .config import ResolvedConfig
from .context import Harness, Plugin
from .llm import endpoint_host as _endpoint_host, hosting_of as _hosting, llm_plugin
from .mcp import mcp_plugin
from .memory import memory_plugin
from .quota import quota_plugin
from .sandbox import sandbox_plugin
from .session import session_plugin
from .skills import skills_plugin
from .goal import goal_plugin
from .hooks import hooks_plugin
from .lsp import lsp_plugin
from .schedule import schedule_plugin
from .workflow import workflow_plugin
from .discuss import discussion_plugin
from .roles import roles_plugin
from .router import router_plugin
from .tools.jobs import jobs_plugin
from .tools.terminal import terminal_plugin
from .subagent import subagent_plugin
from .agentcomm import agentcomm_plugin
from .ralph import ralph_plugin
from .tools.powershell import powershell_plugin
from .tools.sessions import sessions_plugin
from .telemetry import telemetry_plugin
from .tools import tools_plugin
from .tools.bash import bash_tool_plugin
from .tools.fs import fs_tools_plugin
from .tools.search import search_tools_plugin
from .tools.security import security_tool_plugin
from .tools.todo import todo_tool_plugin
from .tools.webfetch import webfetch_tool_plugin
from .tools.browser import browser_tools_plugin
from .tools.desktop_control import desktop_control_plugin
from .board import board_plugin
from .vision import describer_from_config
from .tools.ask import ask_tool_plugin


def _load_user_plugin(module_path: str) -> Plugin:
    path = os.path.abspath(module_path)
    spec = importlib.util.spec_from_file_location(f"xharness_user_{os.path.basename(path)}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = getattr(module, "plugin", None)
    if not isinstance(plugin, Plugin):
        raise TypeError(f"module must expose a Plugin named `plugin`: {module_path}")
    return plugin


def build_harness(
    config: ResolvedConfig,
    resume: str | None = None,
    no_session: bool = False,
    user: Any | None = None,
) -> Harness:
    """`user` is an identity.User when [auth] is on: it gives this harness its own
    session directory and its own spending allowance."""
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("sandbox", sandbox_plugin(config.sandbox))  # before tool-bash
    harness.use("tool-fs", fs_tools_plugin)
    harness.use("tool-search", search_tools_plugin)
    harness.use("tool-bash", bash_tool_plugin)
    harness.use("tool-powershell", powershell_plugin())
    if getattr(config, "terminal", True):
        harness.use("terminal", terminal_plugin())
    harness.use("jobs", jobs_plugin(getattr(config, "jobs", None)))
    harness.use("goal", goal_plugin())
    if getattr(config, "hooks", None):
        harness.use("hooks", hooks_plugin(config.hooks))
    harness.use("tool-todo", todo_tool_plugin)
    harness.use("tool-security", security_tool_plugin)
    harness.use("tool-ask", ask_tool_plugin)
    harness.use("skills", skills_plugin(getattr(config, "skills", None)))
    harness.use("telemetry", telemetry_plugin(config.telemetry))
    harness.use("roles", roles_plugin(
        getattr(config, "agents", None), providers=config.providers,
        allowed=tuple(getattr(user, "allowed_providers", ()) or ()),
        describer=str((getattr(config, "vision", None) or {}).get("provider") or "")))
    harness.use("agentcomm", agentcomm_plugin())
    harness.use("subagent", subagent_plugin(config.subagent))
    harness.use("workflow", workflow_plugin(config.subagent))
    harness.use("ralph", ralph_plugin(config.subagent))
    harness.use("tool-sessions", sessions_plugin())
    if (getattr(config, "discussion", None) or {}).get("enabled", True):
        harness.use("discussion", discussion_plugin(config.discussion))
    harness.use("schedule", schedule_plugin())
    if (getattr(config, "board", None) or {}).get("enabled"):
        harness.use("board", board_plugin())
    if (getattr(config, "lsp", None) or {}).get("enabled", True):
        harness.use("lsp", lsp_plugin(config.lsp))
    if config.memory.get("enabled", True):
        harness.use("memory", memory_plugin(config.memory))
    if config.webfetch:
        harness.use("tool-webfetch", webfetch_tool_plugin)
    if config.browser:
        harness.use("tool-browser", browser_tools_plugin(config.browser, config.browser_approval))
    if getattr(config, "desktop_control", False):
        harness.use("tool-desktop", desktop_control_plugin(config.desktop_approval, describer_from_config(config)))
    if config.mcp_servers:
        harness.use("mcp", mcp_plugin(config.mcp_servers))
    if not no_session:
        harness.use(
            "session",
            session_plugin(
                resume,
                user.name if user else None,
                meta={
                    "model": config.provider.get("model"),
                    "endpoint": _endpoint_host(config.provider.get("base_url")),
                    "hosting": _hosting(config.provider.get("base_url")),
                    "provider": config.provider_name,
                },
            ),
        )
    if user is not None:  # after session and telemetry: the guard reads both
        harness.use(
            "quota",
            quota_plugin(user.name, user.quota_tokens_per_day, user.quota_tokens_per_month),
        )
    if (getattr(config, "routing", None) or {}).get("enabled"):
        # The router is the "llm" service: it picks among the configured models for every call.
        harness.use("llm", router_plugin(config, tuple(getattr(user, "allowed_providers", ()) or ())))
    else:
        harness.use("llm", llm_plugin(config.provider))

    for entry in config.plugins:
        if entry.get("disabled"):
            continue
        plugin = _load_user_plugin(str(entry["module"]))
        harness.use(str(entry["id"]), plugin, config=entry.get("config"))

    # Last, so it covers every registration: built-ins, MCP servers and user
    # plugins alike. A tool that is removed is never offered to the model, so a
    # restricted tier cannot ask for it and be refused -- it simply is not there.
    denied = tuple(getattr(user, "denied_tools", ()) or ())
    if denied:
        harness.ctx.get("tools").remove(denied)
    return harness
