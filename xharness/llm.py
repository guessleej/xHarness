"""OpenAI-compatible chat-completions adapter: urllib + SSE, standard library only.

Works against llama.cpp (llama-server), vLLM, Ollama, LiteLLM, and gateways.
Messages are plain dicts: {"role", "content", "tool_calls"?, "tool_call_id"?}.
Tool calls are {"id", "name", "arguments"} with arguments as a raw JSON string.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Context, Plugin

#: Fields the adapter owns; extra_body may not touch them.
RESERVED_BODY_KEYS = frozenset({"model", "messages", "stream", "stream_options", "tools"})
DEFAULT_TIMEOUT_SECONDS = 600
PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")
#: Statuses that mean "try again shortly", not "this request is wrong".
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: Waits before the 2nd and 3rd attempt. A llama-server behind a load balancer
#: answers 502 for the seconds it takes a backend to come back; failing the
#: whole task on that is worse than waiting.
RETRY_DELAYS = (2.0, 6.0)
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class AssistantTurn:
    content: str
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    usage: Usage | None = None


def _retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (the delta form), capped."""
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS) if value else None
    except ValueError:
        return None  # the HTTP-date form; the default delay is close enough


def endpoint_host(base_url: Any) -> str:
    """Host only: a base_url may carry a path or query that is nobody's business in a report."""
    return urllib.parse.urlsplit(str(base_url or "")).hostname or ""


def hosting_of(base_url: Any) -> str:
    """self-hosted vs bought-in, decided by where the endpoint lives."""
    host = endpoint_host(base_url)
    if not host:
        return "unknown"
    if host in ("localhost", "::1") or host.startswith(PRIVATE_PREFIXES):
        return "self-hosted"
    return "external"


class OpenAIAdapter:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        context_tokens: int | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("provider base_url is required")
        scheme = urllib.parse.urlsplit(base_url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"provider base_url must be http or https, got: {scheme or '(none)'}")
        if not model:
            raise ValueError("provider model is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_headers = extra_headers or {}
        #: Endpoint-specific request fields. Reasoning models are the reason this
        #: exists: several of them put the answer in `reasoning_content` and leave
        #: `content` empty unless the request turns thinking off, and every family
        #: spells that differently (llama.cpp takes
        #: `chat_template_kwargs = { enable_thinking = false }`). Nothing here may
        #: overwrite the fields the adapter itself sets.
        self.extra_body = {
            key: value for key, value in (extra_body or {}).items() if key not in RESERVED_BODY_KEYS
        }
        self.timeout_seconds = timeout_seconds
        self.sleep: Callable[[float], None] = time.sleep  # replaced in tests
        #: How many tokens the endpoint accepts (llama-server -c). Unknown when
        #: None; compaction then only reacts to an overflow error.
        self.context_tokens = int(context_tokens) if context_tokens else None

    def _open(self, request: urllib.request.Request) -> Any:
        """Send the request, retrying transient failures.

        Only failures before the response starts are retried: once tokens have
        streamed to the user, a second attempt would repeat them.
        """
        for attempt in range(len(RETRY_DELAYS) + 1):
            last = attempt == len(RETRY_DELAYS)
            try:
                # Scheme is validated to http/https in __init__, so file:/ and
                # custom schemes cannot reach this call.
                return urllib.request.urlopen(request, timeout=self.timeout_seconds)  # nosec B310
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                if last or error.code not in RETRYABLE_STATUS:
                    raise RuntimeError(f"LLM request failed: {error.code} {error.reason} {detail}") from error
                delay = _retry_after(error.headers.get("Retry-After") if error.headers else None)
                reason = f"{error.code} {error.reason}"
            except urllib.error.URLError as error:
                # Refused or reset: the server is restarting. A DNS failure or a
                # timeout will not fix itself in seconds, so those fail now.
                if last or not isinstance(error.reason, ConnectionError):
                    raise RuntimeError(
                        f"LLM request failed: cannot reach {self.base_url} ({error.reason})"
                    ) from error
                delay = None
                reason = str(error.reason)
            wait = delay if delay is not None else RETRY_DELAYS[attempt]
            print(f"[xharness] model request failed ({reason}); retrying in {wait:g}s", file=sys.stderr)
            self.sleep(wait)
        raise AssertionError("unreachable")

    def _wire_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role == "assistant" and message.get("tool_calls"):
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or None,
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["name"], "arguments": call["arguments"]},
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            elif role == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "content": message.get("content", ""),
                        "tool_call_id": message.get("tool_call_id", ""),
                    }
                )
            else:
                wire.append({"role": role, "content": message.get("content", "")})
        return wire

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], None] | None = None,
    ) -> AssistantTurn:
        key = self.api_key or (os.environ.get(self.api_key_env) if self.api_key_env else None)
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        body: dict[str, Any] = {
            **self.extra_body,
            "model": self.model,
            "messages": self._wire_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
                for tool in tools
            ]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response = self._open(request)

        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        with response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # tolerate keep-alive noise
                if chunk.get("usage"):
                    usage = Usage(
                        prompt_tokens=chunk["usage"].get("prompt_tokens", 0),
                        completion_tokens=chunk["usage"].get("completion_tokens", 0),
                    )
                choices = chunk.get("choices") or []
                delta = choices[0].get("delta") if choices else None
                if not delta:
                    continue
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    if on_delta:
                        on_delta(text)
                for piece in delta.get("tool_calls") or []:
                    slot = calls.setdefault(piece["index"], {"id": "", "name": "", "arguments": ""})
                    if piece.get("id"):
                        slot["id"] = piece["id"]
                    function = piece.get("function") or {}
                    if function.get("name"):
                        slot["name"] += function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        tool_calls = [
            {
                "id": slot["id"] or f"call_{index}",
                "name": slot["name"],
                "arguments": slot["arguments"],
            }
            for index, slot in sorted(calls.items())
        ]
        return AssistantTurn(content="".join(content_parts), tool_calls=tool_calls, usage=usage)


def llm_plugin(adapter_config: dict[str, Any]) -> Plugin:
    """Mount an OpenAI-compatible adapter as the "llm" service."""

    def apply(ctx: Context, _config: Any) -> None:
        ctx.provide("llm", OpenAIAdapter(**adapter_config))

    return Plugin("llm-openai", apply)
