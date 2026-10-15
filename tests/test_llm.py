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
    adapter.sleep = lambda seconds: None
    with pytest.raises(RuntimeError, match="429.*quota exceeded"):
        adapter.stream([{"role": "user", "content": "x"}], [])


def flaky(monkeypatch, failures):
    """urlopen that raises each of `failures` in turn, then streams "ok"."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1](request)
        return sse([json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "[DONE]"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = OpenAIAdapter(base_url="http://localhost:9/v1", model="m1")
    waits = []
    adapter.sleep = waits.append
    return adapter, calls, waits


def http_error(code, headers=None):
    import email.message

    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return lambda request: urllib.error.HTTPError(request.full_url, code, "x", hdrs=message, fp=io.BytesIO(b""))


def test_transient_failures_are_retried_with_backoff(monkeypatch):
    refused = lambda request: urllib.error.URLError(ConnectionRefusedError(61, "refused"))
    adapter, calls, waits = flaky(monkeypatch, [http_error(502), refused])
    assert adapter.stream([{"role": "user", "content": "x"}], []).content == "ok"
    assert len(calls) == 3
    assert waits == [2.0, 6.0]


def test_retry_after_is_honoured_and_capped(monkeypatch):
    adapter, _calls, waits = flaky(monkeypatch, [http_error(429, {"Retry-After": "3"}), http_error(503, {"Retry-After": "999"})])
    adapter.stream([{"role": "user", "content": "x"}], [])
    assert waits == [3.0, 30.0]


def test_a_bad_request_is_not_retried(monkeypatch):
    adapter, calls, waits = flaky(monkeypatch, [http_error(400)])
    with pytest.raises(RuntimeError, match="400"):
        adapter.stream([{"role": "user", "content": "x"}], [])
    assert len(calls) == 1 and waits == []


def test_unknown_host_is_not_retried(monkeypatch):
    dns = lambda request: urllib.error.URLError("nodename nor servname provided")
    adapter, calls, _waits = flaky(monkeypatch, [dns])
    with pytest.raises(RuntimeError, match="cannot reach"):
        adapter.stream([{"role": "user", "content": "x"}], [])
    assert len(calls) == 1


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


def test_non_http_base_url_rejected():
    with pytest.raises(ValueError, match="http or https"):
        OpenAIAdapter(base_url="file:///etc/passwd", model="m")


def test_extra_body_reaches_the_request():
    """Reasoning models need a per-endpoint flag to put the answer in `content`."""
    from xharness.llm import OpenAIAdapter

    adapter = OpenAIAdapter(
        base_url="http://127.0.0.1:1/v1", model="m",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    assert adapter.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_extra_body_cannot_overwrite_what_the_adapter_owns():
    from xharness.llm import OpenAIAdapter

    adapter = OpenAIAdapter(
        base_url="http://127.0.0.1:1/v1", model="real-model",
        extra_body={"model": "hijacked", "messages": [], "tools": [], "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False}},
    )
    assert adapter.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert adapter.model == "real-model"


def test_no_extra_body_by_default():
    from xharness.llm import OpenAIAdapter

    assert OpenAIAdapter(base_url="http://127.0.0.1:1/v1", model="m").extra_body == {}
