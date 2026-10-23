"""1.13: agent roles, the live-agent register, and discussions between agents."""

import copy
import json
import os
import threading
import time

import pytest

from xharness.agent import Agent, AgentOptions
from xharness.commands import CommandHost, dispatch
from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.discuss import discussion_plugin, parse_participants
from xharness.llm import AssistantTurn, OpenAIAdapter, Usage
from xharness.roles import CLAUDE_NAMES, LiveAgents, RoleStore, parse_role, roles_plugin
from xharness.session import export_markdown, sessions_dir
from xharness.subagent import CHILD_TOOLS, subagent_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolContext, ToolResult, tools_plugin
from xharness.web import WebApp
from xharness.workflow import workflow_plugin


class Router:
    """A fake model that answers according to who is asking (the system prompt)."""

    def __init__(self, rules, model="fake", tokens=10):
        self.model = model
        self.base_url = "http://127.0.0.1:1/v1"
        self.context_tokens = None
        self.vision = False
        self.rules = rules
        self.tokens = tokens
        self.calls = []
        self.lock = threading.Lock()

    def stream(self, messages, tools, on_delta=None):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        with self.lock:
            self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": [t["name"] for t in tools]})
        out = "ok"
        for needle, answer in self.rules:
            if needle in system or (not system and needle in str(messages[-1].get("content"))):
                out = answer(messages, tools) if callable(answer) else answer
                break
        if isinstance(out, str):
            out = AssistantTurn(content=out)
        if out.usage is None:
            out.usage = Usage(self.tokens, self.tokens)
        return out

    def asked(self, needle):
        return [c for c in self.calls if needle in c["system"]]


class Log:
    def __init__(self):
        self.events = []
        self.id = "s1"

    def append(self, event):
        self.events.append({"ts": "2026-10-21T01:00:00+00:00", **event})

    def of(self, kind):
        return [e for e in self.events if e.get("type") == kind]


def echo_tool(name, seen=None):
    return Tool(name, name, {"type": "object", "properties": {}},
                lambda args, ctx: (seen.append(name) if seen is not None else None) or ToolResult(f"{name} ran"))


def build(llm, tmp_path, *extra, log=None, roles_settings=None, providers=None, allowed=()):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    h.use("roles", roles_plugin(roles_settings or {"dirs": [str(tmp_path / "roles")]}, site_dir=str(tmp_path / "site"),
                                providers=providers or {}, allowed=allowed))
    for name, plugin in extra:
        h.use(name, plugin)
    h.ctx.provide("llm", llm)
    if log is not None:
        h.ctx.provide("session", log)
    return h


def write_role(directory, name, text):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8") as handle:
        handle.write(text)


def tc(tmp_path):
    return ToolContext(cwd=str(tmp_path), approve=lambda s: True)


def call(name, args, call_id="c1"):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    return tmp_path


# --- roles ------------------------------------------------------------------------

def test_role_file_in_claude_code_layout():
    role = parse_role("---\nname: reviewer\ndescription: >\n  Reviews changes\n  for bugs\ntools: Read, Grep, Bash, mcp__db__query\n"
                      "model: inherit\nmax_turns: 7\nmax_tokens: 40,000\n---\nYou review code.\n", "x", "工作目錄", "/p")
    assert role.name == "reviewer" and role.description == "Reviews changes for bugs"
    assert role.tools == ("read", "grep", "bash", "mcp__db__query")      # Claude Code names map to ours
    assert role.model is None and role.max_turns == 7 and role.max_tokens == 40000
    assert role.prompt == "You review code."
    assert parse_role("---\nname: bad name!\n---\nx", "x", "", "") is None
    assert parse_role("no front matter", "plain", "", "").name == "plain"     # the file name stands in
    assert CLAUDE_NAMES["websearch"] == "web_search"


def test_roles_first_match_wins_and_can_be_switched_off(tmp_path):
    work = tmp_path / "work"
    write_role(work / ".xharness" / "agents", "analyst", "---\ndescription: mine\n---\nmine")
    write_role(work / ".claude" / "agents", "claude-one", "---\ndescription: from claude\n---\nc")
    write_role(tmp_path / "site" / ".xharness" / "agents", "analyst", "---\ndescription: site's\n---\nsite")
    store = RoleStore({}, site_dir=str(tmp_path / "site"))
    found = {r.name: r for r in store.discover(str(work))}
    assert found["analyst"].description == "mine" and found["analyst"].source == "工作目錄"
    assert "claude-one" in found
    assert "analyst" in store.index(str(work)) and "subagent" in store.index(str(work))
    assert RoleStore({"enabled": False}, site_dir=str(tmp_path / "site")).discover(str(work)) == []


def test_role_model_must_be_configured_and_allowed(tmp_path):
    providers = {"granite": {"base_url": "http://x/v1", "model": "granite-4.2"}, "ocr": {"base_url": "http://x/v1", "model": "eyes"}}
    role = parse_role("---\nmodel: granite\n---\nx", "r", "", "")
    adapter, note = RoleStore({}, providers=providers).resolve_model(role)
    assert isinstance(adapter, OpenAIAdapter) and adapter.model == "granite-4.2" and note == ""
    assert RoleStore({}, providers=providers, allowed=("other",)).resolve_model(role)[0] is None
    assert "may not use" in RoleStore({}, providers=providers, allowed=("other",)).resolve_model(role)[1]
    assert RoleStore({}, providers=providers).resolve_model(parse_role("---\nmodel: nope\n---\nx", "r", "", ""))[0] is None
    assert RoleStore({}, providers=providers, describer="ocr").resolve_model(
        parse_role("---\nmodel: ocr\n---\nx", "r", "", ""))[0] is None      # the image describer is not a chat model


