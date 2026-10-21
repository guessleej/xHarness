"""1.10: skills, human commands, compaction, model switching, titles,
search, export and ratings."""

import copy
import json
import os
import time

import pytest

from xharness.agent import SUMMARY_ACK, SUMMARY_PREFIX, Agent, AgentOptions
from xharness.commands import COMMANDS, CommandHost, dispatch, providers_for
from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn, OpenAIAdapter, Usage
from xharness.session import (SessionLog, export_markdown, messages_from_events, search_sessions,
                              session_summary, sessions_dir)
from xharness.skills import SkillStore, parse_front_matter, skills_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import ToolContext, tools_plugin
from xharness.web import WebApp

PROVIDERS = {
    "granite": {"base_url": "http://192.168.10.70:8093/v1", "model": "granite-4.2-30b", "api_key": "secret-key-1"},
    "xvita": {"base_url": "http://192.168.10.70:8091/v1", "model": "gemma-4-31b-xvita-zhtw"},
    "cloud": {"base_url": "https://api.example.com/v1", "model": "big", "api_key": "secret-key-2"},
}
CONFIG = ResolvedConfig(provider=PROVIDERS["granite"], provider_name="granite", providers=PROVIDERS)


class Scripted:
    def __init__(self, turns, model="fake"):
        self.model = model
        self.base_url = "http://127.0.0.1:1/v1"
        self.context_tokens = None
        self.turns = list(turns)
        self.seen = []

    def stream(self, messages, tools, on_delta=None):
        self.seen.append(copy.deepcopy(messages))
        if not self.turns:
            return AssistantTurn(content="(done)")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class Log:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def harness_with(adapter, log=None, skills=None):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("telemetry", telemetry_plugin())
    if skills is not None:
        harness.use("skills", skills)
    harness.ctx.provide("llm", adapter)
    if log is not None:
        harness.ctx.provide("session", log)
    return harness


# --- skills --------------------------------------------------------------------------

