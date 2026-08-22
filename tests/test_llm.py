import io
import json
import urllib.error
import urllib.request

import pytest

from xharness.llm import OpenAIAdapter


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def sse(lines):
    return FakeResponse("".join(f"data: {line}\n\n" for line in lines).encode())


def test_assembles_content_tool_calls_and_usage(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return sse(
            [
                '{"choices":[{"delta":{"content":"Hel"}}]}',
                '{"choices":[{"delta":{"content":"lo"}}]}',
                '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"bash","arguments":"{\\"comm"}}]}}]}',
                '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"and\\": \\"ls\\"}"}}]}}]}',
                '{"usage":{"prompt_tokens":11,"completion_tokens":7},"choices":[]}',
                "[DONE]",
            ]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = OpenAIAdapter(base_url="http://localhost:9/v1/", model="m1")
    deltas = []
    turn = adapter.stream(
        [{"role": "user", "content": "hi"}],
        [{"name": "bash", "description": "run", "parameters": {"type": "object"}}],
        on_delta=deltas.append,
    )
    assert turn.content == "Hello"
    assert deltas == ["Hel", "lo"]
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["name"] == "bash"
    assert json.loads(turn.tool_calls[0]["arguments"]) == {"command": "ls"}
    assert turn.usage.prompt_tokens == 11 and turn.usage.completion_tokens == 7
    assert captured["url"] == "http://localhost:9/v1/chat/completions"
    assert len(captured["body"]["tools"]) == 1


def test_http_error_raises_with_status_and_detail(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many", hdrs=None, fp=io.BytesIO(b"quota exceeded")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = OpenAIAdapter(base_url="http://localhost:9/v1", model="m1")
    with pytest.raises(RuntimeError, match="429.*quota exceeded"):
        adapter.stream([{"role": "user", "content": "x"}], [])


def test_tool_messages_serialize_to_wire_format(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return sse(['{"choices":[{"delta":{"content":"ok"}}]}', "[DONE]"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = OpenAIAdapter(base_url="http://localhost:9/v1", model="m1")
    adapter.stream(
        [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "bash", "arguments": "{}"}]},
            {"role": "tool", "content": "result", "tool_call_id": "c1"},
        ],
        [],
    )
    messages = captured["body"]["messages"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert messages[2]["tool_call_id"] == "c1"


def test_missing_config_rejected():
    with pytest.raises(ValueError):
        OpenAIAdapter(base_url="", model="m")
    with pytest.raises(ValueError):
        OpenAIAdapter(base_url="http://x", model="")
