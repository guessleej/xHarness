"""Compose the default harness: registry, built-in tools, session, adapter, user plugins."""

from __future__ import annotations

import importlib.util
import os

from .config import ResolvedConfig
from .context import Harness, Plugin
from .llm import llm_plugin
from .mcp import mcp_plugin
from .sandbox import sandbox_plugin
from .session import session_plugin
from .telemetry import telemetry_plugin
from .tools import tools_plugin
from .tools.bash import bash_tool_plugin
from .tools.fs import fs_tools_plugin
from .tools.search import search_tools_plugin
from .tools.security import security_tool_plugin
from .tools.todo import todo_tool_plugin
from .tools.webfetch import webfetch_tool_plugin


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
) -> Harness:
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("sandbox", sandbox_plugin(config.sandbox))  # before tool-bash
    harness.use("tool-fs", fs_tools_plugin)
    harness.use("tool-search", search_tools_plugin)
    harness.use("tool-bash", bash_tool_plugin)
    harness.use("tool-todo", todo_tool_plugin)
    harness.use("tool-security", security_tool_plugin)
    harness.use("telemetry", telemetry_plugin(config.telemetry))
    if config.webfetch:
        harness.use("tool-webfetch", webfetch_tool_plugin)
    if config.mcp_servers:
        harness.use("mcp", mcp_plugin(config.mcp_servers))
    if not no_session:
        harness.use("session", session_plugin(resume))
    harness.use("llm", llm_plugin(config.provider))

    for entry in config.plugins:
        if entry.get("disabled"):
            continue
        plugin = _load_user_plugin(str(entry["module"]))
        harness.use(str(entry["id"]), plugin, config=entry.get("config"))
    return harness
