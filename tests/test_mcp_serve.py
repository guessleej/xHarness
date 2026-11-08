"""xharness mcp-serve: the protocol, what is offered, and what still needs the person."""
import io
import json
import os
import sys

import xharness

from xharness.mcp import McpClient
from xharness.mcp_serve import McpServer
from xharness.tools import Tool, ToolResult


def _tools(log):
    def echo(args, ctx):
        print("stray print")                           # must not reach the protocol
        return ToolResult(f"echo {args.get('text')}")

    def careful(args, ctx):
        return ToolResult("sent") if ctx.confirm("送出表單") else ToolResult("not sent", is_error=True)

    def screen(args, ctx):
        return ToolResult("looked") if ctx.approve("desktop_look") else ToolResult("denied", is_error=True)

    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    return [Tool("echo", "Echo", schema, echo), Tool("careful", "Sends", schema, careful, mutating=True),
            Tool("screen", "Desktop", schema, screen, always_ask=True)]


def run(messages, capabilities=None):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": capabilities or {}}}
    lines = [init, {"jsonrpc": "2.0", "method": "notifications/initialized"}] + messages
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))
    stdout, log = io.StringIO(), io.StringIO()
    McpServer(_tools(log), ".", stdin, stdout, log).serve()
    return [json.loads(line) for line in stdout.getvalue().splitlines()], log.getvalue()


def call(msg_id, name, args=None):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def test_initialize_list_and_call(capsys):
    out, log = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, call(2, "echo", {"text": "云碩"}),
                    {"jsonrpc": "2.0", "id": 3, "method": "nope"}, call(4, "missing")])
    assert out[0]["result"]["serverInfo"]["name"] == "xharness" and out[0]["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["echo", "careful", "screen"]
    assert out[1]["result"]["tools"][0]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert out[2]["result"] == {"content": [{"type": "text", "text": "echo 云碩"}], "isError": False}
    assert out[3]["error"]["code"] == -32601 and out[4]["result"]["isError"] is True
    assert "stray print" in capsys.readouterr().err and "[xharness mcp] echo ok" in log
    assert "云碩" not in log                           # arguments are not logged


def test_without_elicitation_what_needs_the_person_is_refused():
    out, _ = run([call(1, "careful"), call(2, "screen")])
    assert out[1]["result"]["isError"] and out[1]["result"]["content"][0]["text"] == "not sent"
    assert out[2]["result"]["isError"] and out[2]["result"]["content"][0]["text"] == "denied"


class Client(io.StringIO):
    """Answers the server's elicitation requests as they arrive, with a ping in between."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.script = []

    def feed(self, messages):
        self.script = [json.dumps(m) + "\n" for m in messages]

    def readline(self):
        return self.script.pop(0) if self.script else ""


def test_elicitation_puts_it_to_the_person_and_other_messages_wait():
    class Out(io.StringIO):
        def write(self, s):
            for line in s.splitlines():
                message = json.loads(line)
                if message.get("method") == "elicitation/create":
                    assert "送出表單" in message["params"]["message"]
                    stdin.script[0:0] = [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n",
                                         json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": stdin.answer}) + "\n"]
            return super().write(s)

    for answer, sent in (({"action": "accept", "content": {"allow": True}}, True),
                         ({"action": "accept", "content": {"allow": False}}, False),
                         ({"action": "decline"}, False)):
        stdin, stdout = Client(answer), Out()
        stdin.feed([{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"capabilities": {"elicitation": {}}}},
                    call(1, "careful")])
        McpServer(_tools(io.StringIO()), ".", stdin, stdout, io.StringIO()).serve()
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        result = next(r for r in replies if r.get("id") == 1)["result"]
        assert result["isError"] is (not sent)
        assert any(r.get("id") == 9 and r.get("result") == {} for r in replies)   # the ping was answered after


def test_a_real_client_talks_to_xharness_mcp_serve(tmp_path):
    (tmp_path / "xharness.toml").write_text(
        'default_provider = "local"\n[providers.local]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "m"\n', encoding="utf-8")
    client = McpClient("self", sys.executable, ["-c", "from xharness.cli import main; raise SystemExit(main())",
                                                 "--config", str(tmp_path / "xharness.toml"), "mcp-serve"],
                       env={"XHARNESS_HOME": str(tmp_path / "home"),
                            "PYTHONPATH": os.path.dirname(os.path.dirname(xharness.__file__))}, cwd=str(tmp_path), timeout=60)
    try:
        client.start()
        names = [t["name"] for t in client.list_tools()]
        assert "python_run" in names and "bash" not in names and "subagent" not in names
        result = client.call("python_run", {"code": "print(6 * 7)"})
        assert not result.is_error and result.output.strip() == "42"
    finally:
        client.close()
