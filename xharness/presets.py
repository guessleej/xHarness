"""Compose the default harness: registry, built-in tools, session, adapter, user plugins."""

from __future__ import annotations

import importlib.util
import os
from typing import Any
from urllib.parse import urlsplit

from .config import ResolvedConfig
from .context import Harness, Plugin
from .llm import llm_plugin
from .mcp import mcp_plugin
from .memory import memory_plugin
from .quota import quota_plugin
from .sandbox import sandbox_plugin
from .session import session_plugin
from .subagent import subagent_plugin
from .telemetry import telemetry_plugin
from .tools import tools_plugin
from .tools.bash import bash_tool_plugin
from .tools.fs import fs_tools_plugin
from .tools.search import search_tools_plugin
from .tools.security import security_tool_plugin
from .tools.todo import todo_tool_plugin
from .tools.webfetch import webfetch_tool_plugin
from .tools.browser import browser_tools_plugin


PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")


def _endpoint_host(base_url: Any) -> str:
    """Host only: a base_url may carry a path or query that is nobody's business in a report."""
    return urlsplit(str(base_url or "")).hostname or ""


def _hosting(base_url: Any) -> str:
    """self-hosted vs bought-in, decided by where the endpoint lives."""
    host = _endpoint_host(base_url)
    if not host:
        return "unknown"
    if host in ("localhost", "::1") or host.startswith(PRIVATE_PREFIXES):
        return "self-hosted"
    return "external"


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
    harness.use("tool-todo", todo_tool_plugin)
    harness.use("tool-security", security_tool_plugin)
    harness.use("telemetry", telemetry_plugin(config.telemetry))
    harness.use("subagent", subagent_plugin(config.subagent))
    if config.memory.get("enabled", True):
        harness.use("memory", memory_plugin(config.memory))
    if config.webfetch:
        harness.use("tool-webfetch", webfetch_tool_plugin)
    if config.browser:
        harness.use("tool-browser", browser_tools_plugin(config.browser, config.browser_approval))
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
    harness.use("llm", llm_plugin(config.provider))

    for entry in config.plugins:
        if entry.get("disabled"):
            continue
        plugin = _load_user_plugin(str(entry["module"]))
        harness.use(str(entry["id"]), plugin, config=entry.get("config"))
    return harness
