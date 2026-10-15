import copy

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.tools import Tool, ToolResult, tools_plugin


class FakeAdapter:
    def __init__(self, turns):
        self.model = "fake"
        self.turns = list(turns)
        self.seen = []

    def stream(self, messages, tools, on_delta=None):
        self.seen.append(copy.deepcopy(messages))
        if not self.turns:
            raise RuntimeError("no scripted turns left")
        return self.turns.pop(0)


def build(turns):
    harness = Harness()
    harness.use("tools", tools_plugin)
    registry = harness.ctx.get("tools")
    registry.register(
        Tool(
            name="echo",
            description="echo text back",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            execute=lambda args, ctx: ToolResult(f"echo:{args['text']}"),
        )
    )
    adapter = FakeAdapter(turns)
    harness.ctx.provide("llm", adapter)
    return harness, adapter


def test_agent_executes_tool_calls_and_returns_final_answer():
    harness, adapter = build(
        [
            AssistantTurn(content="", tool_calls=[{"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'}]),
            AssistantTurn(content="done", usage=Usage(10, 5)),
        ]
    )
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    assert agent.run("please echo hi") == "done"
    tool_messages = [message for message in agent.messages if message["role"] == "tool"]
    assert tool_messages[0]["content"] == "echo:hi"
    assert tool_messages[0]["tool_call_id"] == "c1"
    # The second model call must include the tool result.
    assert adapter.seen[1][-1]["role"] == "tool"
    assert agent.total_usage.prompt_tokens == 10


def test_unknown_tool_and_bad_json_become_error_results():
    harness, _adapter = build(
        [
            AssistantTurn(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "missing_tool", "arguments": "{}"},
                    {"id": "c2", "name": "echo", "arguments": "not json"},
                ],
            ),
            AssistantTurn(content="recovered"),
        ]
    )
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    assert agent.run("task") == "recovered"
    tool_messages = [message for message in agent.messages if message["role"] == "tool"]
    assert "unknown tool" in tool_messages[0]["content"]
    assert "not valid JSON" in tool_messages[1]["content"]


def test_max_turns_stops_a_tool_calling_loop():
    loop_turn = lambda: AssistantTurn(  # noqa: E731
        content="", tool_calls=[{"id": "c", "name": "echo", "arguments": '{"text": "again"}'}]
    )
    harness, adapter = build([loop_turn(), loop_turn(), loop_turn()])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto", max_turns=2))
    assert "max turns" in agent.run("loop forever")
    assert len(adapter.seen) == 2


def test_llm_stream_middleware_wraps_the_model_call():
    harness, _adapter = build([AssistantTurn(content="plain")])
    intercepted = []
    harness.ctx.intercept("llm/stream", lambda payload, next_fn: (intercepted.append(1), next_fn(payload))[1])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    agent.run("task")
    assert len(intercepted) == 1


def test_resume_keeps_existing_system_prompt():
    harness, _adapter = build([AssistantTurn(content="ok")])
    initial = [
        {"role": "system", "content": "custom"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto", initial_messages=initial))
    assert agent.messages[0]["content"] == "custom"
    assert sum(1 for message in agent.messages if message["role"] == "system") == 1


def test_project_instructions_loaded_into_system_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always run scripts/check before finishing.")
    (tmp_path / "CLAUDE.md").write_text("should not be used when AGENTS.md exists")
    harness, _adapter = build([AssistantTurn(content="ok")])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto", cwd=str(tmp_path)))
    system = agent.messages[0]["content"]
    assert "Project instructions (AGENTS.md)" in system
    assert "scripts/check" in system
    assert "should not be used" not in system


def test_claude_md_fallback_and_opt_out(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("repo rules here")
    harness, _adapter = build([AssistantTurn(content="ok")])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto", cwd=str(tmp_path)))
    assert "Project instructions (CLAUDE.md)" in agent.messages[0]["content"]

    harness2, _adapter2 = build([AssistantTurn(content="ok")])
    agent2 = Agent(
        harness2.ctx,
        AgentOptions(approval_mode="auto", cwd=str(tmp_path), project_instructions=False),
    )
    assert "Project instructions" not in agent2.messages[0]["content"]


def test_no_instruction_file_leaves_prompt_unchanged(tmp_path):
    harness, _adapter = build([AssistantTurn(content="ok")])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto", cwd=str(tmp_path)))
    assert "Project instructions" not in agent.messages[0]["content"]


def test_operator_stop_ends_loop_and_skips_tools():
    harness, adapter = build(
        [
            AssistantTurn(content="", tool_calls=[{"id": "c1", "name": "echo", "arguments": '{"text": "a"}'}, {"id": "c2", "name": "echo", "arguments": '{"text": "b"}'}]),
            AssistantTurn(content="should not be reached"),
        ]
    )
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    registry = harness.ctx.get("tools")
    original = registry.get("echo").execute

    def stopping_execute(args, ctx):
        agent.stop()  # the operator presses stop while the first tool runs
        return original(args, ctx)

    registry.get("echo").execute = stopping_execute
    answer = agent.run("go")
    assert answer == "[stopped by operator]"
    tool_messages = [m for m in agent.messages if m["role"] == "tool"]
    assert tool_messages[0]["content"] == "echo:a"
    assert "stopped by operator" in tool_messages[1]["content"]
    assert len(adapter.seen) == 1


def test_system_prompt_states_the_real_model_identity():
    harness, _adapter = build([AssistantTurn(content="ok")])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    system = agent.messages[0]["content"]
    assert "You run on the model 'fake'" in system
    assert "never claim to be GPT" in system
    from xharness.agent import default_system_prompt

    assert "operator's own infrastructure" in default_system_prompt(".")


def test_site_instructions_reach_a_user_working_elsewhere(tmp_path):
    """With per-user workspaces the operator's AGENTS.md must still apply."""
    from xharness.agent import load_project_instructions

    site = tmp_path / "server"
    workspace = tmp_path / "ws"
    site.mkdir()
    workspace.mkdir()
    (site / "AGENTS.md").write_text("站台規則：不確定就說不確定。", encoding="utf-8")

    text = load_project_instructions(str(workspace), str(site))
    assert text is not None and "站台規則" in text and "Site instructions" in text


def test_project_file_refines_the_site_one(tmp_path):
    from xharness.agent import load_project_instructions

    site = tmp_path / "server"
    workspace = tmp_path / "ws"
    site.mkdir()
    workspace.mkdir()
    (site / "AGENTS.md").write_text("站台規則", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("專案慣例", encoding="utf-8")

    text = load_project_instructions(str(workspace), str(site))
    assert text.index("站台規則") < text.index("專案慣例")   # site first, project refines


def test_same_directory_is_not_read_twice(tmp_path):
    from xharness.agent import load_project_instructions

    (tmp_path / "AGENTS.md").write_text("只有一份", encoding="utf-8")
    text = load_project_instructions(str(tmp_path), str(tmp_path))
    assert text.count("只有一份") == 1


def test_no_instructions_anywhere_is_none(tmp_path):
    from xharness.agent import load_project_instructions

    assert load_project_instructions(str(tmp_path), str(tmp_path)) is None


def test_date_line_spells_out_the_weekday_and_offset():
    from datetime import datetime, timedelta, timezone
    from xharness.agent import current_date_line
    taipei = timezone(timedelta(hours=8))
    line = current_date_line(datetime(2026, 10, 14, 20, 27, tzinfo=taipei))
    assert "2026-10-14 (Wednesday)" in line and "UTC+08:00" in line and "CST" not in line


def test_system_prompt_carries_today_even_when_custom():
    """Without it the model assumes a date from training data and writes it into files."""
    from datetime import datetime
    from xharness.agent import Agent, AgentOptions
    from xharness.context import Harness
    from xharness.llm import AssistantTurn
    from xharness.tools import tools_plugin

    class Echo:
        model = "m"
        def __init__(self): self.seen = None
        def stream(self, messages, tools, on_delta=None):
            self.seen = messages
            return AssistantTurn(content="ok")

    harness = Harness()
    harness.use("tools", tools_plugin)
    llm = Echo()
    harness.ctx.provide("llm", llm)
    Agent(harness.ctx, AgentOptions(system_prompt="custom prompt", project_instructions=False)).run("hi")
    system = llm.seen[0]["content"]
    assert system.startswith("custom prompt") and datetime.now().strftime("%Y-%m-%d") in system



class Recorder:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def call(name, arguments, call_id="c"):
    return {"id": call_id, "name": name, "arguments": arguments}


def test_system_prompt_is_logged_once_as_context():
    harness, _adapter = build([AssistantTurn(content="a"), AssistantTurn(content="b")])
    log = Recorder()
    harness.ctx.provide("session", log)
    agent = Agent(harness.ctx, AgentOptions())
    agent.run("one")
    agent.run("two")
    context = [event for event in log.events if event["type"] == "context"]
    assert len(context) == 1
    assert context[0]["content"] == agent.messages[0]["content"]
    assert "Today is" in context[0]["content"]


def test_new_day_is_told_with_the_message_and_logged(monkeypatch):
    import datetime as real

    from xharness import agent as agent_module
    from xharness.session import messages_from_events

    harness, adapter = build([AssistantTurn(content="a"), AssistantTurn(content="b")])
    log = Recorder()
    harness.ctx.provide("session", log)
    agent = Agent(harness.ctx, AgentOptions())
    agent.run("same day")
    assert adapter.seen[0][-1]["content"] == "same day"

    class Tomorrow(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime.now(tz) + real.timedelta(days=1)

    monkeypatch.setattr(agent_module, "datetime", Tomorrow)
    agent.run("next day")
    sent = adapter.seen[1][-1]["content"]
    assert sent.startswith("next day\n\n[Today is ")
    logged = [event for event in log.events if event.get("role") == "user"][-1]
    assert logged["content"] == "next day" and logged["context"]
    # Resume replays what the model was sent, not just what the person typed.
    assert messages_from_events(log.events)[-2]["content"] == sent


def test_repeated_identical_calls_get_a_note_at_three():
    same = '{"text": "x"}'
    turns = [AssistantTurn(content="", tool_calls=[call("echo", same, f"c{i}")]) for i in range(3)]
    harness, _adapter = build(turns + [AssistantTurn(content="done")])
    agent = Agent(harness.ctx, AgentOptions())
    agent.run("loop")
    results = [message["content"] for message in agent.messages if message["role"] == "tool"]
    assert "call 3 in a row" not in results[1]
    assert "call 3 in a row to echo" in results[2]


def test_argument_order_does_not_hide_a_repeat():
    turns = [
        AssistantTurn(content="", tool_calls=[call("echo", '{"text": "x", "n": 1}', "a")]),
        AssistantTurn(content="", tool_calls=[call("echo", '{"n": 1, "text": "x"}', "b")]),
        AssistantTurn(content="", tool_calls=[call("echo", '{"text":"x","n":1}', "c")]),
        AssistantTurn(content="done"),
    ]
    harness, _adapter = build(turns)
    agent = Agent(harness.ctx, AgentOptions())
    agent.run("loop")
    assert "in a row" in [m for m in agent.messages if m["role"] == "tool"][-1]["content"]


def test_oversized_output_is_spilled_to_a_readable_file(tmp_path):
    from xharness.agent import INLINE_OUTPUT_CHARS, SPILL_HEAD_CHARS, spill_output

    small = "x" * INLINE_OUTPUT_CHARS
    assert spill_output(small, str(tmp_path)) == small
    big = "H" * SPILL_HEAD_CHARS + "M" * 50_000 + "TAIL"
    shown = spill_output(big, str(tmp_path))
    assert len(shown) < INLINE_OUTPUT_CHARS + 500
    from xharness.agent import SPILL_TAIL_CHARS

    assert shown.startswith("H" * SPILL_HEAD_CHARS + "\n\n[... the full output")
    assert shown.endswith("TAIL") and shown.count("M") == SPILL_TAIL_CHARS - len("TAIL")
    spilled = list((tmp_path / ".xharness" / "spill").iterdir())
    assert len(spilled) == 1 and spilled[0].read_text() == big
    assert spilled[0].name in shown
    assert (tmp_path / ".xharness" / ".gitignore").read_text() == "*\n"


def test_spill_keeps_only_recent_files(tmp_path):
    from xharness.agent import INLINE_OUTPUT_CHARS, SPILL_KEEP_FILES, spill_output

    for _ in range(SPILL_KEEP_FILES + 3):
        spill_output("y" * (INLINE_OUTPUT_CHARS + 1), str(tmp_path))
    assert len(list((tmp_path / ".xharness" / "spill").iterdir())) == SPILL_KEEP_FILES


class OverflowOnce(FakeAdapter):
    def stream(self, messages, tools, on_delta=None):
        if not getattr(self, "overflowed", False) and len(self.seen) == 3:
            self.overflowed = True
            self.seen.append(None)
            raise RuntimeError("LLM request failed: 400 Bad Request the request exceeds the available context size")
        return super().stream(messages, tools, on_delta)


def test_context_overflow_prunes_old_tool_results_and_retries():
    turns = [AssistantTurn(content="", tool_calls=[call("echo", f'{{"text": "{i}"}}', f"c{i}")]) for i in range(3)]
    harness, adapter = build(turns + [AssistantTurn(content="fits now")])
    adapter.__class__ = OverflowOnce
    log = Recorder()
    harness.ctx.provide("session", log)
    agent = Agent(harness.ctx, AgentOptions())
    assert agent.run("go") == "fits now"
    contents = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert contents[0].startswith("[output removed")
    assert contents[1:] == ["echo:1", "echo:2"]
    assert {"type": "context-pruned", "tool_results": 1} in log.events
    # The log keeps the original output.
    assert [e["output"] for e in log.events if e["type"] == "tool-result"][0] == "echo:0"


def test_other_model_errors_are_not_mistaken_for_overflow():
    from xharness.agent import is_context_overflow

    assert is_context_overflow(RuntimeError("This model's maximum context length is 32768 tokens"))
    assert is_context_overflow(RuntimeError("context_length_exceeded"))
    assert not is_context_overflow(RuntimeError("LLM request failed: 401 Unauthorized"))
    assert not is_context_overflow(RuntimeError("cannot reach http://x (refused)"))


def test_reset_keeps_a_dated_system_prompt():
    harness, _ = build([AssistantTurn(content="a")])
    agent = Agent(harness.ctx, AgentOptions(system_prompt="custom"))
    agent.run("hi")
    agent.reset()
    assert len(agent.messages) == 1
    assert agent.messages[0]["content"].startswith("custom\nToday is ")


def test_default_prompt_asks_to_confirm_relative_dates():
    from xharness.agent import default_system_prompt

    prompt = default_system_prompt("/w", "m")
    assert "relative date" in prompt and "confirm the exact date" in prompt
