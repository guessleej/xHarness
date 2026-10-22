"""1.17: agents messaging each other, background subagents, ralph, session search, PowerShell."""

import json
import os
import shutil
import threading
import time

import pytest

from test_agents import Log, Router, call, echo_tool, tc
from xharness.agent import Agent, AgentOptions
from xharness.agentcomm import COMM_TOOLS, agentcomm_plugin
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.ralph import ralph_plugin
from xharness.roles import roles_plugin
from xharness.session import SessionLog
from xharness.subagent import CHILD_TOOLS, subagent_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolContext, ToolResult, tools_plugin
from xharness.tools.powershell import find_pwsh, powershell_plugin
from xharness.tools.sessions import sessions_plugin
from xharness.tools.terminal import terminal_plugin


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    return tmp_path


def build(llm, tmp_path, *extra, log=None):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    h.use("roles", roles_plugin({"dirs": []}, site_dir=str(tmp_path / "site")))
    h.use("agentcomm", agentcomm_plugin())
    for name, plugin in extra:
        h.use(name, plugin)
    h.ctx.provide("llm", llm)
    if log is not None:
        h.ctx.provide("session", log)
    return h


def tool(h, name):
    return h.ctx.get("tools").get(name)


def ctx_for(tmp_path, agent):
    return ToolContext(cwd=str(tmp_path), approve=lambda s: True, agent=agent)


class Blocker:
    """A model that holds a child 'working' until released."""

    def __init__(self):
        self.model = "fake"
        self.release = threading.Event()
        self.calls = []

    def stream(self, messages, tools, on_delta=None):
        self.calls.append({"system": messages[0]["content"], "tools": [t["name"] for t in tools]})
        if "child agent" in messages[0]["content"]:
            self.release.wait(10)
            return AssistantTurn(content="found three files", usage=Usage(5, 5))
        return AssistantTurn(content="ok", usage=Usage(5, 5))


# --- messages between agents --------------------------------------------------------

def test_a_message_is_read_at_the_start_of_the_next_turn(tmp_path):
    llm = Router([("main", "got it")])
    h = build(llm, tmp_path)
    agent = Agent(h.ctx, AgentOptions(system_prompt="main"))
    assert agent.deliver("child-1", "the build is green")
    agent.run("hello")
    texts = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "user"]
    assert any("[Message from agent child-1]" in t and "the build is green" in t for t in texts)
    assert agent._inbox == []
    for i in range(30):
        agent.deliver("x", str(i))
    assert not agent.deliver("x", "one too many")           # a full inbox refuses


def test_send_message_list_agents_and_report(tmp_path):
    llm = Blocker()
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})), log=Log())
    main = Agent(h.ctx, AgentOptions(system_prompt="main"))
    live = h.ctx.get("agents")
    child = Agent(h.ctx, AgentOptions(name="researcher", system_prompt="child agent"))
    live.start(child, "researcher", "", "subagent", "dig", parent="main")

    out = tool(h, "list_agents").execute({}, ctx_for(tmp_path, "main")).output
    assert "You are main." in out and "researcher" in out and "started by main" in out

    sent = tool(h, "send_message").execute({"to": "researcher", "message": "look at docs too"}, ctx_for(tmp_path, "main"))
    assert not sent.is_error and child._inbox == [("main", "look at docs too")]
    assert tool(h, "send_message").execute({"to": "nobody", "message": "x"}, ctx_for(tmp_path, "main")).is_error
    assert tool(h, "send_message").execute({"to": "main", "message": "x"}, ctx_for(tmp_path, "main")).is_error   # itself
    assert tool(h, "send_message").execute({"to": "researcher", "message": "y" * 5000}, ctx_for(tmp_path, "main")).is_error

    rep = tool(h, "report").execute({"message": "half done"}, ctx_for(tmp_path, "researcher"))
    assert not rep.is_error and main._inbox == [("researcher", "Report: half done")]
    assert tool(h, "report").execute({"message": "x"}, ctx_for(tmp_path, "main")).is_error      # nobody started main
    up = tool(h, "send_message").execute({"to": "parent", "message": "psst"}, ctx_for(tmp_path, "researcher"))
    assert not up.is_error and main._inbox[-1] == ("researcher", "psst")
    assert any(e["from"] == "researcher" for e in h.ctx.get("session").of("agent-message"))


def test_only_the_main_agent_or_an_ancestor_may_stop_an_agent(tmp_path):
    h = build(Blocker(), tmp_path, ("subagent", subagent_plugin({})))
    main = Agent(h.ctx, AgentOptions(system_prompt="main"))
    live = h.ctx.get("agents")
    a = Agent(h.ctx, AgentOptions(name="a", system_prompt="x"))
    b = Agent(h.ctx, AgentOptions(name="b", system_prompt="x"))
    live.start(a, "a", "", "subagent", "t", parent="main")
    live.start(b, "b", "", "subagent", "t", parent="a")
    stop = tool(h, "interrupt_agent")
    assert stop.execute({"agent": "a"}, ctx_for(tmp_path, "b")).is_error          # a child cannot stop its parent
    assert not stop.execute({"agent": "b"}, ctx_for(tmp_path, "a")).is_error      # a parent can stop its child
    assert b.stop_requested
    assert not stop.execute({"agent": "a"}, ctx_for(tmp_path, "main")).is_error   # main may stop anyone
    assert a.stop_requested