def write_skill(root, name, description="does a thing", body="Step one.\n", extra=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8")
    for rel, text in (extra or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def test_front_matter_handles_folded_descriptions():
    fields, body = parse_front_matter("---\nname: a\ndescription: >\n  first line\n  second line\n---\nBody\n")
    assert fields == {"name": "a", "description": "first line second line"} and body == "Body\n"
    assert parse_front_matter("no front matter")[0] == {}


def test_skills_are_found_in_priority_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    work, site = tmp_path / "work", tmp_path / "site"
    write_skill(work / ".xharness" / "skills", "report", "mine")
    write_skill(site / ".xharness" / "skills", "report", "site version")
    write_skill(site / ".xharness" / "skills", "deck", "site deck")
    write_skill(tmp_path / "home" / "skills", "notes", "machine-wide")
    store = SkillStore({}, site_dir=str(site))
    found = {s.name: s for s in store.discover(str(work))}
    assert set(found) == {"report", "deck", "notes"}
    assert found["report"].description == "mine"          # the person's own wins
    assert found["deck"].source == "站台" and found["notes"].source == "本機"


def test_home_claude_skills_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "private", "personal")
    assert not SkillStore({}, site_dir=str(tmp_path / "s")).discover(str(tmp_path / "w"))
    assert SkillStore({"claude_home": True}, site_dir=str(tmp_path / "s")).discover(str(tmp_path / "w"))


def test_index_goes_into_the_prompt_and_the_body_through_the_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    write_skill(tmp_path / ".xharness" / "skills", "brand", "use the brand colours",
                body="Use #bf181f.\n", extra={"refs/palette.md": "red"})
    adapter = Scripted([AssistantTurn(content="ok")])
    harness = harness_with(adapter, skills=skills_plugin({}, site_dir=str(tmp_path)))
    agent = Agent(harness.ctx, AgentOptions(cwd=str(tmp_path)))
    system = agent.messages[0]["content"]
    assert "## Skills" in system and "- brand: use the brand colours" in system and "#bf181f" not in system
    tool = harness.ctx.get("tools").get("skill")
    result = tool.execute({"name": "brand"}, ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    assert "Use #bf181f." in result.output and "refs/palette.md" in result.output
    missing = tool.execute({"name": "nope"}, ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    assert missing.is_error and "brand" in missing.output


def test_children_without_the_skill_tool_get_no_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    write_skill(tmp_path / ".xharness" / "skills", "brand")
    harness = harness_with(Scripted([]), skills=skills_plugin({}, site_dir=str(tmp_path)))
    agent = Agent(harness.ctx, AgentOptions(cwd=str(tmp_path), exclude_tools={"skill"}))
    assert "## Skills" not in agent.messages[0]["content"]


# --- compaction ------------------------------------------------------------------------

def long_conversation(agent):
    agent.messages += [
        {"role": "user", "content": "first task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read", "arguments": "{}"}]},
        {"role": "tool", "content": "file text", "tool_call_id": "c1"},
        {"role": "assistant", "content": "done first"},
        {"role": "user", "content": "second task"},
    ]


def test_compact_replaces_history_before_the_latest_request():
    log = Log()
    adapter = Scripted([AssistantTurn(content="Goal: two tasks. First done.")])
    agent = Agent(harness_with(adapter, log).ctx, AgentOptions())
    long_conversation(agent)
    result = agent.compact()
    assert result["replaced"] == 4 and result["kept"] == 1
    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant", "user"]
    assert agent.messages[1]["content"].startswith(SUMMARY_PREFIX) and agent.messages[2]["content"] == SUMMARY_ACK
    assert agent.messages[3]["content"] == "second task"
    # the summariser saw the earlier conversation, not the system prompt or the tools
    request = adapter.seen[0]
    assert "first task" in request[1]["content"] and "second task" not in request[1]["content"]
    assert any(e["type"] == "compaction" for e in log.events)


def test_nothing_to_compact_returns_none():
    agent = Agent(harness_with(Scripted([])).ctx, AgentOptions())
    agent.messages.append({"role": "user", "content": "only one"})
    assert agent.compact() is None


def test_resume_rebuilds_the_compacted_history():
    events = [
        {"type": "message", "role": "user", "content": "first task"},
        {"type": "message", "role": "assistant", "content": "done first"},
        {"type": "message", "role": "user", "content": "second task"},
        {"type": "compaction", "summary": "S", "kept": 1, "replaced": 2},
        {"type": "message", "role": "assistant", "content": "done second"},
    ]
    rebuilt = messages_from_events(events)
    assert [m["content"] for m in rebuilt] == [SUMMARY_PREFIX + "S", SUMMARY_ACK, "second task", "done second"]


def test_auto_compaction_runs_before_the_next_call_once_the_window_fills():
    log = Log()
    big = Usage(prompt_tokens=900, completion_tokens=10)
    adapter = Scripted([
        AssistantTurn(content="", tool_calls=[{"id": "t", "name": "nothing", "arguments": "{}"}], usage=big),
        AssistantTurn(content="summary of before"),   # the compaction call
        AssistantTurn(content="finished"),
    ])
    adapter.context_tokens = 1000
    agent = Agent(harness_with(adapter, log).ctx, AgentOptions())
    agent.messages += [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}]
    assert agent.run("new task") == "finished"
    assert any(e["type"] == "compaction" and e["reason"] == "auto" for e in log.events)
    assert adapter.seen[2][1]["content"] == SUMMARY_PREFIX + "summary of before"


def test_overflow_is_answered_by_compaction_first():
    log = Log()
    overflow = RuntimeError("LLM request failed: 400 the request exceeds the available context size")
    adapter = Scripted([overflow, AssistantTurn(content="summary"), AssistantTurn(content="fits")])
    agent = Agent(harness_with(adapter, log).ctx, AgentOptions())
    agent.messages += [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}]
    assert agent.run("new") == "fits"
    assert [e["reason"] for e in log.events if e["type"] == "compaction"] == ["overflow"]


# --- commands --------------------------------------------------------------------------

def host_for(agent, harness, **kwargs):
    return CommandHost(agent=agent, ctx=harness.ctx, config=CONFIG, **kwargs)


def test_unknown_or_plain_text_is_not_a_command():
    harness = harness_with(Scripted([]))
    host = host_for(Agent(harness.ctx, AgentOptions()), harness)
    assert dispatch(host, "hello") is None and dispatch(host, "/etc/hosts 是什麼") is None
    assert "/compact" in dispatch(host, "/help").text


def test_web_only_commands_stay_off_the_terminal():
    harness = harness_with(Scripted([]))
    host = host_for(Agent(harness.ctx, AgentOptions()), harness)
    refused = dispatch(host, "/stop", surface="cli")
    assert refused is not None and not refused.ok        # answered, never sent to the model
    assert "/stop" not in dispatch(host, "/help", surface="cli").text
    assert dispatch(host, "/stop", surface="web").action == "stop"


