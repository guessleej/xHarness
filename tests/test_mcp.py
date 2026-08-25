import sys
import textwrap

from xharness.context import Harness
from xharness.mcp import McpClient, mcp_plugin
from xharness.tools import ToolContext, ToolRegistry, tools_plugin

FAKE_SERVER = textwrap.dedent(
    """
    import json, sys

    TOOLS = [
        {
            "name": "echo",
            "description": "Echo the text back.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "peek",
            "description": "A read-only tool.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        },
    ]

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if "id" not in message:
            continue  # notification
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message["params"]
            if params["name"] == "echo":
                result = {"content": [{"type": "text", "text": params["arguments"]["text"]}]}
            else:
                result = {"content": [{"type": "text", "text": "peeked"}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
        sys.stdout.flush()
    """
)


def write_server(tmp_path):
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER)
    return str(path)


def test_client_handshake_list_and_call(tmp_path):
    client = McpClient("fake", sys.executable, args=[write_server(tmp_path)], timeout=10)
    client.start()
    try:
        tools = client.list_tools()
        assert [tool["name"] for tool in tools] == ["echo", "peek"]
        result = client.call("echo", {"text": "hello"})
        assert result.output == "hello"
        assert not result.is_error
    finally:
        client.close()


def test_plugin_registers_namespaced_tools_and_approval(tmp_path):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use(
        "mcp",
        mcp_plugin({"fake": {"command": sys.executable, "args": [write_server(tmp_path)], "timeout_seconds": 10}}),
    )
    try:
        registry: ToolRegistry = harness.ctx.get("tools")
        names = {tool.name for tool in registry.list()}
        assert {"mcp__fake__echo", "mcp__fake__peek"} <= names

        echo = registry.get("mcp__fake__echo")
        peek = registry.get("mcp__fake__peek")
        assert echo.mutating  # no readOnlyHint -> treated as side-effectful
        assert not peek.mutating

        deny = ToolContext(cwd=".", approve=lambda _s: False)
        denied = echo.execute({"text": "x"}, deny)
        assert denied.is_error and "denied" in denied.output
        # Read-only tools bypass approval entirely.
        peeked = peek.execute({}, deny)
        assert peeked.output == "peeked"

        allow = ToolContext(cwd=".", approve=lambda _s: True)
        assert echo.execute({"text": "hi"}, allow).output == "hi"
    finally:
        harness.dispose()


def test_broken_server_is_skipped_not_fatal(tmp_path):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("mcp", mcp_plugin({"broken": {"command": "/nonexistent/mcp-server"}}))
    try:
        registry: ToolRegistry = harness.ctx.get("tools")
        assert registry.list() == []
    finally:
        harness.dispose()
