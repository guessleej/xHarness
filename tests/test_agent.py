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
