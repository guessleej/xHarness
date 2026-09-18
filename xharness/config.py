"""Configuration: xharness.toml discovery, provider resolution, env fallback."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .session import harness_home


@dataclass
class ResolvedConfig:
    provider: dict[str, Any]
    provider_name: str
    system_prompt: str | None = None
    max_turns: int | None = None
    approval: str = "prompt"
    project_instructions: bool = True
    telemetry: dict[str, Any] = field(default_factory=dict)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    sandbox: dict[str, Any] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    webfetch: bool = False


def _find_config_file(explicit_path: str | None) -> str | None:
    if explicit_path:
        if not os.path.exists(explicit_path):
            raise FileNotFoundError(f"config file not found: {explicit_path}")
        return explicit_path
    for candidate in ("xharness.toml", os.path.join(harness_home(), "config.toml")):
        if os.path.exists(candidate):
            return candidate
    return None


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
    if model_override:
        provider = {**provider, "model": model_override}
    approval = raw.get("approval", "prompt")
    if approval not in ("prompt", "auto"):
        raise ValueError("approval must be prompt or auto")
    return ResolvedConfig(
        provider=provider,
        provider_name=provider_name,
        system_prompt=raw.get("system_prompt"),
        max_turns=raw.get("max_turns"),
        approval=approval,
        project_instructions=bool(raw.get("project_instructions", True)),
        telemetry=raw.get("telemetry", {}) or {},
        plugins=raw.get("plugins", []),
        sandbox=raw.get("sandbox", {}),
        mcp_servers=raw.get("mcp", {}).get("servers", {}),
        webfetch=bool(raw.get("tools", {}).get("webfetch", False)),
    )