def test_subagent_with_a_role_gets_only_its_tools_and_instructions(tmp_path):
    write_role(tmp_path / "roles", "reader", "---\ndescription: reads\ntools: Read\nmax_tokens: 5000\n---\nOnly read, never write.")
    llm = Router([("child agent named", lambda m, t: AssistantTurn(content="read it"))])
    log = Log()
    h = build(llm, tmp_path, ("subagent", subagent_plugin()), log=log)
    seen = []
    for name in ("read", "write", "bash"):
        h.ctx.get("tools").register(echo_tool(name, seen))
    result = h.ctx.get("tools").get("subagent").execute({"task": "look", "agent": "reader"}, tc(tmp_path))
    assert result.output == "read it"
    child = llm.asked("child agent named")[0]
    assert child["tools"] == ["read"] and "Only read, never write." in child["system"]
    run = log.of("agent-run")[0]
    assert run["role"] == "reader" and run["kind"] == "subagent" and run["status"] == "done" and run["calls"] == 1
    bad = h.ctx.get("tools").get("subagent").execute({"task": "x", "agent": "ghost"}, tc(tmp_path))
    assert bad.is_error and "reader" in bad.output


def test_workflow_steps_can_name_roles(tmp_path):
    write_role(tmp_path / "roles", "tester", "---\ntools: bash\n---\nYou run tests.")
    llm = Router([("child agent named 'step-t'", "tests pass"), ("child agent named", "done")])
    log = Log()
    h = build(llm, tmp_path, ("subagent", subagent_plugin()), ("workflow", workflow_plugin()), log=log)
    h.ctx.get("tools").register(echo_tool("bash"))
    h.ctx.get("tools").register(echo_tool("write"))
    out = h.ctx.get("tools").get("workflow").execute(
        {"steps": [{"id": "w", "task": "write"}, {"id": "t", "task": "test", "depends_on": ["w"], "agent": "tester"}]}, tc(tmp_path))
    assert not out.is_error
    tester = llm.asked("child agent named 'step-t'")[0]
    assert tester["tools"] == ["bash"] and "You run tests." in tester["system"]
    assert {(r["name"], r["kind"], r["role"]) for r in log.of("agent-run")} == {("step-w", "workflow", ""), ("step-t", "workflow", "tester")}


def test_agent_token_allowance_stops_it(tmp_path):
    loop = lambda m, t: AssistantTurn(content="", tool_calls=[call("read", {})], usage=Usage(400, 200))   # noqa: E731
    llm = Router([("", loop)])
    h = build(llm, tmp_path)
    h.ctx.get("tools").register(echo_tool("read"))
    agent = Agent(h.ctx, AgentOptions(max_tokens=1000, max_turns=10))
    answer = agent.run("go")
    assert "1,000-token allowance" in answer and agent.llm_calls == 2


def test_agent_runs_on_its_own_model(tmp_path):
    house, own = Router([("", "house")], model="house"), Router([("", "own")], model="own")
    h = build(house, tmp_path)
    assert Agent(h.ctx, AgentOptions(llm=own)).run("x") == "own"
    assert not house.calls and "'own'" in own.calls[0]["system"]


# --- live agents ---------------------------------------------------------------------

def test_live_agents_record_stop_and_forget_old_ones(tmp_path):
    log = Log()
    h = build(Router([]), tmp_path, log=log)
    live = h.ctx.get("agents")
    changes = []
    h.ctx.on("agents/changed", lambda _p: changes.append(1))
    a = Agent(h.ctx, AgentOptions())
    first = live.start(a, "a", "analyst", "discussion", "topic", room="r1")
    live.activity(first, "grep")
    assert live.public()[0]["activity"] == "grep" and live.running() == 1
    assert live.stop_room("r1") == 1 and a.stop_requested
    record = live.end(first, ok=True)
    assert record["status"] == "stopped" and log.of("agent-run")[0]["room"] == "r1" and changes
    assert not live.stop(first)                    # already ended
    for i in range(40):
        live.end(live.start(Agent(h.ctx, AgentOptions()), f"x{i}"), ok=True)
    assert len(live.public()) <= 31


def test_children_are_registered_while_they_work(tmp_path):
    states = []

    def child(messages, tools):
        states.append([a["status"] for a in h.ctx.get("agents").public()])
        return AssistantTurn(content="fine")
    llm = Router([("child agent named", child)])
    h = build(llm, tmp_path, ("subagent", subagent_plugin()))
    h.ctx.get("tools").get("subagent").execute({"task": "x"}, tc(tmp_path))
    assert states == [["running"]] and h.ctx.get("agents").public()[0]["status"] == "done"