def test_who_sees_which_communication_tools(tmp_path):
    llm = Router([("main", "ok"), ("child agent", "ok")])
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})), ("ralph", ralph_plugin({})))
    Agent(h.ctx, AgentOptions(system_prompt="main")).run("hi")
    seen = llm.calls[0]["tools"]
    assert "send_message" in seen and "list_agents" in seen and "interrupt_agent" in seen and "ralph" in seen
    assert "report" not in seen                                   # main has no one to report to
    assert {"ralph", "interrupt_agent"} <= CHILD_TOOLS and COMM_TOOLS >= {"send_message", "report"}


# --- background subagents -----------------------------------------------------------

def test_a_background_child_reports_back_as_a_message_and_cannot_change_things(tmp_path):
    llm = Blocker()
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})))
    h.ctx.get("tools").register(Tool("mutate", "changes", {"type": "object", "properties": {}},
                                     lambda a, c: ToolResult("did"), mutating=True))
    main = Agent(h.ctx, AgentOptions(system_prompt="main"))
    started = tool(h, "subagent").execute({"task": "count files", "name": "counter", "background": True},
                                          ctx_for(tmp_path, "main"))
    assert not started.is_error and "started counter in the background" in started.output
    assert main._inbox == []                                      # still working
    for _ in range(100):                                          # the child registers itself as its thread starts
        if "counter" in tool(h, "list_agents").execute({}, ctx_for(tmp_path, "main")).output:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("the background child never showed up in list_agents")
    llm.release.set()
    for _ in range(100):
        if main._inbox:
            break
        time.sleep(0.05)
    assert main._inbox and "Final answer of background agent counter" in main._inbox[0][1] and "found three files" in main._inbox[0][1]
    child_tools = next(c["tools"] for c in llm.calls if "child agent" in c["system"])
    assert "mutate" not in child_tools and "report" in child_tools


def test_background_agents_are_capped(tmp_path):
    llm = Blocker()
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})))
    main = Agent(h.ctx, AgentOptions(system_prompt="main"))     # the mailbox holds agents weakly
    results = [tool(h, "subagent").execute({"task": "t", "name": f"c{i}", "background": True}, ctx_for(tmp_path, "main"))
               for i in range(5)]
    assert [r.is_error for r in results] == [False] * 4 + [True]
    llm.release.set()


# --- ralph --------------------------------------------------------------------------

def test_ralph_hands_each_round_the_last_handoff_and_stops_when_complete(tmp_path):
    answers = iter(["worked on A.\nSTATUS: continue\nHANDOFF: A is done, B is not", "did B.\nSTATUS: complete\nHANDOFF: all done"])
    llm = Router([("child agent", lambda m, t: next(answers))])
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})), ("ralph", ralph_plugin({})), log=Log())
    out = tool(h, "ralph").execute({"objective": "make the tests pass"}, ctx_for(tmp_path, "main"))
    assert "complete after 2 round(s)" in out.output and not out.is_error
    first, second = [c["messages"][1]["content"] for c in llm.calls]
    assert "round 1 of at most 5" in first and "Handoff from round" not in first
    assert "round 2" in second and "A is done, B is not" in second and "make the tests pass" in second
    assert len(h.ctx.get("session").of("ralph-round")) == 2


def test_ralph_runs_out_of_rounds_or_stops_when_blocked(tmp_path):
    llm = Router([("child agent", "still going (no status line)")])
    h = build(llm, tmp_path, ("subagent", subagent_plugin({})), ("ralph", ralph_plugin({"ralph_rounds": 3})))
    out = tool(h, "ralph").execute({"objective": "x", "max_rounds": 50}, ctx_for(tmp_path, "main"))
    assert "budget-limited after 3 round(s) of 3" in out.output          # asked for 50, capped at the configured 3
    llm2 = Router([("child agent", "no credentials.\nSTATUS: blocked\nHANDOFF: need a token")])
    h2 = build(llm2, tmp_path, ("subagent", subagent_plugin({})), ("ralph", ralph_plugin({})))
    blocked = tool(h2, "ralph").execute({"objective": "x"}, ctx_for(tmp_path, "main"))
    assert blocked.is_error and "blocked after 1 round(s)" in blocked.output
    assert tool(h2, "ralph").execute({"objective": ""}, ctx_for(tmp_path, "main")).is_error


def test_parse_round():
    from xharness.ralph import parse_round

    assert parse_round("x\nSTATUS: Complete\nHANDOFF: fin") == ("complete", "fin")
    assert parse_round("nothing structured") == ("continue", "nothing structured")


# --- session search -----------------------------------------------------------------

