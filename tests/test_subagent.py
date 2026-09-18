import threading

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage
from xharness.session import messages_from_events
from xharness.subagent import subagent_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolResult, tools_plugin


class RoutingAdapter:
    """Answers by role: children finish immediately, the parent fans out then concludes."""

    def __init__(self, parent_tool_call):
        self.model = "fake"
        self.parent_tool_call = parent_tool_call
        self.child_tool_names = []
        self.lock = threading.Lock()

    def stream(self, messages, tools, on_delta=None):
        system = messages[0]["content"]
        if "child agent" in system:
            with self.lock:
                self.child_tool_names.append(sorted(tool["name"] for tool in tools))
            task = messages[1]["content"]
            return AssistantTurn(content=f"child done: {task}", usage=Usage(10, 5))
        if messages[-1]["role"] == "tool":
            return AssistantTurn(content="parent final: " + messages[-1]["content"][:400], usage=Usage(20, 5))
        return AssistantTurn(content="", tool_calls=[self.parent_tool_call], usage=Usage(30, 5))


def build(parent_tool_call, config=None):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.ctx.get("tools").register(
        Tool(
            name="mutate",
            description="needs approval",
            parameters={"type": "object", "properties": {}},
            execute=lambda args, ctx: ToolResult("mutated") if ctx.approve("mutate") else ToolResult("denied", is_error=True),
            mutating=True,
        )
    )
    harness.use("telemetry", telemetry_plugin())
    harness.use("subagent", subagent_plugin(config))
    adapter = RoutingAdapter(parent_tool_call)
    harness.ctx.provide("llm", adapter)
    return harness, adapter


def test_single_subagent_returns_child_answer():
    call = {"id": "c1", "name": "subagent", "arguments": '{"task": "count files", "name": "counter"}'}
    harness, adapter = build(call)
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    answer = agent.run("delegate")
    assert answer.startswith("parent final: child done: count files")
    assert harness.ctx.get("telemetry").calls == 3  # parent, child, parent
    assert adapter.child_tool_names and "subagent" not in adapter.child_tool_names[0]
    assert "subagent_batch" not in adapter.child_tool_names[0]


def test_batch_runs_children_in_parallel_and_labels_results():
    call = {
        "id": "c1",
        "name": "subagent_batch",
        "arguments": '{"tasks": [{"task": "alpha", "name": "a"}, {"task": "beta"}, "gamma"]}',
    }
    harness, _adapter = build(call, {"max_workers": 3})
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    answer = agent.run("fan out")
    tool_message = next(m for m in agent.messages if m["role"] == "tool")
    assert "## a\nchild done: alpha" in tool_message["content"]
    assert "child done: beta" in tool_message["content"]
    assert "child done: gamma" in tool_message["content"]
    assert harness.ctx.get("telemetry").calls == 5
    assert answer.startswith("parent final")


def test_child_approvals_route_through_parent_policy():
    # Parent in prompt mode with a denying prompter: the child's mutating tool must be denied.
    harness, adapter = build({"id": "c1", "name": "subagent", "arguments": '{"task": "mutate please"}'})

    class MutatingChildAdapter(RoutingAdapter):
        def stream(self, messages, tools, on_delta=None):
            if "child agent" in messages[0]["content"] and messages[-1]["role"] != "tool":
                return AssistantTurn(content="", tool_calls=[{"id": "m1", "name": "mutate", "arguments": "{}"}])
            if "child agent" in messages[0]["content"]:
                return AssistantTurn(content="child saw: " + messages[-1]["content"])
            return super().stream(messages, tools, on_delta)

    harness.ctx._services["llm"] = MutatingChildAdapter(adapter.parent_tool_call)
    seen = []
    agent = Agent(
        harness.ctx,
        AgentOptions(approval_mode="prompt", prompt=lambda summary: (seen.append(summary), False)[1]),
    )
    answer = agent.run("delegate mutation")
    assert any(summary.startswith("[child-") and "mutate" in summary for summary in seen)
    assert "denied" in answer


def test_invalid_inputs():
    harness, _adapter = build({"id": "c1", "name": "subagent", "arguments": '{"task": ""}'})
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    agent.run("x")
    tool_message = next(m for m in agent.messages if m["role"] == "tool")
    assert "task is required" in tool_message["content"]


def test_resume_ignores_subagent_events():
    events = [
        {"type": "message", "role": "user", "content": "parent task"},
        {"type": "message", "role": "user", "content": "child task", "agent": "child-1"},
        {"type": "message", "role": "assistant", "content": "child answer", "agent": "child-1"},
        {"type": "message", "role": "assistant", "content": "parent answer"},
    ]
    messages = messages_from_events(events)
    assert [m["content"] for m in messages] == ["parent task", "parent answer"]
