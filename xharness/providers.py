"""Provider catalog: presets for well-known OpenAI-compatible endpoints, and
a probe that asks each configured provider which models it serves.

A preset only fills in defaults (base_url, api_key_env); explicit keys in the
config always win, and `model` is always yours to choose. Local presets keep
xHarness on-prem; the hosted ones are opt-in and send prompts off-machine.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "llama-cpp": {"base_url": "http://127.0.0.1:8080/v1", "local": True, "notes": "llama-server --port 8080 (add --jinja for tool calls)"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "local": True, "notes": "Ollama's OpenAI-compatible endpoint"},
    "vllm": {"base_url": "http://127.0.0.1:8000/v1", "local": True, "notes": "vllm serve --enable-auto-tool-choice --tool-call-parser <parser>"},
    "lmstudio": {"base_url": "http://127.0.0.1:1234/v1", "local": True, "notes": "LM Studio local server"},
    "litellm": {"base_url": "http://127.0.0.1:4000/v1", "api_key_env": "LITELLM_API_KEY", "local": True, "notes": "LiteLLM proxy / gateway"},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "local": False},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "local": False},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY", "local": False},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY", "local": False},
    "together": {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY", "local": False},
}

PROVIDER_KEYS = {"base_url", "model", "api_key", "api_key_env", "temperature", "max_tokens", "extra_headers", "timeout_seconds"}


def apply_preset(provider: dict[str, Any]) -> dict[str, Any]:
    """Merge a named preset under the provider's explicit keys."""
    name = provider.get("preset")
    if not name:
        return {key: value for key, value in provider.items() if key in PROVIDER_KEYS}
    preset = PRESETS.get(str(name))
    if preset is None:
        raise KeyError(f"unknown provider preset: {name} (available: {', '.join(sorted(PRESETS))})")
    merged = {key: value for key, value in preset.items() if key in PROVIDER_KEYS}
    merged.update({key: value for key, value in provider.items() if key in PROVIDER_KEYS})
    return merged


@dataclass
class ProbeResult:
    name: str
    base_url: str
    ok: bool
    models: list[str]
    detail: str = ""


def probe_provider(name: str, provider: dict[str, Any], timeout: float = 5.0) -> ProbeResult:
    """GET /models and report what the endpoint advertises."""
    base_url = str(provider.get("base_url") or "").rstrip("/")
    if not base_url:
        return ProbeResult(name, "", False, [], "no base_url")
    if not base_url.startswith(("http://", "https://")):
        return ProbeResult(name, base_url, False, [], "base_url must be http(s)")
    headers = {"Accept": "application/json"}
    key = provider.get("api_key") or (os.environ.get(str(provider["api_key_env"])) if provider.get("api_key_env") else None)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base_url}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - http(s) only, validated below
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as error:
        return ProbeResult(name, base_url, False, [], f"HTTP {error.code}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        return ProbeResult(name, base_url, False, [], str(getattr(error, "reason", error))[:120])
    entries = data.get("data") or data.get("models") or []
    models = [str(entry.get("id") or entry.get("name") or "?") for entry in entries if isinstance(entry, dict)]
    return ProbeResult(name, base_url, True, models)
