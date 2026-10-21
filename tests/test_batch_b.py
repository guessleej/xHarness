"""1.11: background jobs, the persistent terminal, plan mode, goal mode,
@-mentions and images."""

import base64
import copy
import json
import os
import time

import pytest

from xharness.agent import PLAN_CONTEXT, Agent, AgentOptions
from xharness.commands import CommandHost, dispatch
from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.goal import goal_plugin, run_goal
from xharness.llm import AssistantTurn, OpenAIAdapter
from xharness.mentions import expand, list_files
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolContext, ToolResult, tools_plugin
from xharness.tools.jobs import JobManager, jobs_plugin
from xharness.tools.terminal import terminal_plugin
from xharness.vision import MAX_IMAGES, data_uri, describer_from_config, save_images
from xharness.web import WebApp

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Scripted:
    def __init__(self, turns, vision=False):
        self.model = "fake"
        self.base_url = "http://127.0.0.1:1/v1"
        self.context_tokens = None
        self.vision = vision
        self.turns = list(turns)
        self.seen = []

    def stream(self, messages, tools, on_delta=None):
        self.seen.append({"messages": copy.deepcopy(messages), "tools": [t["name"] for t in tools]})
        return self.turns.pop(0) if self.turns else AssistantTurn(content="(done)")


class Log:
    def __init__(self):
        self.events = []

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


def ctx_for(cwd, approve=True, agent="main"):
    return ToolContext(cwd=str(cwd), approve=lambda _s: approve, agent=agent)


# --- background jobs ---------------------------------------------------------------

def test_job_runs_in_the_background_and_reports(tmp_path):
    manager = JobManager()
    job = manager.start("echo start; sleep 0.3; echo finished", str(tmp_path))
    assert job.state == "running"
    manager.wait(job.id, 5)
    assert job.state == "done" and job.exit_code == 0
    assert "finished" in manager.tail(job)
    assert (tmp_path / ".xharness" / ".gitignore").read_text() == "*\n"


def test_failing_job_and_limits(tmp_path):
    manager = JobManager(max_running=1, max_seconds=0.5)
    slow = manager.start("sleep 5", str(tmp_path))
    with pytest.raises(RuntimeError):
        manager.start("echo too many", str(tmp_path))
    manager.wait(slow.id, 5)
    assert slow.state == "timeout"
    bad = manager.start("exit 3", str(tmp_path))
    manager.wait(bad.id, 5)
    assert bad.state == "failed" and bad.exit_code == 3


def test_stop_and_shutdown_kill_the_process_group(tmp_path):
    manager = JobManager()
    job = manager.start("sleep 30 & sleep 30; wait", str(tmp_path))
    assert manager.stop(job.id).state == "stopped"
    other = manager.start("sleep 30", str(tmp_path))
    manager.shutdown()
    assert other.state == "stopped"
    other.finished.wait(5)
    assert other.process.poll() is not None


def test_job_tools_need_approval_and_finished_jobs_are_announced(tmp_path):
    adapter = Scripted([AssistantTurn(content="", tool_calls=[call("job_start", {"command": "echo hi"})]),
                        AssistantTurn(content="started"), AssistantTurn(content="next")])
    h = harness(adapter, ("jobs", jobs_plugin()))
    tool = h.ctx.get("tools").get("job_start")
    assert tool.mutating
    assert tool.execute({"command": "echo x"}, ctx_for(tmp_path, approve=False)).output == "denied by approval policy"
    agent = Agent(h.ctx, AgentOptions(cwd=str(tmp_path)))
    agent.run("start a job")
    manager = h.ctx.get("jobs")
    manager.wait("j1", 5)
    agent.run("anything new?")
    sent = adapter.seen[-1]["messages"][-1]["content"]
    assert "Background job j1 (echo hi) ended: done" in sent
    agent.run("again")
    assert "Background job" not in adapter.seen[-1]["messages"][-1]["content"]   # announced once
    h.dispose()


# --- terminal -------------------------------------------------------------------------