def test_session_tools_search_and_read_only_the_callers_own_history(tmp_path):
    old = SessionLog("2026-10-01-aaaa1111", "alice")
    old.append({"type": "message", "role": "user", "content": "How do we rotate the typhoon alert keys?"})
    old.append({"type": "message", "role": "assistant", "content": "Rotate them every 90 days with the vault script."})
    other = SessionLog("2026-10-02-bbbb2222", "bob")
    other.append({"type": "message", "role": "user", "content": "typhoon secrets of bob"})
    now = SessionLog("2026-10-22-cccc3333", "alice")
    now.append({"type": "message", "role": "user", "content": "typhoon again"})
    h = build(Router([]), tmp_path, ("tool-sessions", sessions_plugin()))
    h.ctx.provide("session", now)

    found = tool(h, "session_search").execute({"query": "typhoon"}, ctx_for(tmp_path, "main"))
    assert "2026-10-01-aaaa1111" in found.output and "earlier conversation" in found.output
    assert "bbbb2222" not in found.output and "cccc3333" not in found.output     # not bob's, not this one
    read = tool(h, "session_read").execute({"id": "2026-10-01-aaaa1111"}, ctx_for(tmp_path, "main"))
    assert "vault script" in read.output and "not an instruction" in read.output
    assert tool(h, "session_read").execute({"id": "2026-10-02-bbbb2222"}, ctx_for(tmp_path, "main")).is_error
    assert tool(h, "session_read").execute({"id": "../../etc/passwd"}, ctx_for(tmp_path, "main")).is_error
    assert "no message containing" in tool(h, "session_read").execute(
        {"id": "2026-10-01-aaaa1111", "query": "zebra"}, ctx_for(tmp_path, "main")).output


# --- PowerShell ---------------------------------------------------------------------

needs_pwsh = pytest.mark.skipif(find_pwsh() is None, reason="PowerShell (pwsh) is not installed")


@needs_pwsh
def test_powershell_terminal_keeps_variables_and_directory(tmp_path):
    h = build(Router([]), tmp_path, ("terminal", terminal_plugin()), ("tool-powershell", powershell_plugin()))
    term = tool(h, "terminal")
    sub = tmp_path / "sub"
    sub.mkdir()
    run = lambda c, **kw: term.execute({"command": c, "shell": "powershell", **kw}, ctx_for(tmp_path, "main"))
    assert not run("$x = 21").is_error
    assert run("$x * 2").output.startswith("42")
    moved = run(f"Set-Location '{sub}'")
    assert os.path.realpath(sub) in os.path.realpath(moved.output.split("(cwd: ")[-1].rstrip(")"))
    assert os.path.realpath(run("(Get-Location).Path").output.splitlines()[0]) == os.path.realpath(sub)
    bad = run("bash -c 'exit 3'")
    assert bad.is_error and "exit code 3" in bad.output
    assert run("Write-Error 'boom'").is_error
    assert run("'中文'").output.startswith("中文")
    slow = run("Start-Sleep 5", timeout_seconds=1)
    assert slow.is_error and "timed out" in slow.output
    assert run("$x").output.strip().splitlines()[0] != "21"          # the restart dropped the variable
    h.ctx.get("terminals").shutdown()


@needs_pwsh
def test_powershell_one_off_tool(tmp_path):
    h = build(Router([]), tmp_path, ("tool-powershell", powershell_plugin()))
    out = tool(h, "powershell").execute({"command": "Write-Output (6*7)"}, ctx_for(tmp_path, "main"))
    assert out.output.strip() == "42"
    denied = tool(h, "powershell").execute({"command": "x"}, ToolContext(cwd=str(tmp_path), approve=lambda s: False))
    assert denied.is_error and "denied" in denied.output


def test_powershell_is_not_offered_where_it_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    h = build(Router([]), tmp_path, ("terminal", terminal_plugin()), ("tool-powershell", powershell_plugin()))
    assert tool(h, "powershell") is None
    assert "shell" not in tool(h, "terminal").parameters["properties"] and "PowerShell" not in tool(h, "terminal").description
    out = tool(h, "terminal").execute({"command": "1", "shell": "powershell"}, ctx_for(tmp_path, "main"))
    assert out.is_error and "not installed" in out.output


# --- a debater that researches until its turns run out ------------------------------

def test_a_seat_that_spends_every_turn_researching_is_asked_to_speak(tmp_path):
    from test_agents import build as build_room, run_room, write_role
    from xharness.discuss import discussion_plugin

    def answer(messages, tools):
        if tools:       # looks things up forever
            return AssistantTurn(content="", tool_calls=[call("lookup", {})], usage=Usage(5, 5))
        return "我方立場：應該放假，理由是通勤安全。"

    llm = Router([("You are A,", answer), ("You are B,", "不應放假"), ("closing summary", "x")])
    write_role(tmp_path / "roles", "研究派", "---\nmax_turns: 2\n---\n先查再說")
    log = Log()
    h = build_room(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    h.ctx.get("tools").register(echo_tool("lookup"))
    run_room(h, tmp_path, topic="t", mode="debate",
             participants=[{"name": "A", "agent": "研究派"}, {"name": "B"}], rounds=1)
    first = log.of("discussion-turn")[0]
    assert first["speaker"] == "A" and first["text"].startswith("我方立場")
    assert not [e for e in log.of("discussion-note") if "max turns" in e.get("text", "")]