def test_unknown_bare_commands_are_answered_not_sent():
    harness = harness_with(Scripted([]))
    host = host_for(Agent(harness.ctx, AgentOptions()), harness)
    assert not dispatch(host, "/frobnicate").ok
    assert dispatch(host, "/etc/hosts 是什麼") is None and dispatch(host, "/usr/bin") is None


def test_compact_and_model_are_refused_while_running():
    harness = harness_with(Scripted([]))
    host = host_for(Agent(harness.ctx, AgentOptions()), harness, running=True)
    assert not dispatch(host, "/compact").ok and not dispatch(host, "/model xvita").ok


def test_model_switch_replaces_the_adapter_and_the_identity_line():
    log = Log()
    harness = harness_with(Scripted([]), log)
    agent = Agent(harness.ctx, AgentOptions())
    result = dispatch(host_for(agent, harness), "/model xvita")
    assert result.ok and result.data == {"provider": "xvita", "model": "gemma-4-31b-xvita-zhtw"}
    llm = harness.ctx.get("llm")
    assert isinstance(llm, OpenAIAdapter) and llm.model == "gemma-4-31b-xvita-zhtw"
    assert "gemma-4-31b-xvita-zhtw" in agent.messages[0]["content"]
    switch = next(e for e in log.events if e["type"] == "model-switch")
    assert switch["hosting"] == "self-hosted" and "api_key" not in json.dumps(switch)


def test_model_switch_respects_the_tier_and_the_switch():
    harness = harness_with(Scripted([]))
    agent = Agent(harness.ctx, AgentOptions())
    assert not dispatch(host_for(agent, harness, allowed_providers=("granite",)), "/model cloud").ok
    assert not dispatch(host_for(agent, harness, model_switch=False), "/model xvita").ok


def test_provider_listing_never_carries_keys():
    rows = providers_for(CONFIG)
    assert {r["name"] for r in rows} == {"granite", "xvita", "cloud"}
    assert "secret-key" not in json.dumps(rows)
    assert {r["name"]: r["hosting"] for r in rows}["cloud"] == "external"


def test_title_and_feedback_commands():
    harness = harness_with(Scripted([]))
    seen = {}
    host = host_for(Agent(harness.ctx, AgentOptions()), harness,
                    set_title=lambda t: seen.update(title=t), get_title=lambda: seen.get("title", ""),
                    rate=lambda rating, note: seen.update(rating=(rating, note)) or 2)
    assert dispatch(host, "/title 「整理校慶通知」").ok and seen["title"] == "整理校慶通知"
    assert dispatch(host, "/feedback down 日期算錯").ok and seen["rating"] == ("down", "日期算錯")
    assert not dispatch(host, "/feedback maybe").ok


def test_every_command_has_help_text():
    assert all(c.help for c in COMMANDS.values())


# --- sessions: titles, search, export ------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    os.makedirs(sessions_dir(None), exist_ok=True)
    return tmp_path


def write_session(session_id, events):
    with open(os.path.join(sessions_dir(None), f"{session_id}.jsonl"), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps({"ts": "2026-10-21T01:00:00+00:00", **event}, ensure_ascii=False) + "\n")


def test_summary_search_and_export(home):
    write_session("a1", [
        {"type": "meta", "model": "granite"},
        {"type": "message", "role": "user", "content": "把校慶通知的日期改成下週五"},
        {"type": "message", "role": "assistant", "content": "", "tool_calls": [{"name": "edit", "arguments": "{}"}]},
        {"type": "message", "role": "assistant", "content": "改好了"},
        {"type": "title", "title": "校慶通知改日期"},
    ])
    write_session("b2", [{"type": "message", "role": "user", "content": "跑資安掃描"}])
    assert session_summary("a1", None) == {"title": "校慶通知改日期", "preview": "把校慶通知的日期改成下週五"}
    hits = search_sessions("校慶", None)
    assert [h["id"] for h in hits] == ["a1"] and "校慶" in hits[0]["snippet"]
    assert search_sessions("", None) == []
    text = export_markdown("a1", None)
    assert text.startswith("# 校慶通知改日期") and "## 提問" in text and "工具 `edit`" in text and "改好了" in text
    assert export_markdown("../etc/passwd", None) is None and export_markdown("missing", None) is None


# --- usage report: per-task model and ratings ----------------------------------------

