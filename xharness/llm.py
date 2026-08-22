"""OpenAI-compatible chat-completions adapter: urllib + SSE, standard library only.

Works against llama.cpp (llama-server), vLLM, Ollama, LiteLLM, and gateways.
Messages are plain dicts: {"role", "content", "tool_calls"?, "tool_call_id"?}.
Tool calls are {"id", "name", "arguments"} with arguments as a raw JSON string.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Context, Plugin

DEFAULT_TIMEOUT_SECONDS = 600


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class AssistantTurn:
    content: str
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    usage: Usage | None = None


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
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url:
            raise ValueError("provider base_url is required")
        if not model:
            raise ValueError("provider model is required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_headers = extra_headers or {}
        self.timeout_seconds = timeout_seconds

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
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)  # noqa: S310
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM request failed: {error.code} {error.reason} {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"LLM request failed: cannot reach {self.base_url} ({error.reason})") from error

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
