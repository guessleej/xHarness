"""1.12: hooks, workflows, reminders, language servers and the Agent Client Protocol."""

import copy
import io
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from xharness.acp import AcpServer
from xharness.agent import Agent, AgentOptions
from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.hooks import HookRunner, hooks_plugin, load_hooks
from xharness.llm import AssistantTurn
from xharness.lsp import LspManager, lsp_plugin
from xharness.schedule import Scheduler, parse_when, schedule_plugin
from xharness.subagent import subagent_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolContext, ToolResult, tools_plugin
from xharness.tools.fs import fs_tools_plugin
from xharness.web import WebApp
from xharness.workflow import plan_order, workflow_plugin


class Scripted:
    def __init__(self, turns):
        self.model = "fake"
        self.base_url = "http://127.0.0.1:1/v1"
        self.context_tokens = None
        self.vision = False
        self.turns = list(turns)
        self.seen = []
        self.lock = threading.Lock()

    def stream(self, messages, tools, on_delta=None):
        with self.lock:
            self.seen.append(copy.deepcopy(messages))
            turn = self.turns.pop(0) if self.turns else AssistantTurn(content="(done)")
        if callable(turn):
            turn = turn(messages)
        if on_delta and turn.content:
            on_delta(turn.content)
        return turn


class Log:
    def __init__(self):
        self.events = []
        self.id = "s1"

    def append(self, event):
        self.events.append(event)


def harness(adapter, *plugins, log=None):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    for name, plugin in plugins:
        h.use(name, plugin)
    h.ctx.provide("llm", adapter)
    if log is not None:
        h.ctx.provide("session", log)
    return h


def call(name, args, call_id="c"):
    return {"id": call_id, "name": name, "arguments": json.dumps(args)}


def recorder_tool(seen, mutating=True):
    return Tool("bash", "run", {"type": "object", "properties": {"command": {"type": "string"}}},
                lambda args, ctx: (seen.append(ctx.approve("bash: " + args.get("command", ""))) or ToolResult("ran")),
                mutating=mutating)


# --- hooks ----------------------------------------------------------------------------

def hook_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("import json,sys\nevent=json.load(sys.stdin)\n" + body)
    return f"{sys.executable} {path}"


def test_hooks_load_from_config_and_claude_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}],
                                              "Unknown": [{"hooks": [{"command": "x"}]}]}}))
    hooks = load_hooks({"files": [str(settings), str(tmp_path / "missing.json")],
                        "Stop": [{"command": "b", "timeout": 5}]})
    assert [(h.event, h.command, h.matcher) for h in hooks] == [("Stop", "b", ""), ("PreToolUse", "a", "Bash")]
    assert hooks[1].matches("bash") and hooks[1].matches("terminal") and not hooks[1].matches("read")


def test_pre_tool_use_blocks_with_exit_2_and_logs(tmp_path):
    seen = []
    log = Log()
    blocker = hook_script(tmp_path, "block.py", "sys.stderr.write('no rm here: '+event['tool_input']['command']); sys.exit(2)")
    adapter = Scripted([AssistantTurn(content="", tool_calls=[call("bash", {"command": "rm -rf x"})]), AssistantTurn(content="ok")])
    h = harness(adapter, ("hooks", hooks_plugin({"PreToolUse": [{"matcher": "Bash", "command": blocker}]})), log=log)
    h.ctx.get("tools").register(recorder_tool(seen))
    Agent(h.ctx, AgentOptions(cwd=str(tmp_path))).run("clean up")
    result = [m for m in adapter.seen[1] if m["role"] == "tool"][0]["content"]
    assert result.startswith("blocked by a hook: no rm here: rm -rf x") and seen == []
    assert any(e["type"] == "hook" and e["exit"] == 2 for e in log.events)