def test_report_charges_each_task_to_its_own_model_and_counts_ratings(home):
    from xharness.report import collect

    write_session("s1", [
        {"type": "meta", "model": "granite", "hosting": "self-hosted"},
        {"type": "telemetry", "total_tokens": 100, "calls": 1, "model": "granite", "hosting": "self-hosted"},
        {"type": "telemetry", "total_tokens": 300, "calls": 2, "model": "big", "hosting": "external"},
        {"type": "feedback", "turn": 1, "rating": "down", "note": "太慢"},
        {"type": "feedback", "turn": 1, "rating": "up", "note": ""},
        {"type": "feedback", "turn": 2, "rating": "down", "note": "算錯"},
    ])
    os.utime(os.path.join(sessions_dir(None), "s1.jsonl"))
    report = collect(CONFIG, since="30d", now=__import__("datetime").datetime(2026, 10, 22, tzinfo=__import__("datetime").timezone.utc))
    assert report["hosting"]["self_hosted"]["tokens"] == 100
    assert report["hosting"]["external"]["tokens"] == 200
    assert dict(report["spend"]["models"]) == {"granite": 1, "big": 1}
    feedback = report["health"]["feedback"]
    assert (feedback["up"], feedback["down"]) == (1, 1)       # turn 1's later rating replaced the first
    assert [n["note"] for n in feedback["notes"]] == ["算錯"]


def test_telemetry_tags_each_task_with_its_model():
    log = Log()
    adapter = Scripted([AssistantTurn(content="hi", usage=Usage(5, 5))], model="granite-4.2-30b")
    adapter.base_url = "http://192.168.10.70:8093/v1"
    Agent(harness_with(adapter, log).ctx, AgentOptions()).run("x")
    record = next(e for e in log.events if e["type"] == "telemetry")
    assert record["model"] == "granite-4.2-30b" and record["hosting"] == "self-hosted"


# --- web ------------------------------------------------------------------------------

def make_app(turns, providers=True):
    def factory(resume, user=None):
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.use("telemetry", telemetry_plugin())
        harness.ctx.provide("llm", Scripted(turns))
        return harness

    config = CONFIG if providers else ResolvedConfig(provider=PROVIDERS["granite"], provider_name="granite")
    return WebApp(config, approval_mode="auto", harness_factory=factory)


def wait_done(conv, count=1):
    deadline = time.time() + 5
    while time.time() < deadline:
        if sum(e["type"] in ("done", "error") for e in conv.events) >= count and not conv.running:
            return
        time.sleep(0.02)


def test_web_titles_a_conversation_after_its_first_task():
    app = make_app([AssistantTurn(content="答案"), AssistantTurn(content="「校慶日期」")])
    conv = app.create()
    app.send(conv, "把校慶通知日期改掉")
    deadline = time.time() + 5
    while time.time() < deadline and not conv.title:
        time.sleep(0.02)
    assert conv.title == "校慶日期" and conv.summary()["title"] == "校慶日期"
    assert any(e["type"] == "title" for e in conv.events)


def test_web_command_and_model_switch():
    app = make_app([])
    conv = app.create()
    result = app.command(conv, "/model xvita", None)
    assert result.ok and conv.provider == "xvita"
    assert conv.summary()["model"] == "gemma-4-31b-xvita-zhtw"
    assert any(e["type"] == "model" for e in conv.events)
    assert app.command(conv, "just text", None) is None
    assert "secret-key" not in json.dumps(app.models(None))


def test_new_conversation_can_start_on_another_model_and_bad_names_fail():
    app = make_app([])
    assert app.create(provider="xvita").summary()["model"] == "gemma-4-31b-xvita-zhtw"
    with pytest.raises(ValueError):
        app.create(provider="nope")


def test_web_rating_is_recorded_and_bounded():
    app = make_app([AssistantTurn(content="一"), AssistantTurn(content="t")])
    conv = app.create()
    assert app.rate(conv, None, "up") is None          # nothing answered yet
    app.send(conv, "問題")
    wait_done(conv)
    assert app.rate(conv, 1, "down", "x" * 900) == 1
    assert conv.summary()["ratings"] == {"1": "down"}
    assert app.rate(conv, 5, "up") is None             # no such reply
    assert app.rate(conv, 1, None) == 1 and conv.summary()["ratings"] == {}


def test_export_shows_taipei_time(home):
    write_session("t1", [{"type": "message", "role": "user", "content": "x"}])
    assert "- 開始：2026-10-21 09:00（台北時間）" in export_markdown("t1", None)
