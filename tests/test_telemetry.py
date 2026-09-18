import pytest

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.telemetry import BudgetExceeded, telemetry_plugin
from xharness.tools import Tool, ToolResult, tools_plugin


class FakeAdapter:
    def __init__(self, turns):
        self.model = "fake"
        self.turns = list(turns)

    def stream(self, messages, tools, on_delta=None):
        return self.turns.pop(0)


def build(turns, telemetry_config=None):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.ctx.get("tools").register(
        Tool(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            execute=lambda args, ctx: ToolResult("ok"),
        )
    )
    harness.use("telemetry", telemetry_plugin(telemetry_config))
    harness.ctx.provide("llm", FakeAdapter(turns))
    return harness


def test_counts_calls_tokens_tool_calls_and_latency():
    harness = build(
        [
            AssistantTurn(
                content="",
                tool_calls=[{"id": "c1", "name": "echo", "arguments": "{}"}],
                usage=Usage(100, 20),
            ),
            AssistantTurn(content="done", usage=Usage(150, 30)),
        ]
    )
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    assert agent.run("task") == "done"
    telemetry = harness.ctx.get("telemetry")
    report = telemetry.report()
    assert report["calls"] == 2
    assert report["prompt_tokens"] == 250
    assert report["completion_tokens"] == 50
    assert report["total_tokens"] == 300
    assert report["tool_calls"] == 1
    assert report["elapsed_seconds"] >= 0
    assert "model calls" in telemetry.summary()


def test_token_budget_stops_the_loop():
    loop = lambda: AssistantTurn(  # noqa: E731
        content="",
        tool_calls=[{"id": "c", "name": "echo", "arguments": "{}"}],
        usage=Usage(1000, 100),
    )
    harness = build([loop(), loop(), loop()], {"max_total_tokens": 1000})
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    with pytest.raises(BudgetExceeded, match="tokens reached the limit"):
        agent.run("task")
    # the first call went through and was recorded before the brake engaged
    assert harness.ctx.get("telemetry").calls == 1


def test_call_budget_stops_the_loop():
    loop = lambda: AssistantTurn(  # noqa: E731
        content="", tool_calls=[{"id": "c", "name": "echo", "arguments": "{}"}]
    )
    harness = build([loop(), loop(), loop()], {"max_llm_calls": 2})
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    with pytest.raises(BudgetExceeded, match="model calls reached the limit"):
        agent.run("task")
    assert harness.ctx.get("telemetry").calls == 2


def test_cost_reporting():
    harness = build(
        [AssistantTurn(content="done", usage=Usage(1000, 1000))],
        {"cost_per_1k_tokens": 0.5},
    )
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    agent.run("task")
    assert harness.ctx.get("telemetry").report()["cost"] == 1.0


def test_no_budget_means_unlimited():
    harness = build([AssistantTurn(content="done")])
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    assert agent.run("task") == "done"