def test_terminal_keeps_directory_and_variables(tmp_path):
    (tmp_path / "sub").mkdir()
    h = harness(Scripted([]), ("terminal", terminal_plugin()))
    tool = h.ctx.get("tools").get("terminal")
    ctx = ctx_for(tmp_path)
    assert "sub" in tool.execute({"command": "cd sub && export X=7"}, ctx).output
    result = tool.execute({"command": "echo $X; pwd"}, ctx)
    assert result.output.startswith("7\n") and result.output.rstrip().endswith("/sub)")
    assert tool.execute({"command": "false"}, ctx).is_error
    assert "fresh shell" in tool.execute({"reset": True}, ctx).output
    assert tool.execute({"command": "echo ${X:-unset}"}, ctx).output.startswith("unset")
    h.dispose()


def test_terminal_times_out_and_restarts(tmp_path):
    h = harness(Scripted([]), ("terminal", terminal_plugin()))
    tool = h.ctx.get("tools").get("terminal")
    result = tool.execute({"command": "sleep 20", "timeout_seconds": 1}, ctx_for(tmp_path))
    assert result.is_error and "restarted" in result.output
    assert tool.execute({"command": "echo back"}, ctx_for(tmp_path)).output.startswith("back")
    assert tool.execute({"command": "echo x"}, ctx_for(tmp_path, approve=False)).is_error
    h.dispose()


def test_each_agent_has_its_own_shell(tmp_path):
    h = harness(Scripted([]), ("terminal", terminal_plugin()))
    tool = h.ctx.get("tools").get("terminal")
    tool.execute({"command": "export WHO=main"}, ctx_for(tmp_path, agent="main"))
    assert tool.execute({"command": "echo ${WHO:-none}"}, ctx_for(tmp_path, agent="child-1")).output.startswith("none")
    h.dispose()


# --- plan mode ------------------------------------------------------------------------

def mutating_tool(seen):
    return Tool("write_it", "changes things", {"type": "object", "properties": {}},
                lambda args, ctx: seen.append("wrote") or ToolResult("written"), mutating=True)