def test_pre_tool_use_allow_skips_and_ask_forces_the_prompt(tmp_path):
    asked = []
    for decision, mode, expect_prompt in (("allow", "prompt", False), ("ask", "auto", True)):
        seen = []
        script = hook_script(tmp_path, f"{decision}.py",
                             f"print(json.dumps({{'hookSpecificOutput': {{'permissionDecision': '{decision}'}}}}))")
        adapter = Scripted([AssistantTurn(content="", tool_calls=[call("bash", {"command": "ls"})]), AssistantTurn(content="ok")])
        h = harness(adapter, ("hooks", hooks_plugin({"PreToolUse": [{"command": script}]})))
        h.ctx.get("tools").register(recorder_tool(seen))
        agent = Agent(h.ctx, AgentOptions(cwd=str(tmp_path), approval_mode=mode,
                                          prompt=lambda summary: asked.append(summary) or True))
        agent.run("list")
        assert seen == [True] and bool(asked) == expect_prompt
        asked.clear()


def test_prompt_context_post_tool_note_and_stop_continuation(tmp_path):
    log = Log()
    context = hook_script(tmp_path, "ctx.py", "print('branch is main; tests must pass')")
    post = hook_script(tmp_path, "post.py", "print(json.dumps({'hookSpecificOutput': {'additionalContext': 'lint: 2 warnings'}}))")
    counter = tmp_path / "count"
    stop = hook_script(tmp_path, "stop.py",
                       f"import os\np={str(counter)!r}\nn=int(open(p).read()) if os.path.exists(p) else 0\nopen(p,'w').write(str(n+1))\n"
                       "print(json.dumps({'decision':'block','reason':'run the tests first'}) if n < 5 else '')")
    adapter = Scripted([AssistantTurn(content="", tool_calls=[call("bash", {"command": "ls"})])] + [AssistantTurn(content="done")] * 6)
    settings = {"UserPromptSubmit": [{"command": context}], "PostToolUse": [{"command": post}], "Stop": [{"command": stop}]}
    h = harness(adapter, ("hooks", hooks_plugin(settings)), log=log)
    h.ctx.get("tools").register(recorder_tool([], mutating=False))
    Agent(h.ctx, AgentOptions(cwd=str(tmp_path))).run("fix it")
    assert "From a hook: branch is main" in adapter.seen[0][-1]["content"]
    tool_msg = [m for m in adapter.seen[1] if m["role"] == "tool"][0]["content"]
    assert "[hook] lint: 2 warnings" in tool_msg
    continues = [m for m in adapter.seen[-1] if m["role"] == "user" and "A hook asks you to continue" in m["content"]]
    assert len(continues) == 3       # capped, even though the hook kept blocking


def test_user_prompt_block_never_reaches_the_model(tmp_path):
    adapter = Scripted([])
    block = hook_script(tmp_path, "b.py", "sys.stderr.write('outside office hours'); sys.exit(2)")
    h = harness(adapter, ("hooks", hooks_plugin({"UserPromptSubmit": [{"command": block}]})))
    assert Agent(h.ctx, AgentOptions(cwd=str(tmp_path))).run("hi") == "[blocked by a hook: outside office hours]"
    assert adapter.seen == []


def test_broken_hooks_are_not_fatal_and_notify_receives_the_turn(tmp_path):
    out = tmp_path / "notify.json"
    notifier = tmp_path / "notify.py"
    notifier.write_text(f"import sys\nopen({str(out)!r},'w').write(sys.argv[-1])\n")
    adapter = Scripted([AssistantTurn(content="all good")])
    h = harness(adapter, ("hooks", hooks_plugin({"UserPromptSubmit": [{"command": "exit 7"}],
                                                 "notify": [sys.executable, str(notifier)]})))
    assert Agent(h.ctx, AgentOptions(cwd=str(tmp_path))).run("go") == "all good"
    deadline = time.time() + 5
    while time.time() < deadline and not out.exists():
        time.sleep(0.05)
    payload = json.loads(out.read_text())
    assert payload["type"] == "agent-turn-complete" and payload["last-assistant-message"] == "all good"


