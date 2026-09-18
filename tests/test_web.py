import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolResult, tools_plugin
from xharness.web import CSRF_HEADER, WebApp, make_handler

CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")


class ScriptedAdapter:
    def __init__(self, turns):
        self.model = "fake"
        self.turns = list(turns)

    def stream(self, messages, tools, on_delta=None):
        turn = self.turns.pop(0) if self.turns else AssistantTurn(content="(no more turns)")
        if on_delta and turn.content:
            on_delta(turn.content)
        return turn


def factory_with(turns):
    def factory(resume):
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.ctx.get("tools").register(
            Tool(
                name="mutate",
                description="needs approval",
                parameters={"type": "object", "properties": {}},
                execute=lambda args, ctx: ToolResult("mutated") if ctx.approve("mutate now") else ToolResult("denied", is_error=True),
                mutating=True,
            )
        )
        harness.use("telemetry", telemetry_plugin())
        harness.ctx.provide("llm", ScriptedAdapter(turns))
        return harness

    return factory


class Client:
    def __init__(self, port, token=None):
        self.port = port
        self.token = token

    def request(self, method, path, body=None, host=None, csrf=True, bearer=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if csrf:
            headers[CSRF_HEADER] = "1"
        if self.token and bearer:
            headers["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = data.decode("utf-8", errors="replace")
        return response.status, payload

    def events(self, conv_id, until_types=("done", "error"), ticket=None, limit=40):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        path = f"/api/conversations/{conv_id}/events?after=0"
        if ticket:
            path += f"&ticket={ticket}"
        headers = {"Host": f"127.0.0.1:{self.port}"}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        assert response.status == 200, response.read()
        seen = []
        while len(seen) < limit:
            line = response.readline().decode("utf-8")
            if not line:
                break
            if line.startswith("data: "):
                event = json.loads(line[6:])
                seen.append(event)
                if event["type"] in until_types:
                    break
        conn.close()
        return seen


@pytest.fixture()
def server_factory():
    servers = []

    def start(turns, approval_mode="auto", token=None):
        app = WebApp(CONFIG, approval_mode=approval_mode, harness_factory=factory_with(turns))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, token))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, app))
        return Client(server.server_address[1], token), app

    yield start
    for server, app in servers:
        server.shutdown()
        server.server_close()
        app.dispose()


def test_index_and_meta(server_factory):
    client, _app = server_factory([])
    status, page = client.request("GET", "/")
    assert status == 200 and "xHarness" in page and "compositionstart" in page
    status, meta = client.request("GET", "/api/meta")
    assert status == 200 and meta["model"] == "fake" and meta["auth"] is False


def test_csrf_and_host_guards(server_factory):
    client, _app = server_factory([])
    status, payload = client.request("POST", "/api/conversations", body={}, csrf=False)
    assert status == 403 and CSRF_HEADER in payload["error"]
    status, _ = client.request("GET", "/api/meta", host="evil.example:80")
    assert status == 403
    status, _ = client.request("GET", "/", host="evil.example")
    assert status == 403


def test_conversation_streams_to_done(server_factory):
    client, _app = server_factory([AssistantTurn(content="hello from the model", usage=Usage(12, 4))])
    status, conv = client.request("POST", "/api/conversations", body={})
    assert status == 201
    status, _ = client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "hi"})
    assert status == 202
    events = client.events(conv["id"])
    types = [event["type"] for event in events]
    assert types[0] == "user" and "delta" in types and types[-1] == "done"
    done = events[-1]
    assert done["answer"] == "hello from the model" and done["usage"]["total_tokens"] == 16
    status, listing = client.request("GET", "/api/conversations")
    assert status == 200 and listing[0]["preview"] == "hi" and listing[0]["running"] is False