def test_discussion_is_a_child_tool():
    assert "discussion" in CHILD_TOOLS     # participants and children cannot open rooms of their own


# --- discussions -----------------------------------------------------------------------

def speaker(first, later="【無補充】"):
    def answer(messages, tools):
        asked = [m for m in messages if m["role"] == "user"]
        return first if len(asked) == 1 else later
    return answer


def run_room(h, tmp_path, **args):
    return h.ctx.get("tools").get("discussion").execute(args, tc(tmp_path))


def test_roundtable_ends_when_a_round_has_nothing_new(tmp_path):
    llm = Router([("You are 甲,", speaker("甲：先看註冊率 95.2%")), ("You are 乙,", speaker("乙：同意，另看休學率")),
                  ("You are 丙,", speaker("丙：不同意，數據不夠")), ("closing summary", "共識：先看註冊率。分歧：丙認為數據不夠。")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    out = run_room(h, tmp_path, topic="註冊率怎麼看", participants=["甲", "乙", "丙"], rounds=5)
    turns = log.of("discussion-turn")
    assert [t["speaker"] for t in turns] == ["甲", "乙", "丙", "甲", "乙", "丙"]
    end = log.of("discussion-end")[0]
    assert end["reason"] == "converged" and end["turns"] == 6 and "丙認為數據不夠" in end["summary"]
    assert "整輪都沒有新意見" in out.output and "[第 1 輪] 丙：丙：不同意" in out.output
    # Each participant is told only what was said since its last turn.
    second = llm.asked("You are 甲,")[1]["messages"][-1]["content"]
    assert "Since you last spoke" in second and "乙：同意" in second and "甲：先看" not in second
    first_of_c = llm.asked("You are 丙,")[0]["messages"][-1]["content"]
    assert "甲：先看" in first_of_c and "乙：同意" in first_of_c
    assert len(log.of("agent-run")) == 3 and all(r["kind"] == "discussion" for r in log.of("agent-run"))


def test_debate_gets_sides_and_a_verdict(tmp_path):
    llm = Router([("You are A,", "應該延長修業"), ("You are B,", "不應延長"), ("closing summary", "裁決：正方較有說服力")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    out = run_room(h, tmp_path, topic="延長修業年限", mode="debate", participants=[{"name": "A"}, {"name": "B"}], rounds=2)
    start = log.of("discussion-start")[0]
    assert [p["stance"] for p in start["participants"]] == ["正方", "反方"]
    assert "Your side: 正方" in llm.asked("You are A,")[0]["system"]
    assert "裁決" in llm.asked("closing summary")[0]["messages"][-1]["content"]
    assert log.of("discussion-end")[0]["reason"] == "rounds" and "正方較有說服力" in out.output


def test_a_debater_who_only_says_no_data_is_asked_to_argue(tmp_path):
    reasons = "依推論：放假保障通勤安全，勞工與學生不必冒險出門，這個代價遠小於停工一天的損失，理由有三。" * 3
    llm = Router([("You are A,", speaker("查不到相關資料，無法提供資料佐證。", reasons)), ("You are B,", "不應放假"),
                  ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="颱風天是否放假", mode="debate", participants=[{"name": "A"}, {"name": "B"}], rounds=1)
    first = log.of("discussion-turn")[0]
    assert first["speaker"] == "A" and first["text"].startswith("依推論") and first["rechecked"]
    assert "查不到資料不是不論述的理由" in llm.asked("You are A,")[1]["messages"][-1]["content"]
    assert "argue from principle" in llm.asked("You are A,")[0]["system"]


def test_review_ends_when_every_reviewer_approves(tmp_path):
    llm = Router([("You are 作者,", speaker("初稿", "修正稿")), ("You are 審查,", speaker("第二段數字沒有出處", "【通過】")),
                  ("closing summary", "全數通過")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="寫一段說明", mode="review", participants=["作者", "審查"], rounds=5)
    assert "You are the author" in llm.asked("You are 作者,")[0]["system"]
    assert "You are a reviewer" in llm.asked("You are 審查,")[0]["system"]
    assert log.of("discussion-end")[0]["reason"] == "approved" and len(log.of("discussion-turn")) == 4


def test_free_mode_follows_the_moderator_and_falls_back_to_taking_turns(tmp_path):
    picks = iter(['{"next": "乙"}', "not json", '{"next": "乙"}', '{"end": true}'])
    llm = Router([("You moderate", lambda m, t: next(picks)), ("You are 甲,", "甲說"), ("You are 乙,", "乙說"),
                  ("closing summary", "結論")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="t", mode="free", participants=["甲", "乙"], rounds=3)
    order = [t["speaker"] for t in log.of("discussion-turn")]
    # 乙 as asked; garbage -> whoever spoke least (甲); 乙 again; then the moderator ends it.
    assert order == ["乙", "甲", "乙"] and log.of("discussion-end")[0]["reason"] == "moderator"


def test_free_mode_cannot_end_before_everyone_spoke(tmp_path):
    llm = Router([("You moderate", '{"end": true}'), ("You are 甲,", "甲說"), ("You are 乙,", "乙說"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="t", mode="free", participants=["甲", "乙"], rounds=2)
    assert {t["speaker"] for t in log.of("discussion-turn")} == {"甲", "乙"}


def test_room_stops_at_its_token_allowance(tmp_path):
    llm = Router([("You are", "說話"), ("closing summary", "x")], tokens=500)
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin({"max_tokens": 2500})), log=log)
    run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=8)
    end = log.of("discussion-end")[0]
    assert end["reason"] == "budget" and end["turns"] == 3     # 1,000 tokens a turn; the fourth would pass 2,500


def test_stopping_one_participant_lets_the_others_continue(tmp_path):
    def first(messages, tools):
        h.ctx.get("agents").stop(next(a["id"] for a in h.ctx.get("agents").public() if a["name"] == "乙"))
        return "甲說"
    llm = Router([("You are 甲,", first), ("You are 乙,", "乙說"), ("You are 丙,", "丙說"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="t", participants=["甲", "乙", "丙"], rounds=2)
    speakers = [t["speaker"] for t in log.of("discussion-turn")]
    assert "乙" not in speakers and speakers.count("甲") == 2 and speakers.count("丙") == 2
    assert {r["name"]: r["status"] for r in log.of("agent-run")}["乙"] == "stopped"


def test_stopping_the_room_ends_it(tmp_path):
    def first(messages, tools):
        h.ctx.get("agents").stop_all()
        return "甲說"
    llm = Router([("You are 甲,", first), ("You are 乙,", "乙說"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=3)
    # The reply already on its way is kept (a model call cannot be interrupted); nobody speaks after it.
    assert log.of("discussion-end")[0]["reason"] == "stopped"
    assert [t["speaker"] for t in log.of("discussion-turn")] == ["甲"]


def test_participants_with_roles_get_their_tools_and_model_note(tmp_path):
    write_role(tmp_path / "roles", "stats", "---\ndescription: statistics\ntools: grep\nmodel: missing\n---\nYou run tests of significance.")
    llm = Router([("You are stats,", "p = 0.03"), ("You are 甲,", "好"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    for name in ("grep", "write"):
        h.ctx.get("tools").register(echo_tool(name))
    run_room(h, tmp_path, topic="t", participants=["stats", "甲"], rounds=1)
    stats = llm.asked("You are stats,")[0]
    assert stats["tools"] == ["grep"] and "tests of significance" in stats["system"]
    assert "discussion" not in llm.asked("You are 甲,")[0]["tools"]
    assert any("not configured" in n["text"] for n in log.of("discussion-note"))


def test_bad_participant_lists_are_refused(tmp_path):
    h = build(Router([]), tmp_path, ("discussion", discussion_plugin()))
    cwd = str(tmp_path)
    assert parse_participants(h.ctx, ["only"], "roundtable", cwd)[1]
    assert "both called" in parse_participants(h.ctx, ["a", "a"], "roundtable", cwd)[1]
    assert "no agent role" in parse_participants(h.ctx, [{"name": "a", "agent": "ghost"}, "b"], "roundtable", cwd)[1]
    assert "two different stances" in parse_participants(
        h.ctx, [{"name": "a", "stance": "正方"}, {"name": "b", "stance": "正方"}], "debate", cwd)[1]
    assert parse_participants(h.ctx, ["a[1]", "b"], "roundtable", cwd)[1]
    out = run_room(h, tmp_path, topic="t", mode="chat", participants=["a", "b"])
    assert out.is_error and "roundtable" in out.output


# --- export, report, commands, web ---------------------------------------------------------

def test_export_and_report_include_discussions_and_agents(tmp_path):
    from xharness.report import collect, format_report

    llm = Router([("You are 甲,", "甲的看法"), ("You are 乙,", "乙的看法"), ("closing summary", "兩人看法不同")])
    os.makedirs(sessions_dir(None), exist_ok=True)

    class FileLog(Log):
        def append(self, event):
            super().append(event)
            with open(os.path.join(sessions_dir(None), "d1.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.events[-1], ensure_ascii=False) + "\n")
    log = FileLog()
    log.append({"type": "meta", "model": "fake"})
    log.append({"type": "telemetry", "total_tokens": 80, "calls": 4})
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="要不要開夜間班", participants=["甲", "乙"], rounds=1)
    text = export_markdown("d1", None)
    assert "## 圓桌討論：要不要開夜間班" in text and "**第 1 輪・乙**" in text and "乙的看法" in text
    assert "兩人看法不同" in text and "## 代理執行紀錄" in text
    from datetime import datetime, timezone
    report = collect(ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake"),
                     since="30d", now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    agents = report["agents"]
    assert agents["runs"] == 2 and agents["discussions"]["count"] == 1 and agents["discussions"]["turns"] == 2
    assert agents["by_role"][0]["role"].startswith("（未指定角色的討論參與者")
    assert "討論 1 場" in format_report(report)


def test_agents_command_lists_roles_and_stops(tmp_path):
    write_role(tmp_path / "roles", "analyst", "---\ndescription: 查指標\ntools: read\n---\nx")
    h = build(Router([]), tmp_path)
    agent = Agent(h.ctx, AgentOptions(cwd=str(tmp_path)))
    host = CommandHost(agent=agent, ctx=h.ctx, config=None)
    shown = dispatch(host, "/agents")
    assert "analyst" in shown.text and shown.data["roles"][0]["tools"] == ["read"]
    child = Agent(h.ctx, AgentOptions())
    h.ctx.get("agents").start(child, "c")
    assert "c（子代理）" in dispatch(host, "/agents").text
    assert "1 個代理" in dispatch(host, "/agents stop").text and child.stop_requested


def test_web_lists_agents_stops_them_with_the_conversation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_role(tmp_path / ".xharness" / "agents", "analyst", "---\ndescription: 查指標\n---\nx")
    gate = threading.Event()
    stopped = []

    def child(messages, tools):
        gate.wait(5)
        return AssistantTurn(content="child done")
    llm = Router([("child agent named", child),
                  ("", lambda m, t: AssistantTurn(content="", tool_calls=[call("subagent", {"task": "dig"})])
                   if not any(x["role"] == "tool" for x in m) else AssistantTurn(content="all done"))])

    def factory(resume, user=None):
        h = Harness()
        h.use("tools", tools_plugin)
        h.use("telemetry", telemetry_plugin())
        h.use("roles", roles_plugin({}, site_dir=str(tmp_path)))
        h.use("subagent", subagent_plugin())
        h.use("discussion", discussion_plugin())
        h.ctx.provide("llm", llm)
        return h

    app = WebApp(ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake"),
                 approval_mode="auto", harness_factory=factory)
    assert [r["name"] for r in app.roles(None)] == ["analyst"]
    conv = app.create()
    app.send(conv, "go")
    deadline = time.time() + 5
    while time.time() < deadline and not conv.summary()["agents"]:
        time.sleep(0.02)
    live = conv.summary()["agents"]
    assert live and live[0]["status"] == "running"
    assert any(e["type"] == "agents" for e in conv.events)
    assert app.stop(conv)
    stopped.append(conv.harness.ctx.get("agents").public()[0]["activity"])
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and conv.running:
        time.sleep(0.02)
    assert conv.summary()["agents"][0]["status"] == "stopped"
    assert app.agents_action(conv, {"all": True}) == 0     # nothing left running


# --- figures are checked, not trusted ---------------------------------------------------------

def test_unsourced_figures_are_found():
    from xharness.discuss import _values, unsourced

    sources = _values("學年度,註冊率\n110,95.0%\n111,90.0%\n114,73.33%")
    assert unsourced("註冊率從 95% 掉到 73.3%，第 2 年是 84%，共 3 年", sources) == ["84"]
    assert unsourced("1,200 人", _values("1200")) == []
    assert _values("110,95.0%") == {110.0, 95.0} and {110950.0, 110.0, 950.0} <= _values("110,950")
    assert unsourced("第1輪 第2年", set()) == []          # ordinals are not figures


def test_a_speaker_quoting_figures_unchecked_is_sent_to_look_them_up(tmp_path):
    def analyst(messages, tools):
        last = str(messages[-1].get("content"))
        if messages[-1]["role"] == "tool":
            return "查過了：111 學年註冊率 90.0%。"
        if "主持人提醒" in last:
            return AssistantTurn(content="", tool_calls=[call("read", {})])
        return "第二年註冊率是 84%。"
    llm = Router([("You are 分析,", analyst), ("You are 質疑,", "我想看 95% 的出處"),
                  ("closing summary", "共識：註冊率 90.0%；另有人說 42 人")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    h.ctx.get("tools").register(Tool("read", "read", {"type": "object", "properties": {}},
                                     lambda a, c: ToolResult("學年度,註冊率\n110,95.0%\n111,90.0%")))
    out = run_room(h, tmp_path, topic="註冊率", participants=["分析", "質疑"], rounds=1)
    first, second = log.of("discussion-turn")
    assert first["rechecked"] and first["unverified"] == [] and "90.0%" in first["text"] and first["tools"] == ["read"]
    assert any("84" in n["text"] for n in log.of("discussion-note"))
    # 95% became a source once the analyst read the file, so the sceptic's quote stands.
    assert second["unverified"] == [] and not second["rechecked"]
    assert "主持人註記：結論中的 42" in log.of("discussion-end")[0]["summary"]
    assert "主持人註記：結論中的 42" in out.output


def test_a_speaker_without_tools_is_marked_not_retried(tmp_path):
    llm = Router([("You are 甲,", "畢業 42-48 人"), ("You are 乙,", "同意"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    out = run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=1)
    first = log.of("discussion-turn")[0]
    assert first["unverified"] == ["42", "48"] and not first["rechecked"]
    assert "主持人註記：42、48 在查到的資料裡找不到原文" in out.output
    assert "主持人註記" in llm.asked("You are 乙,")[0]["messages"][-1]["content"]   # the next speaker is warned
    assert "不可寫成共識" in llm.asked("closing summary")[0]["messages"][-1]["content"]


def test_invented_sources_are_found():
    from xharness.discuss import uncited

    corpus = "學年度,招生名額,報到人數\n110,60,57"
    files = {"招生數據.csv"}
    assert uncited("報到 57 人（資料來源：六年制電資系研討會）", corpus, files) == ["六年制電資系研討會"]
    assert uncited("出自表格「近年報到狀況」", corpus, files) == ["近年報到狀況"]
    assert uncited("（資料來源：背景資訊）（資料來源：招生數據.csv 的報到人數欄）出自「報到人數」欄", corpus, files) == []
    assert uncited("（資料來源：招生數據.csv、評鑑報告）", corpus, files) == ["評鑑報告"]


def test_an_invented_source_is_marked_and_passed_on(tmp_path):
    llm = Router([("You are 甲,", "報到 57 人（資料來源：六年制電資系研討會）"), ("You are 乙,", "同意"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="報到 57 人怎麼看", participants=["甲", "乙"], rounds=1)
    first = log.of("discussion-turn")[0]
    assert first["uncited"] == ["六年制電資系研討會"] and first["unverified"] == []
    assert "「六年制電資系研討會」不在任何查到的資料裡" in llm.asked("You are 乙,")[0]["messages"][-1]["content"]
    assert "never name a report" in llm.asked("You are 甲,")[0]["system"]


def test_reopening_a_live_session_returns_the_live_conversation(tmp_path, monkeypatch):
    from xharness.session import session_plugin

    monkeypatch.chdir(tmp_path)

    def factory(resume, user=None):
        h = Harness()
        h.use("tools", tools_plugin)
        h.use("session", session_plugin(resume, None))
        h.ctx.provide("llm", Router([("", "hi")]))
        return h

    app = WebApp(ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake"),
                 approval_mode="auto", harness_factory=factory)
    first = app.create()
    again = app.create(resume=first.session_id)
    assert again is first and len(app.conversations) == 1     # one log, one writer


def test_argument_errors_show_a_valid_call_and_names_can_be_a_string(tmp_path):
    llm = Router([("You are", "好"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    bad = run_room(h, tmp_path, participants=["a", "b"])
    assert bad.is_error and "Simplest valid call" in bad.output
    run_room(h, tmp_path, topic="t", participants="甲、乙", rounds=1)
    assert [t["speaker"] for t in log.of("discussion-turn")] == ["甲", "乙"]


def test_tool_schemas_use_single_types():
    """llama.cpp's Gemma chat template rejects a type list; every schema here must use one type."""
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("roles", roles_plugin({}))
    h.use("subagent", subagent_plugin())
    h.use("workflow", workflow_plugin())
    h.use("discussion", discussion_plugin())

    def walk(node):
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list), node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for tool in h.ctx.get("tools").list():
        walk(tool.parameters)


def test_a_stop_during_the_recheck_is_not_lost(tmp_path):
    def speaker(messages, tools):
        last = str(messages[-1].get("content"))
        if "主持人提醒" in last:
            # The person presses Stop while the speaker is re-checking.
            h.ctx.get("agents").stop_all()
            return "改正：沒有那個數字"
        return "大概是 83%"
    llm = Router([("You are 甲,", speaker), ("You are 乙,", "乙說"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    h.ctx.get("tools").register(echo_tool("read"))
    run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=3)
    assert log.of("discussion-end")[0]["reason"] == "stopped"
    assert [t["speaker"] for t in log.of("discussion-turn")] == ["甲"]     # nobody speaks after the stop
    assert not llm.asked("You are 乙,")


def test_book_title_citations_are_checked():
    from xharness.discuss import uncited

    assert uncited("（依據《本系開設夜間部評估方案》）", "", set()) == ["本系開設夜間部評估方案"]
    assert uncited("參閱『招生數據.csv』", "", {"招生數據.csv"}) == []


def test_a_stop_before_the_recheck_skips_it(tmp_path):
    def speaker(messages, tools):
        h.ctx.get("agents").stop_all()       # pressed while the first, unchecked answer is being written
        return "大概是 83%"
    llm = Router([("You are 甲,", speaker), ("You are 乙,", "乙說"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    h.ctx.get("tools").register(echo_tool("read"))
    run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=3)
    assert len(llm.asked("You are 甲,")) == 1          # no recheck run after the stop
    assert log.of("discussion-end")[0]["reason"] == "stopped" and not log.of("discussion-turn")


def test_the_home_page_offers_a_discussion_and_the_example_roles_parse():
    from xharness.web import INDEX_HTML

    assert "代理討論" in INDEX_HTML and "setSelectionRange" in INDEX_HTML      # fills the box, does not send
    here = os.path.join(os.path.dirname(__file__), "..", "examples", "agents")
    roles = RoleStore({"dirs": [here]}, site_dir=here).discover(None)
    assert {r.name for r in roles} == {"查資料員", "審查員", "正方", "反方"}
    assert all(r.description and r.prompt and r.tools for r in roles)


def test_usage_history_reads_every_persons_sessions(monkeypatch, tmp_path):
    from xharness.usage import history

    def write(directory, name, tokens):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                                     "type": "telemetry", "total_tokens": tokens, "calls": 1}) + "\n")
    write(sessions_dir(None), "shared.jsonl", 100)
    write(sessions_dir("jefflee"), "mine.jsonl", 900)
    assert history(since="1d")["totals"]["total_tokens"] == 1000                    # the fleet chart: everyone
    assert history(directory=sessions_dir("jefflee"), since="1d")["totals"]["total_tokens"] == 900


def test_the_fleet_page_updates_cards_in_place():
    from xharness.web import INDEX_HTML

    assert "function syncCards" in INDEX_HTML and "data-key" not in INDEX_HTML.split("function syncCards")[0][-200:]
    assert ".fc{background:var(--surface);border-radius:18px;padding:16px 18px;box-shadow:var(--shadow-sm);display:flex;flex-direction:column;gap:8px}" in INDEX_HTML
    assert "kp.textContent='';" not in INDEX_HTML.split("async function renderFleet")[1].split("function enter")[0].replace(
        "if(kp.children.length!==tiles.length){kp.textContent='';", "")     # tiles are not rebuilt on every poll


def test_role_names_may_be_chinese(tmp_path):
    assert parse_role("---\nname: 正方\n---\nx", "x", "", "").name == "正方"
    assert parse_role("---\nname: 審查員-2\n---\nx", "x", "", "").name == "審查員-2"
    for bad in ("a b", "../x", "_x", "x/y", ""):
        assert parse_role(f"---\nname: {bad}\n---\nx", "", "", "") is None, bad


def test_arrow_keys_in_the_command_menu_do_not_rebuild_it():
    from xharness.web import INDEX_HTML

    move = INDEX_HTML.split("function movePop(d)")[1].split("}")[0]
    assert "markPop()" in move and "updatePop" not in move and "showMentions" not in move
    assert "pop.scrollTop" in INDEX_HTML.split("function markPop()")[1].split("function movePop")[0]
    assert ".cmdpop .ci:hover" not in INDEX_HTML        # one highlight, moved by keys and mouse alike


def test_the_page_revalidates_and_offers_a_reload_after_an_upgrade():
    from xharness.web import INDEX_HTML

    assert "xHarness 已更新到" in INDEX_HTML and "location.reload()" in INDEX_HTML
    assert "popIndex=0;}" in INDEX_HTML.split("function closePop()")[1][:120]
    src = open(os.path.join(os.path.dirname(__file__), "..", "xharness", "web.py"), encoding="utf-8").read()
    assert 'self.send_header("Cache-Control", "no-cache")' in src.split("def _html")[1][:900]


# --- team against team ---------------------------------------------------------------------

def team_room(tmp_path, rules, log, **args):
    llm = Router(rules)
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    out = h.ctx.get("tools").get("discussion").execute(args, tc(tmp_path))
    return llm, out


def test_teams_talk_inside_then_speak_for_the_team(tmp_path):
    log = Log()
    def says(private, public):
        return lambda messages, tools: public if "public turn" in str(messages[-1]["content"]) else private
    llm, out = team_room(tmp_path, [
        ("You are 甲1,", says("甲組內：我們主張開班", "甲代表")), ("You are 甲2,", says("甲組內：先看師資", "甲組公開：支持開班")),
        ("You are 乙1,", says("乙組內：我們反對", "乙組公開：反對")), ("You are 乙2,", says("乙組內：成本太高", "乙代表")),
        ("closing summary", "各組最後的立場：甲組支持、乙組反對")], log,
        topic="要不要開夜間班", rounds=2,
        teams=[{"name": "甲組", "stance": "支持", "members": ["甲1", "甲2"], "spokesperson": "甲2"},
               {"name": "乙組", "members": [{"name": "乙1"}, {"name": "乙2"}]}])
    assert not out.is_error, out.output
    turns = [(t["round"], t["team"], t["scope"], t["speaker"]) for t in log.of("discussion-turn")]
    # Round 1: each team's members talk inside (spokesperson last), then each spokesperson speaks publicly.
    assert turns[:6] == [(1, "甲組", "caucus", "甲1"), (1, "甲組", "caucus", "甲2"),
                         (1, "乙組", "caucus", "乙2"), (1, "乙組", "caucus", "乙1"),
                         (1, "甲組", "plenary", "甲2"), (1, "乙組", "plenary", "乙1")]
    assert len(turns) == 12 and log.of("discussion-end")[0]["reason"] == "rounds"
    # A team's internal talk never reaches the other team; public statements reach everyone.
    for call in llm.asked("You are 乙"):
        heard = " ".join(str(m.get("content")) for m in call["messages"][1:] if m["role"] == "user")
        assert "甲組內" not in heard
    second_round_b = [c for c in llm.asked("You are 乙1,")][-1]["messages"]
    assert any("甲組公開：支持開班" in str(m.get("content")) for m in second_round_b if m["role"] == "user")
    assert "You are your team's spokesperson" in llm.asked("You are 甲2,")[0]["system"]
    assert "You are your team's spokesperson" not in llm.asked("You are 甲1,")[0]["system"]
    assert "Your team's position: 支持" in llm.asked("You are 甲1,")[0]["system"]
    assert "各組最後的立場" in llm.asked("closing summary")[0]["messages"][-1]["content"]
    start = log.of("discussion-start")[0]
    assert start["mode"] == "teams" and {p["team"] for p in start["participants"]} == {"甲組", "乙組"}


def test_a_one_member_team_only_speaks_in_public(tmp_path):
    log = Log()
    team_room(tmp_path, [("You are", "說"), ("closing summary", "x")], log, topic="t", rounds=1,
              teams=[{"name": "A", "members": ["a1", "a2"]}, {"name": "B", "members": "b1"}])
    scopes = [(t["team"], t["scope"]) for t in log.of("discussion-turn")]
    assert scopes == [("A", "caucus"), ("A", "caucus"), ("A", "plenary"), ("B", "plenary")]


def test_bad_team_lists_are_refused_with_an_example(tmp_path):
    h = build(Router([]), tmp_path, ("discussion", discussion_plugin()))
    tool = h.ctx.get("tools").get("discussion")
    for teams, why in [([{"name": "A", "members": ["x"]}], "at least two teams"),
                       ([{"name": "A", "members": ["x"]}, {"name": "A", "members": ["y"]}], "repeated"),
                       ([{"name": "A", "members": ["x"]}, {"name": "B", "members": ["x"]}], "two teams"),
                       ([{"name": "A", "members": []}, {"name": "B", "members": ["y"]}], "no members"),
                       ([{"name": "A", "members": ["x"], "spokesperson": "z"}, {"name": "B", "members": ["y"]}], "not a member")]:
        out = tool.execute({"topic": "t", "mode": "teams", "teams": teams}, tc(tmp_path))
        assert out.is_error and why in out.output and "Simplest valid team call" in out.output, (why, out.output)
    out = tool.execute({"topic": "t", "teams": [{"name": "A", "members": ["x"]}, {"name": "B", "members": ["y"]}],
                        "rounds": 1}, tc(tmp_path))
    assert not out.is_error                       # teams given without a mode means teams mode


def test_a_real_file_in_book_title_marks_is_not_flagged():
    from xharness.discuss import uncited

    assert uncited("（資料來源：《招生數據.csv》）", "資料來源：招生數據.csv", set()) == []


# --- lessons from staffKM's knowledge-base rooms (2026-10-24) -------------------------

def test_a_participants_brief_is_not_evidence(tmp_path):
    llm = Router([("You are 甲,", "報到 57 人（出處：招生簡報）"), ("You are 乙,", "同意"), ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    run_room(h, tmp_path, topic="招生怎麼看", rounds=1,
             participants=[{"name": "甲", "brief": "你知道去年報到 57 人，資料在招生簡報"}, {"name": "乙"}])
    first = log.of("discussion-turn")[0]
    assert first["unverified"] == ["57"] and first["uncited"] == ["招生簡報"]


def test_consensus_that_only_one_participant_said_is_marked():
    from xharness.discuss import tag_one_sided

    transcript = [{"speaker": "甲", "text": "報到率 87% 很穩定"}, {"speaker": "乙", "text": "我同意要調整名額"},
                  {"speaker": "丙", "text": "名額要調整，但 87% 我沒看到"}]
    summary = "1. 共識：\n- 報到率 87%\n- 名額要調整\n2. 分歧：\n- 甲說 120 人"
    out = tag_one_sided(summary, transcript)
    assert "報到率 87%" in out and "此點只出現在" not in out.split("\n")[1]        # 甲 and 丙 both said 87
    one = tag_one_sided("共識：\n- 每年 120 人報到\n分歧：\n- 甲說 120 人", [{"speaker": "甲", "text": "每年 120 人"},
                                                                     {"speaker": "乙", "text": "同意"}])
    lines = one.split("\n")
    assert "只出現在 甲 的發言" in lines[1] and "只出現在" not in lines[3]          # only consensus lines are checked


def test_review_stops_when_the_draft_no_longer_changes_and_returns_the_real_draft(tmp_path):
    draft = "第一段：說明招生現況與三年趨勢。" * 4
    llm = Router([("You are 作者,", speaker(draft, "感謝建議，無需修訂。")), ("You are 審查,", "第二段沒有出處"),
                  ("closing summary", "x")])
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin()), log=log)
    out = run_room(h, tmp_path, topic="寫說明", mode="review", participants=["作者", "審查"], rounds=6)
    assert log.of("discussion-end")[0]["reason"] == "unchanged"
    assert len(log.of("discussion-turn")) == 4                          # two rounds, not six
    assert "## 定稿\n" + draft in out.output                              # the thank-you is not the final draft
    assert "not finding it yourself is normal" in llm.asked("You are 審查,")[0]["system"]


def test_control_tokens_and_self_citations_are_cleaned():
    from xharness.discuss import clean_utterance

    assert clean_utterance("<|channel>model 我方認為應放假。（資料來源：無）") == "我方認為應放假。"
    assert clean_utterance("結論如下<|im_end|>") == "結論如下"
    assert clean_utterance("依推論如此（出處：作者）") == "依推論如此"
    assert clean_utterance("（資料來源：勞動部公告）") == "（資料來源：勞動部公告）"       # a real source stays


def test_the_closing_summary_keeps_a_reserve(tmp_path):
    llm = Router([("You are", "說話"), ("closing summary", "結論")], tokens=500)
    log = Log()
    h = build(llm, tmp_path, ("discussion", discussion_plugin({"max_tokens": 10_000})), log=log)
    run_room(h, tmp_path, topic="t", participants=["甲", "乙"], rounds=20)
    end = log.of("discussion-end")[0]
    assert end["reason"] == "budget" and end["summary"] == "結論"
    assert end["turns"] == 9            # participants stop at 9,000 (10,000 less a tenth), 1,000 a turn