def test_pre_compact_can_veto(tmp_path):
    veto = hook_script(tmp_path, "v.py", "sys.exit(2)")
    h = harness(Scripted([AssistantTurn(content="summary")]), ("hooks", hooks_plugin({"PreCompact": [{"command": veto}]})))
    agent = Agent(h.ctx, AgentOptions(cwd=str(tmp_path)))
    agent.messages += [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert agent.compact() is None


# --- workflows -------------------------------------------------------------------------

def test_plan_order_waves_and_bad_graphs():
    steps = [{"id": "read", "task": "x"}, {"id": "design", "task": "x", "depends_on": ["read"]},
             {"id": "code", "task": "x", "depends_on": ["design"]}, {"id": "tests", "task": "x", "depends_on": ["design"]},
             {"id": "review", "task": "x", "depends_on": ["code", "tests"]}]
    assert plan_order(steps) == [["read"], ["design"], ["code", "tests"], ["review"]]
    with pytest.raises(ValueError, match="cycle"):
        plan_order([{"id": "a", "task": "x", "depends_on": ["b"]}, {"id": "b", "task": "x", "depends_on": ["a"]}])
    with pytest.raises(ValueError, match="unknown"):
        plan_order([{"id": "a", "task": "x", "depends_on": ["zzz"]}])
    with pytest.raises(ValueError, match="unique"):
        plan_order([{"id": "a", "task": "x"}, {"id": "a", "task": "y"}])


def test_workflow_hands_results_forward_and_skips_after_failure(tmp_path):
    def child(messages):
        task = messages[-1]["content"]
        system = messages[0]["content"]
        if "find the bug" in task:
            return AssistantTurn(content="bug is in parser.py line 40")
        if "fix it" in task:
            assert "bug is in parser.py line 40" in system      # result of its dependency
            raise RuntimeError("model unavailable")
        return AssistantTurn(content="report")
    adapter = Scripted([child] * 6)
    h = harness(adapter, ("subagent", subagent_plugin({})), ("workflow", workflow_plugin({})))
    tool = h.ctx.get("tools").get("workflow")
    result = tool.execute({"steps": [
        {"id": "find", "task": "find the bug"},
        {"id": "fix", "task": "fix it", "depends_on": ["find"]},
        {"id": "verify", "task": "verify", "depends_on": ["fix"]},
        {"id": "notes", "task": "write notes", "depends_on": ["find"]}]},
        ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    out = result.output
    assert "## find: done" in out and "## fix: failed" in out and "## notes: done" in out
    assert "## verify: skipped" in out and "step fix did not succeed" in out
    assert tool.mutating


# --- reminders -------------------------------------------------------------------------

def test_parse_when_variants():
    now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    assert parse_when({"in_minutes": 30}, now) == (now + timedelta(minutes=30)).timestamp()
    assert parse_when({"at": "09:30"}, now) == (now + timedelta(hours=23, minutes=30)).timestamp()   # tomorrow
    assert parse_when({"at": "2026-10-22T08:00"}, now) == datetime(2026, 10, 22, 8, 0, tzinfo=now.tzinfo).timestamp()
    for bad in ({}, {"in_minutes": 0}, {"at": "2026-10-01T08:00"}, {"in_minutes": 60 * 24 * 30}, {"at": "soon"}):
        with pytest.raises(ValueError):
            parse_when(bad, now)


def test_repeating_reminders_and_restore():
    log = Log()
    scheduler = Scheduler()
    scheduler.session = lambda: log
    start = time.time()
    scheduler.add("check queue", start - 1, every_minutes=10, times=2)
    first = scheduler.take_due(start)
    assert [r.message for r in first] == ["check queue"] and scheduler.pending()[0].remaining == 1
    assert scheduler.take_due(start) == []
    restored = Scheduler()
    restored.restore(log.events)
    assert [(r.id, r.remaining) for r in restored.pending()] == [("r1", 1)]
    scheduler.take_due(start + 11 * 60)
    assert scheduler.pending() == []


def test_schedule_tool_refuses_without_a_delivering_front_end(tmp_path):
    h = harness(Scripted([]), ("schedule", schedule_plugin()))
    tool = h.ctx.get("tools").get("schedule_add")
    result = tool.execute({"message": "x", "in_minutes": 5}, ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    assert result.is_error and "Web UI" in result.output


def test_web_delivers_a_due_reminder_as_a_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    adapter = Scripted([AssistantTurn(content="checked")])

    def factory(resume, user=None):
        h = harness(adapter, ("schedule", schedule_plugin()))
        return h

    app = WebApp(ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake"),
                 approval_mode="auto", harness_factory=factory)
    conv = app.create()
    scheduler = conv.harness.ctx.get("schedule")
    assert scheduler.deliverable
    scheduler.add("look at the training job", time.time() - 1)
    assert app.tick_reminders() == 1
    deadline = time.time() + 5
    while time.time() < deadline and conv.running or not any(e["type"] == "done" for e in conv.events):
        time.sleep(0.02)
        if time.time() > deadline:
            break
    sent = adapter.seen[0][-1]["content"]
    assert sent.startswith("[提醒 r1] look at the training job") and "nobody typed it" in sent
    assert any(e["type"] == "user" and e["reminder"] for e in conv.events)


# --- language servers ------------------------------------------------------------------

FAKE_LSP = r'''
import json, sys
def read():
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        line = line.strip()
        if not line: break
        if line.lower().startswith(b"content-length:"): length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))
def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body); sys.stdout.buffer.flush()
docs = {}
while True:
    msg = read()
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": 99, "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"definitionProvider": True}}})
    elif method == "textDocument/didOpen":
        uri = params["textDocument"]["uri"]; docs[uri] = params["textDocument"]["text"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [
            {"range": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 9}}, "severity": 1, "message": "undefined name 'value'"}]}})
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": params["textDocument"]["uri"],
              "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 8}}}]})
    elif method == "textDocument/references":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": uri, "range": {"start": {"line": 0, "character": 4}}},
              {"uri": uri, "range": {"start": {"line": 3, "character": 0}}}]})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": "markdown", "value": "def main() -> None"}}})
    elif method == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": mid, "result": [{"name": "main", "kind": 12,
              "range": {"start": {"line": 0, "character": 0}}, "selectionRange": {"start": {"line": 0, "character": 4}}, "children": []}]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        sys.exit(0)
    elif mid is not None and "method" in msg:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
'''


@pytest.fixture()
def lsp_harness(tmp_path):
    server = tmp_path / "fake_lsp.py"
    server.write_text(FAKE_LSP)
    (tmp_path / "app.py").write_text("def main():\n    value()\n\n\nmain()\n")
    h = harness(Scripted([]), ("lsp", lsp_plugin({"auto": False, "servers": {"fake": {
        "command": [sys.executable, str(server)], "extensions": [".py"]}}})))
    yield h, ToolContext(cwd=str(tmp_path), approve=lambda s: True)
    h.dispose()


def test_lsp_definition_references_hover_symbols(lsp_harness):
    h, ctx = lsp_harness
    tool = h.ctx.get("tools").get("lsp")
    assert tool.execute({"action": "definition", "path": "app.py", "line": 5, "symbol": "main"}, ctx).output == "app.py:1:5  def main():"
    refs = tool.execute({"action": "references", "path": "app.py", "line": 1, "symbol": "main"}, ctx).output
    assert refs.splitlines() == ["app.py:1:5  def main():", "app.py:4:1"]
    assert tool.execute({"action": "hover", "path": "app.py", "line": 1, "symbol": "main"}, ctx).output == "def main() -> None"
    assert "function main  (line 1)" in tool.execute({"action": "symbols", "path": "app.py"}, ctx).output
    assert "undefined name 'value'" in tool.execute({"action": "diagnostics", "path": "app.py"}, ctx).output


def test_lsp_reports_bad_input_and_missing_servers(lsp_harness, tmp_path):
    h, ctx = lsp_harness
    tool = h.ctx.get("tools").get("lsp")
    assert tool.execute({"action": "definition", "path": "app.py", "line": 1, "symbol": "nope"}, ctx).is_error
    assert tool.execute({"action": "jump", "path": "app.py"}, ctx).is_error
    (tmp_path / "x.zig").write_text("x")
    missing = tool.execute({"action": "symbols", "path": "x.zig"}, ctx)
    assert missing.is_error and "no language server" in missing.output
    assert LspManager({"auto": False}).server_for("a.py") is None


# --- ACP -------------------------------------------------------------------------------

class Pipe:
    """A line-based pipe the server reads from and the test writes to."""

    def __init__(self):
        self.lines = []
        self.cond = threading.Condition()
        self.closed = False

    def write_line(self, obj):
        with self.cond:
            self.lines.append(json.dumps(obj) + "\n")
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def __iter__(self):
        while True:
            with self.cond:
                while not self.lines and not self.closed:
                    self.cond.wait(0.1)
                if not self.lines:
                    return
                line = self.lines.pop(0)
            yield line


class Out(io.StringIO):
    def messages(self):
        return [json.loads(line) for line in self.getvalue().splitlines() if line.strip()]


def wait_for(out, predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [m for m in out.messages() if predicate(m)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("timed out; got: " + json.dumps(out.messages())[-2000:])


def test_acp_session_streams_tools_diffs_and_asks_permission(tmp_path):
    (tmp_path / "notes.md").write_text("date: 11/15\n")
    adapter = Scripted([
        AssistantTurn(content="", tool_calls=[call("edit", {"path": "notes.md", "old_string": "11/15", "new_string": "11/22"})]),
        AssistantTurn(content="Done."),
    ])

    def factory():
        h = harness(adapter, ("tool-fs", fs_tools_plugin))
        return h

    config = ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake")
    stdin, stdout = Pipe(), Out()
    server = AcpServer(config, stdin=stdin, stdout=stdout, harness_factory=factory)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    stdin.write_line({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
    init = wait_for(stdout, lambda m: m.get("id") == 1)[0]["result"]
    assert init["protocolVersion"] == 1 and init["agentCapabilities"]["loadSession"] is False
    stdin.write_line({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}})
    session_id = wait_for(stdout, lambda m: m.get("id") == 2)[0]["result"]["sessionId"]
    stdin.write_line({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                      "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "change the date"}]}})
    ask = wait_for(stdout, lambda m: m.get("method") == "session/request_permission")[0]
    assert {o["kind"] for o in ask["params"]["options"]} == {"allow_once", "allow_always", "reject_once"}
    stdin.write_line({"jsonrpc": "2.0", "id": ask["id"], "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}}})
    done = wait_for(stdout, lambda m: m.get("id") == 3)[0]
    assert done["result"] == {"stopReason": "end_turn"}
    updates = [m["params"]["update"] for m in stdout.messages() if m.get("method") == "session/update"]
    call_update = next(u for u in updates if u["sessionUpdate"] == "tool_call")
    assert call_update["kind"] == "edit" and call_update["content"][0]["type"] == "diff"
    assert call_update["content"][0]["oldText"] == "date: 11/15\n" and call_update["content"][0]["newText"] == "date: 11/22\n"
    assert any(u["sessionUpdate"] == "tool_call_update" and u["status"] == "completed" for u in updates)
    assert any(u["sessionUpdate"] == "agent_message_chunk" and u["content"]["text"] == "Done." for u in updates)
    assert (tmp_path / "notes.md").read_text() == "date: 11/22\n"
    stdin.write_line({"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {"cwd": "relative/path"}})
    assert wait_for(stdout, lambda m: m.get("id") == 4)[0]["error"]["code"] == -32602
    stdin.write_line({"jsonrpc": "2.0", "id": 5, "method": "nope", "params": {}})
    assert wait_for(stdout, lambda m: m.get("id") == 5)[0]["error"]["code"] == -32601
    stdin.close()
    thread.join(5)


def test_acp_rejection_and_unsupported_blocks(tmp_path):
    adapter = Scripted([AssistantTurn(content="", tool_calls=[call("write", {"path": "x.txt", "content": "hi"})]),
                        AssistantTurn(content="ok")])
    config = ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake")
    stdin, stdout = Pipe(), Out()
    server = AcpServer(config, stdin=stdin, stdout=stdout,
                       harness_factory=lambda: harness(adapter, ("tool-fs", fs_tools_plugin)))
    threading.Thread(target=server.serve, daemon=True).start()
    stdin.write_line({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = wait_for(stdout, lambda m: m.get("id") == 1)[0]["result"]["sessionId"]
    stdin.write_line({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                      "params": {"sessionId": sid, "prompt": [{"type": "audio", "data": "x"}]}})
    assert wait_for(stdout, lambda m: m.get("id") == 2)[0]["error"]["code"] == -32602
    stdin.write_line({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                      "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "write it"}]}})
    ask = wait_for(stdout, lambda m: m.get("method") == "session/request_permission")[0]
    stdin.write_line({"jsonrpc": "2.0", "id": ask["id"], "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}}})
    assert wait_for(stdout, lambda m: m.get("id") == 3)[0]["result"]["stopReason"] == "end_turn"
    assert not (tmp_path / "x.txt").exists()
    stdin.close()


def test_a_tool_refused_three_times_is_withdrawn_for_the_task(tmp_path):
    log = Log()
    blocker = hook_script(tmp_path, "nobash.py", "sys.stderr.write('bash is disabled'); sys.exit(2)")
    turns = [AssistantTurn(content="", tool_calls=[call("bash", {"command": f"ls {i}"}, f"c{i}")]) for i in range(5)]
    adapter = Scripted(turns + [AssistantTurn(content="gave up")])
    h = harness(adapter, ("hooks", hooks_plugin({"PreToolUse": [{"matcher": "Bash", "command": blocker}]})), log=log)
    h.ctx.get("tools").register(recorder_tool([]))
    agent = Agent(h.ctx, AgentOptions(cwd=str(tmp_path)))
    agent.run("count files")
    third = [m for m in adapter.seen[3] if m["role"] == "tool"][-1]["content"]
    assert "no longer available for the rest of this task" in third
    # from then on bash is not offered; a further call is refused as unavailable, not run
    assert any(e["type"] == "tool-withdrawn" and e["name"] == "bash" for e in log.events)
    agent.run("again")      # a new task gets the tool back
    assert agent._task_hidden == set() or "bash" not in agent._task_hidden


def test_search_under_a_missing_directory_is_an_error_not_no_matches(tmp_path):
    from xharness.tools.search import search_tools_plugin

    h = Harness()
    h.use("tools", tools_plugin)
    h.use("search", search_tools_plugin)
    h.use("fs", fs_tools_plugin)
    ctx = ToolContext(cwd=str(tmp_path), approve=lambda s: True)
    for name in ("glob", "grep"):
        result = h.ctx.get("tools").get(name).execute({"pattern": "*.md" if name == "glob" else "x", "path": "/no/such/place"}, ctx)
        assert result.is_error and "no such directory" in result.output and str(tmp_path) in result.output
    missing = h.ctx.get("tools").get("read").execute({"path": "/no/such/file.md"}, ctx)
    assert "use a path relative to it" in missing.output