def test_approval_round_trip(server_factory):
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "m1", "name": "mutate", "arguments": "{}"}]),
        AssistantTurn(content="finished"),
    ]
    client, _app = server_factory(turns, approval_mode="prompt")
    _, conv = client.request("POST", "/api/conversations", body={})
    client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "mutate"})
    events = client.events(conv["id"], until_types=("approval_request",))
    request = events[-1]
    assert request["type"] == "approval_request" and "mutate now" in request["summary"]
    status, payload = client.request(
        "POST", f"/api/conversations/{conv['id']}/approvals", body={"id": request["id"], "allow": True}
    )
    assert status == 200 and payload["ok"] is True
    events = client.events(conv["id"])
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["ok"] is True and "mutated" in tool_end["output"]
    assert events[-1]["type"] == "done" and events[-1]["answer"] == "finished"


def test_busy_conversation_rejects_second_message(server_factory):
    turns = [AssistantTurn(content="", tool_calls=[{"id": "m1", "name": "mutate", "arguments": "{}"}]), AssistantTurn(content="ok")]
    client, _app = server_factory(turns, approval_mode="prompt")
    _, conv = client.request("POST", "/api/conversations", body={})
    client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "one"})
    client.events(conv["id"], until_types=("approval_request",))
    status, payload = client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "two"})
    assert status == 409 and "busy" in payload["error"]


def test_token_mode_requires_bearer_and_tickets_for_sse(server_factory):
    client, _app = server_factory([AssistantTurn(content="secret ok")], token="s3cret")
    status, _ = client.request("GET", "/api/meta", bearer=False)
    assert status == 401
    status, meta = client.request("GET", "/api/meta", host="10.0.0.5:3080")
    assert status == 200 and meta["auth"] is True
    _, conv = client.request("POST", "/api/conversations", body={})
    client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "go"})
    status, ticket = client.request("POST", "/api/tickets", body={})
    assert status == 200 and ticket["ttl"] == 60
    events = client.events(conv["id"], ticket=ticket["ticket"])
    assert events[-1]["type"] == "done"
    with pytest.raises(AssertionError):
        client.events(conv["id"], ticket=ticket["ticket"])  # single use


def test_serve_refuses_non_loopback_without_token(capsys):
    from xharness.web import serve

    assert serve(CONFIG, host="0.0.0.0", port=0, open_browser=False) == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_memory_endpoint_lists_memories(server_factory, tmp_path, monkeypatch):
    from xharness.memory import MemoryStore

    monkeypatch.setitem(CONFIG.memory, "dir", str(tmp_path))
    MemoryStore(str(tmp_path)).write("fav-editor", "User prefers vim", "vim")
    client, _app = server_factory([])
    status, payload = client.request("GET", "/api/memory")
    assert status == 200 and payload[0]["name"] == "fav-editor" and payload[0]["description"] == "User prefers vim"


def test_fleet_view_and_stop(server_factory):
    turns = [AssistantTurn(content="", tool_calls=[{"id": "m1", "name": "mutate", "arguments": "{}"}]), AssistantTurn(content="ok")]
    client, _app = server_factory(turns, approval_mode="prompt")
    _, conv = client.request("POST", "/api/conversations", body={})
    client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "fleet me"})
    client.events(conv["id"], until_types=("approval_request",))
    status, fleet = client.request("GET", "/api/fleet")
    assert status == 200 and fleet["summary"]["waiting"] == 1 and fleet["summary"]["conversations"] == 1
    item = fleet["items"][0]
    assert item["state"] == "waiting" and item["pending_approvals"] and item["model"] == "fake"
    status, payload = client.request("POST", f"/api/conversations/{conv['id']}/stop", body={})
    assert status == 200 and payload["ok"] is True
    events = client.events(conv["id"])
    types = [e["type"] for e in events]
    assert "stop_requested" in types and events[-1]["type"] == "done"
    assert events[-1]["answer"] == "[stopped by operator]"
    status, fleet = client.request("GET", "/api/fleet")
    assert fleet["items"][0]["state"] == "idle"
    status, payload = client.request("POST", f"/api/conversations/{conv['id']}/stop", body={})
    assert status == 409
