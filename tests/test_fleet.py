import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from xharness.config import ResolvedConfig
from xharness.fleet import Node, format_table, forward, load_nodes, node_name, poll_all, poll_node
from xharness.llm import AssistantTurn
from xharness.web import WebApp, make_handler

from tests.test_web import Client, factory_with


def test_load_nodes_and_validation(monkeypatch):
    nodes = load_nodes({"nodes": {"farm": {"url": "http://10.0.0.5:3080/", "token_env": "FARM_TOKEN", "timeout_seconds": 1}}})
    assert nodes[0].url == "http://10.0.0.5:3080" and nodes[0].timeout == 1.0
    monkeypatch.setenv("FARM_TOKEN", "abc")
    assert nodes[0].token == "abc"
    assert load_nodes({}) == [] and load_nodes(None) == []
    with pytest.raises(ValueError, match="needs a url"):
        load_nodes({"nodes": {"bad": {}}})
    with pytest.raises(ValueError, match="http"):
        load_nodes({"nodes": {"bad": {"url": "ftp://x"}}})
    assert node_name({"name": "hub-1"}) == "hub-1" and node_name({})


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_poll_node_reports_snapshot_and_failures(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        seen["client"] = request.get_header("X-xharness-client")
        return FakeResponse(json.dumps({"node": "farm", "version": "0.7.0", "model": "m", "summary": {"conversations": 1}, "items": [{"id": "abc"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("T", "tok")
    report = poll_node(Node("farm", "http://h:1", token_env="T"))
    assert report["ok"] and report["items"] == [{"id": "abc"}] and report["node"] == "farm"
    assert seen["auth"] == "Bearer tok" and seen["client"] == "hub"

    def refuse(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    report = poll_node(Node("farm", "http://h:1"))
    assert not report["ok"] and "refused" in report["error"]

    def unauthorized(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "nope", hdrs=None, fp=io.BytesIO(b'{"error":"unauthorized"}'))

    monkeypatch.setattr(urllib.request, "urlopen", unauthorized)
    report = poll_node(Node("farm", "http://h:1"))
    assert not report["ok"] and report["error"] == "HTTP 401"
    assert "DOWN" in format_table([report])


def test_forward_allowlist():
    node = Node("farm", "http://h:1")
    assert forward(node, "abc", "delete", {})[0] == 404
    assert forward(node, "../x", "stop", {})[0] == 400


@pytest.fixture()
def two_servers(monkeypatch):
    """A node with a token, and a hub configured to watch it."""
    servers = []
    node_turns = [AssistantTurn(content="", tool_calls=[{"id": "m1", "name": "mutate", "arguments": "{}"}]), AssistantTurn(content="node done")]
    node_config = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "node-model"}, provider_name="n", fleet={"name": "farm"})
    node_app = WebApp(node_config, approval_mode="prompt", harness_factory=factory_with(node_turns))
    node_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(node_app, "node-secret"))
    node_server.daemon_threads = True
    threading.Thread(target=node_server.serve_forever, daemon=True).start()
    servers.append((node_server, node_app))
    node_port = node_server.server_address[1]

    monkeypatch.setenv("NODE_FARM_TOKEN", "node-secret")
    hub_config = ResolvedConfig(
        provider={"base_url": "http://localhost:1/v1", "model": "hub-model"},
        provider_name="h",
        fleet={"name": "hub", "nodes": {"farm": {"url": f"http://127.0.0.1:{node_port}", "token_env": "NODE_FARM_TOKEN"}}},
    )
    hub_app = WebApp(hub_config, approval_mode="auto", harness_factory=factory_with([AssistantTurn(content="hub done")]))
    hub_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub_app, None))
    hub_server.daemon_threads = True
    threading.Thread(target=hub_server.serve_forever, daemon=True).start()
    servers.append((hub_server, hub_app))

    yield Client(node_port, "node-secret"), Client(hub_server.server_address[1])
    for server, app in servers:
        server.shutdown()
        server.server_close()
        app.dispose()


def test_hub_aggregates_node_and_forwards_stop(two_servers):
    node_client, hub_client = two_servers
    # a conversation on the node, blocked on approval
    _, conv = node_client.request("POST", "/api/conversations", body={})
    node_client.request("POST", f"/api/conversations/{conv['id']}/messages", body={"text": "node task"})
    node_client.events(conv["id"], until_types=("approval_request",), ticket=node_client.request("POST", "/api/tickets", body={})[1]["ticket"])

    status, fleet = hub_client.request("GET", "/api/fleet")
    assert status == 200 and fleet["node"] == "hub" and fleet["summary"]["conversations"] == 0
    node = fleet["nodes"][0]
    assert node["ok"] and node["name"] == "farm" and node["node"] == "farm" and node["model"] == "node-model"
    assert node["summary"]["waiting"] == 1 and node["items"][0]["state"] == "waiting"

    # the node's own /api/fleet (asked as a hub) must not include a nodes key
    status, snapshot = node_client.request("GET", "/api/fleet")
    assert status == 200 and "nodes" not in snapshot

    # forward a stop through the hub; the browser never talks to the node
    status, payload = hub_client.request("POST", f"/api/nodes/farm/conversations/{conv['id']}/stop", body={})
    assert status == 200 and payload["ok"] is True
    status, payload = hub_client.request("POST", f"/api/nodes/nope/conversations/{conv['id']}/stop", body={})
    assert status == 404
    status, fleet = hub_client.request("GET", "/api/fleet")
    assert fleet["nodes"][0]["items"][0]["state"] == "idle"


def test_poll_all_runs_in_parallel(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return FakeResponse(json.dumps({"node": "x", "summary": {}, "items": []}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    reports = poll_all([Node("a", "http://a:1"), Node("b", "http://b:1")])
    assert [report["name"] for report in reports] == ["a", "b"] and all(report["ok"] for report in reports)


def test_web_token_sources(tmp_path, monkeypatch):
    from xharness.cli import _web_token

    monkeypatch.delenv("XHARNESS_WEB_TOKEN", raising=False)
    monkeypatch.delenv("XHARNESS_WEB_TOKEN_FILE", raising=False)
    assert _web_token("explicit") == "explicit"
    assert _web_token(None) is None
    monkeypatch.setenv("XHARNESS_WEB_TOKEN", "from-env")
    assert _web_token(None) == "from-env"
    monkeypatch.delenv("XHARNESS_WEB_TOKEN")
    secret = tmp_path / "tok"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("XHARNESS_WEB_TOKEN_FILE", str(secret))
    assert _web_token(None) == "from-file"
