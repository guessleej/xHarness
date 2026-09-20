"""Configuration: xharness.toml discovery, provider resolution, env fallback."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .providers import apply_preset
from .session import harness_home


@dataclass
class ResolvedConfig:
    provider: dict[str, Any]
    provider_name: str
    system_prompt: str | None = None
    max_turns: int | None = None
    approval: str = "prompt"
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_instructions: bool = True
    telemetry: dict[str, Any] = field(default_factory=dict)
    subagent: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    fleet: dict[str, Any] = field(default_factory=dict)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    sandbox: dict[str, Any] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    webfetch: bool = False
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser: str | None = None
    browser_approval: bool = True


def _find_config_file(explicit_path: str | None) -> str | None:
    if explicit_path:
        if not os.path.exists(explicit_path):
            raise FileNotFoundError(f"config file not found: {explicit_path}")
        return explicit_path
    for candidate in ("xharness.toml", os.path.join(harness_home(), "config.toml")):
        if os.path.exists(candidate):
            return candidate
    return None


def _browser_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value)
    if not url.startswith(("http://", "https://")):
        raise ValueError("[tools] browser must be an http(s) URL of the camofox service, e.g. http://127.0.0.1:9377")
    return url


def load_config(
    explicit_path: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> ResolvedConfig:
    path = _find_config_file(explicit_path)
    raw: dict[str, Any] = {}
    if path:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)

    providers: dict[str, dict[str, Any]] = raw.get("providers", {})
    provider_name = provider_override or raw.get("default_provider") or next(iter(providers), "env")
    provider = providers.get(provider_name)
    if provider_override and provider is None:
        raise KeyError(f"provider not found in config: {provider_override}")
    if provider is None:
        base_url = os.environ.get("XHARNESS_BASE_URL")
        model = os.environ.get("XHARNESS_MODEL")
        if base_url and model:
            provider = {"base_url": base_url, "model": model, "api_key_env": "XHARNESS_API_KEY"}
    if provider is None:
        raise RuntimeError(
            "no provider configured: create xharness.toml (see xharness.example.toml) "
            "or set XHARNESS_BASE_URL and XHARNESS_MODEL"
        )
    provider = apply_preset(provider)
    if model_override:
        provider = {**provider, "model": model_override}
    if not provider.get("model"):
        raise ValueError(f"provider {provider_name!r} has no model; set model = \"...\" (presets never choose a model for you)")
    approval = raw.get("approval", "prompt")
    if approval not in ("prompt", "auto"):
        raise ValueError("approval must be prompt or auto")
    return ResolvedConfig(
        provider=provider,
        provider_name=provider_name,
        system_prompt=raw.get("system_prompt"),
        max_turns=raw.get("max_turns"),
        approval=approval,
        providers={name: apply_preset(entry) for name, entry in providers.items()},
        project_instructions=bool(raw.get("project_instructions", True)),
        telemetry=raw.get("telemetry", {}) or {},
        subagent=raw.get("subagent", {}) or {},
        memory=raw.get("memory", {}) or {},
        fleet=raw.get("fleet", {}) or {},
        plugins=raw.get("plugins", []),
        sandbox=raw.get("sandbox", {}),
        mcp_servers=raw.get("mcp", {}).get("servers", {}),
        webfetch=bool(raw.get("tools", {}).get("webfetch", False)),
        channels=raw.get("channels", {}) or {},
        browser=_browser_url(raw.get("tools", {}).get("browser")),
        browser_approval=bool(raw.get("tools", {}).get("browser_approval", True)),
    )
