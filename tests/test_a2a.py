"""A2A: agent card, Bearer token, tasks, approvals as input-required, cancel."""
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from xharness.a2a import A2AService, _loopback, agent_card, load_token, make_handler
from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.tools import Tool, ToolResult, tools_plugin

TOKEN = "test-token-a2a"
RAN = []


def make_agent(prompt, turns, name="a2a", approval="prompt"):
    harness = Harness()
    harness.use("tools", tools_plugin)

    def write(args, ctx):
        if not ctx.approve("write: report.txt"):
            return ToolResult("denied by approval policy", is_error=True)
        RAN.append("write")
        return ToolResult("written")

    def send(args, ctx):
        return ToolResult("sent") if ctx.confirm and ctx.confirm("送出") else ToolResult("not sent", is_error=True)

    tools = harness.ctx.get("tools")
    tools.register(Tool("write_report", "Write", {"type": "object", "properties": {}}, write, mutating=True))
    tools.register(Tool("send_form", "Send", {"type": "object", "properties": {}}, send, mutating=True))

    class LLM:
        model = "fake"

        def __init__(self):
            self.turns = list(turns)

        def stream(self, messages, tools, on_delta=None):
            step = self.turns.pop(0)
            if callable(step):
                step = step()
            return step

    harness.ctx.provide("llm", LLM())
    return Agent(harness.ctx, AgentOptions(name=name, approval_mode=approval, prompt=prompt))


def call(name):
    return AssistantTurn(content="", tool_calls=[{"id": name, "name": name, "arguments": "{}"}])


@pytest.fixture()
def server():
    scripts = []

    def factory(prompt):
        return make_agent(prompt, scripts.pop(0))

    service = A2AService(factory, wait_seconds=10)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, TOKEN, agent_card("http://x/", "test")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    RAN.clear()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", scripts, service
    httpd.shutdown()


def rpc(base, method, params, token=TOKEN):
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(base + "/", data=body, headers=headers), timeout=20) as r:
        return json.loads(r.read())


def send(text, **extra):
    message = {"role": "user", "parts": [{"kind": "text", "text": text}], "messageId": "m1", **extra}
    return {"message": message}


def test_card_is_public_and_says_how_to_authenticate(server):
    base, _, _ = server
    card = json.loads(urllib.request.urlopen(base + "/.well-known/agent-card.json", timeout=5).read())
    assert card["protocolVersion"] == "0.3.0" and card["preferredTransport"] == "JSONRPC"
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer" and card["capabilities"]["streaming"] is False
    assert {s["id"] for s in card["skills"]} == {"research", "analysis", "files"}


def test_calls_without_the_token_are_refused(server):
    base, _, _ = server
    for token in (None, "wrong"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            rpc(base, "tasks/get", {"id": "x"}, token=token)
        assert caught.value.code == 401


def test_a_task_runs_to_completion_and_the_context_continues(server):
    base, scripts, _ = server
    scripts.append([AssistantTurn(content="第一題答案"), AssistantTurn(content="第二題答案")])
    first = rpc(base, "message/send", send("第一題"))["result"]
    assert first["status"]["state"] == "completed" and first["artifacts"][0]["parts"][0]["text"] == "第一題答案"
    second = rpc(base, "message/send", send("第二題", contextId=first["contextId"]))["result"]
    assert second["contextId"] == first["contextId"] and second["artifacts"][0]["parts"][0]["text"] == "第二題答案"
    got = rpc(base, "tasks/get", {"id": first["id"], "historyLength": 1})["result"]
    assert len(got["history"]) == 1 and got["history"][0]["role"] == "agent"
    assert rpc(base, "tasks/get", {"id": "nope"})["error"]["code"] == -32001


@pytest.mark.parametrize("answer,ran", [("允許", True), ("不要", False)])
def test_an_approval_becomes_input_required_and_the_answer_decides(server, answer, ran):
    base, scripts, _ = server
    scripts.append([call("write_report"), AssistantTurn(content="好了")])
    waiting = rpc(base, "message/send", send("寫報告"))["result"]
    assert waiting["status"]["state"] == "input-required"
    assert "write: report.txt" in waiting["status"]["message"]["parts"][0]["text"]
    done = rpc(base, "message/send", send(answer, taskId=waiting["id"], contextId=waiting["contextId"]))["result"]
    assert done["status"]["state"] == "completed" and (RAN == ["write"]) is ran
    again = rpc(base, "message/send", send("允許", taskId=waiting["id"]))
    assert again["error"]["code"] == -32602                       # nothing is waiting any more


def test_what_needs_a_person_is_refused_without_asking(server):
    base, scripts, service = server
    scripts.append([call("send_form"), AssistantTurn(content="沒送出")])
    result = rpc(base, "message/send", send("送出表單"))["result"]
    assert result["status"]["state"] == "completed"           # never stopped at input-required
    agent = service.contexts[result["contextId"]]["agent"]
    assert [m["content"] for m in agent.messages if m["role"] == "tool"] == ["not sent"]


def test_non_blocking_then_cancel(server):
    base, scripts, _ = server
    release = threading.Event()

    def slow():
        release.wait(5)
        return AssistantTurn(content="太慢了")

    scripts.append([slow])
    started = rpc(base, "message/send", {**send("慢慢做"), "configuration": {"blocking": False}})["result"]
    assert started["status"]["state"] in ("submitted", "working")
    canceled = rpc(base, "tasks/cancel", {"id": started["id"]})["result"]
    assert canceled["status"]["state"] == "canceled"
    release.set()
    time.sleep(0.2)
    assert rpc(base, "tasks/get", {"id": started["id"]})["result"]["status"]["state"] == "canceled"
    assert rpc(base, "tasks/cancel", {"id": started["id"]})["error"]["code"] == -32002


def test_streaming_unknown_methods_and_bad_input(server):
    base, _, _ = server
    assert rpc(base, "message/stream", send("x"))["error"]["code"] == -32004
    assert rpc(base, "nope", {})["error"]["code"] == -32601
    assert rpc(base, "message/send", {"message": {"parts": []}})["error"]["code"] == -32602


def test_token_file_is_private_and_reused(tmp_path):
    path = str(tmp_path / "home" / "a2a-token")
    token = load_token(path)
    assert len(token) >= 40 and load_token(path) == token
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert _loopback("127.0.0.1") and _loopback("::1") and _loopback("localhost") and not _loopback("0.0.0.0")