def test_plan_mode_hides_change_tools_and_says_so():
    seen = []
    adapter = Scripted([AssistantTurn(content="", tool_calls=[call("write_it", {})]), AssistantTurn(content="1. plan")])
    h = harness(adapter, log=Log())
    h.ctx.get("tools").register(mutating_tool(seen))
    agent = Agent(h.ctx, AgentOptions())
    agent.plan_mode = True
    agent.run("fix it")
    assert "write_it" not in adapter.seen[0]["tools"] and "subagent" not in adapter.seen[0]["tools"]
    assert PLAN_CONTEXT in adapter.seen[0]["messages"][-1]["content"]
    assert seen == []        # a call the model makes anyway is refused
    tool_result = [m for m in adapter.seen[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "plan mode is on" in tool_result
    agent.plan_mode = False
    agent.run("go")
    assert "write_it" in adapter.seen[-1]["tools"]


def test_plan_command_toggles_and_logs():
    log = Log()
    h = harness(Scripted([]), log=log)
    agent = Agent(h.ctx, AgentOptions())
    host = CommandHost(agent=agent, ctx=h.ctx, config=None)
    assert dispatch(host, "/plan").data == {"plan_mode": True} and agent.plan_mode
    assert dispatch(host, "/plan off").data == {"plan_mode": False} and not agent.plan_mode
    assert not dispatch(host, "/plan maybe").ok
    assert [e["on"] for e in log.events if e["type"] == "plan-mode"] == [True, False]


# --- goal mode ------------------------------------------------------------------------

def test_goal_runs_rounds_until_goal_done():
    log = Log()
    adapter = Scripted([
        AssistantTurn(content="started, more to do"),
        AssistantTurn(content="", tool_calls=[call("goal_done", {"status": "done", "summary": "all tests pass"})]),
        AssistantTurn(content="finished"),
    ])
    h = harness(adapter, ("goal", goal_plugin()), log=log)
    agent = Agent(h.ctx, AgentOptions())
    events = []
    assert run_goal(agent, "make tests pass", 5, on_event=events.append) == "finished"
    state = h.ctx.get("goal")
    assert (state.status, state.round, state.summary) == ("done", 2, "all tests pass")
    assert "goal_done" in adapter.seen[0]["tools"]
    assert adapter.seen[0]["messages"][-1]["content"].startswith("Goal: make tests pass")
    assert "round 2 of 5" in adapter.seen[1]["messages"][-1]["content"]
    assert [e["state"] for e in events] == ["started", "round", "round", "done"]
    assert any(e["type"] == "goal-end" and e["status"] == "done" for e in log.events)
    agent.run("unrelated")
    assert "goal_done" not in adapter.seen[-1]["tools"]   # only offered while a goal is active


def test_goal_stops_at_the_round_limit_or_when_blocked():
    h = harness(Scripted([AssistantTurn(content="x")] * 5), ("goal", goal_plugin()))
    agent = Agent(h.ctx, AgentOptions())
    run_goal(agent, "never ends", 3)
    assert h.ctx.get("goal").status == "incomplete" and h.ctx.get("goal").round == 3
    blocked = Scripted([AssistantTurn(content="", tool_calls=[call("goal_done", {"status": "blocked", "summary": "no access"})]),
                        AssistantTurn(content="sorry")])
    h2 = harness(blocked, ("goal", goal_plugin()))
    run_goal(Agent(h2.ctx, AgentOptions()), "needs access", 5)
    assert h2.ctx.get("goal").status == "blocked"


def test_goal_command_parses_rounds():
    h = harness(Scripted([]), ("goal", goal_plugin()))
    host = CommandHost(agent=Agent(h.ctx, AgentOptions()), ctx=h.ctx, config=None)
    result = dispatch(host, "/goal --rounds 8 讓所有測試通過")
    assert result.action == "goal" and result.data == {"objective": "讓所有測試通過", "rounds": 8}
    assert dispatch(host, "/goal 2026 年的報表").data == {"objective": "2026 年的報表", "rounds": 5}
    assert dispatch(host, "/goal 3 個檔案要改").data == {"objective": "3 個檔案要改", "rounds": 5}
    assert dispatch(host, "/goal --rounds 99 x").data["rounds"] == 20
    assert not dispatch(CommandHost(agent=host.agent, ctx=h.ctx, config=None, running=True), "/goal x").ok


# --- @ mentions -----------------------------------------------------------------------

def test_mentions_attach_text_files_inside_the_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2")
    (tmp_path / "shot.png").write_bytes(PNG)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    context, files, images = expand("fix @src/app.py and @blob.bin, see @shot.png, not @../secret.txt or @missing.md", str(tmp_path))
    assert files == ["src/app.py", "shot.png"]
    assert "### src/app.py" in context and "print('hi')" in context and "binary" in context
    assert "nope" not in context and images == [str((tmp_path / "shot.png").resolve())]
    assert expand("email me at a@b.com", str(tmp_path)) == (None, [], [])


def test_file_picker_ranks_name_matches(tmp_path):
    for rel in ("docs/readme.md", "readme.md", "node_modules/x/readme.md", ".git/readme.md", "src/other_readme.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    paths = [f["path"] for f in list_files(str(tmp_path), "readme")]
    assert paths[0] == "readme.md" and "node_modules/x/readme.md" not in paths and ".git/readme.md" not in paths


# --- images ---------------------------------------------------------------------------

def test_save_images_validates_and_dedupes(tmp_path):
    item = {"type": "image/png", "data": base64.b64encode(PNG).decode()}
    paths = save_images([item, item], str(tmp_path))
    assert len(set(paths)) == 1 and os.path.isfile(paths[0])
    for bad in ({"type": "image/svg+xml", "data": item["data"]}, {"type": "image/png", "data": "not base64!"}):
        with pytest.raises(ValueError):
            save_images([bad], str(tmp_path))
    with pytest.raises(ValueError):
        save_images([item] * (MAX_IMAGES + 1), str(tmp_path))


def test_vision_adapter_sends_image_parts_and_others_get_text(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    message = {"role": "user", "content": "what is this", "images": [str(path)]}
    seeing = OpenAIAdapter(base_url="http://x/v1", model="m", vision=True)._wire_messages([message])[0]["content"]
    assert seeing[0] == {"type": "text", "text": "what is this"} and seeing[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert OpenAIAdapter(base_url="http://x/v1", model="m")._wire_messages([message])[0]["content"] == "what is this"
    assert data_uri(str(tmp_path / "gone.png")) is None


def test_blind_model_is_told_or_gets_a_description(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    log = Log()
    adapter = Scripted([AssistantTurn(content="ok"), AssistantTurn(content="ok")])
    agent = Agent(harness(adapter, log=log).ctx, AgentOptions())
    agent.run("look", images=[str(path)])
    assert "cannot see images" in adapter.seen[0]["messages"][-1]["content"]
    described = Agent(harness(adapter).ctx, AgentOptions(describe_image=lambda p, req: "a red error dialog",
                                                         describer_name="ocr-model"))
    described.run("look", images=[str(path)])
    assert "a red error dialog" in adapter.seen[1]["messages"][-1]["content"]
    event = next(e for e in log.events if e.get("role") == "user")
    assert event["images"] == [str(path)]


def test_describer_comes_from_the_vision_section():
    providers = {"text": {"base_url": "http://x/v1", "model": "t"}, "ocr": {"base_url": "http://x/v1", "model": "eyes"}}
    config = ResolvedConfig(provider=providers["text"], provider_name="text", providers=providers, vision={"provider": "ocr"})
    name, _describe = describer_from_config(config)
    assert name == "eyes"
    assert describer_from_config(ResolvedConfig(provider=providers["text"], provider_name="text", providers=providers)) is None


# --- web ------------------------------------------------------------------------------

def web_app(turns, tmp_path):
    def factory(resume, user=None):
        h = Harness()
        h.use("tools", tools_plugin)
        h.use("telemetry", telemetry_plugin())
        h.use("goal", goal_plugin())
        h.use("jobs", jobs_plugin())
        h.ctx.provide("llm", Scripted(turns))
        return h

    config = ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake")
    return WebApp(config, approval_mode="auto", harness_factory=factory)


def wait_idle(conv):
    deadline = time.time() + 5
    while time.time() < deadline and (conv.running or not any(e["type"] in ("done", "error") for e in conv.events)):
        time.sleep(0.02)


def test_web_goal_and_mentions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.md").write_text("date: 11/15")
    app = web_app([AssistantTurn(content="", tool_calls=[call("goal_done", {"status": "done", "summary": "ok"})]),
                   AssistantTurn(content="done")], tmp_path)
    conv = app.create()
    app.send(conv, "update @notes.md", goal={"rounds": 3})
    wait_idle(conv)
    kinds = [e["type"] for e in conv.events]
    assert "attached" in kinds and "goal" in kinds
    assert conv.summary()["goal"]["status"] == "done"


def test_web_saves_images_into_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = web_app([], tmp_path)
    conv = app.create()
    paths = app.save_images(conv, [{"type": "image/png", "data": base64.b64encode(PNG).decode()}])
    assert paths and paths[0].startswith(str(tmp_path))
    assert app.save_images(conv, "nonsense") == []


def test_the_image_describer_is_not_offered_as_a_chat_model():
    from xharness.commands import providers_for

    providers = {"text": {"base_url": "http://x/v1", "model": "t"}, "ocr": {"base_url": "http://x/v1", "model": "eyes"}}
    config = ResolvedConfig(provider=providers["text"], provider_name="text", providers=providers, vision={"provider": "ocr"})
    assert [row["name"] for row in providers_for(config)] == ["text"]
