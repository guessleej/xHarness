import json
import os

import pytest

from xharness.agent import Agent, AgentOptions
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.memory import MemoryStore, memory_plugin, parse_memory
from xharness.tools import ToolContext, tools_plugin


def test_store_write_read_index_and_audit(tmp_path):
    store = MemoryStore(str(tmp_path))
    memory = store.write("user-role", "Jeff is the CTO", "Jeff leads engineering.\nPrefers concise replies.", kind="user", actor={"agent": "main", "session": "s1"})
    assert memory.updated
    again = store.read("user-role")
    assert again is not None and again.kind == "user" and "concise" in again.body
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8").count("user-role") == 1
    assert store.index_lines() == ["- user-role (user): Jeff is the CTO"]

    store.write("user-role", "Jeff is the CTO (updated)", "new body", kind="user")
    records = store.audit()
    assert [record["action"] for record in records] == ["create", "update"]
    assert records[0]["agent"] == "main" and records[0]["session"] == "s1"

    assert store.delete("user-role") is True
    assert store.read("user-role") is None
    assert store.delete("user-role") is False
    assert store.audit()[-1]["action"] == "delete"
    assert "(no memories yet)" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_store_validation(tmp_path):
    store = MemoryStore(str(tmp_path))
    with pytest.raises(ValueError, match="slug"):
        store.write("Bad Name", "desc", "body")
    with pytest.raises(ValueError, match="description"):
        store.write("ok", "   ", "body")
    with pytest.raises(ValueError, match="exceeds"):
        store.write("ok", "desc", "x" * 9000)
    assert store.read("../etc/passwd") is None


def test_search_ranks_name_and_description_hits_first(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.write("deploy-target", "Deploy goes to the MI50 farm", "Use rsync; the farm is called gpu-farm.")
    store.write("coffee", "Jeff likes coffee", "The office deploy machine also makes coffee. deploy deploy.")
    hits = store.search("deploy farm")
    assert [memory.name for memory, _score, _snippet in hits] == ["deploy-target"]
    hits = store.search("deploy")
    assert hits[0][0].name == "deploy-target"  # name/description weight beats raw body counts
    assert store.search("") == []
    assert store.search("nothing-here") == []


def test_index_is_capped(tmp_path):
    store = MemoryStore(str(tmp_path), max_index_chars=120)
    for index in range(6):
        store.write(f"item-{index}", f"description number {index} that is fairly long", "body")
    text = store.index_text()
    assert len(text) < 200 and "more; use memory_search" in text


def test_parse_memory_without_frontmatter():
    memory = parse_memory("raw", "First line is the description\nmore text")
    assert memory.description == "First line is the description" and memory.kind == "note"


class CapturingAdapter:
    def __init__(self, turns):
        self.model = "fake"
        self.turns = list(turns)
        self.seen_system = []

    def stream(self, messages, tools, on_delta=None):
        self.seen_system.append(messages[0]["content"])
        return self.turns.pop(0) if self.turns else AssistantTurn(content="done")


def build(tmp_path, turns):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("memory", memory_plugin({"dir": str(tmp_path)}))
    adapter = CapturingAdapter(turns)
    harness.ctx.provide("llm", adapter)
    return harness, adapter


def test_index_is_injected_into_system_prompt_without_touching_agent_messages(tmp_path):
    harness, adapter = build(tmp_path, [AssistantTurn(content="ok")])
    harness.ctx.get("memory").write("fav-editor", "User prefers vim", "vim, no plugins")
    agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
    agent.run("hi")
    assert "## Memory index" in adapter.seen_system[0] and "fav-editor" in adapter.seen_system[0]
    assert "Memory index" not in agent.messages[0]["content"]


def test_tools_write_read_search_delete_with_approval_and_audit(tmp_path):
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "w", "name": "memory_write", "arguments": json.dumps({"name": "repo-rule", "description": "Always run tests", "content": "pytest before finishing", "kind": "project"})}]),
        AssistantTurn(content="", tool_calls=[{"id": "r", "name": "memory_read", "arguments": json.dumps({"name": "repo-rule"})}]),
        AssistantTurn(content="", tool_calls=[{"id": "s", "name": "memory_search", "arguments": json.dumps({"query": "tests"})}]),
        AssistantTurn(content="", tool_calls=[{"id": "d", "name": "memory_delete", "arguments": json.dumps({"name": "repo-rule"})}]),
        AssistantTurn(content="finished"),
    ]
    harness, _adapter = build(tmp_path, turns)
    approvals = []
    agent = Agent(harness.ctx, AgentOptions(approval_mode="prompt", prompt=lambda s: (approvals.append(s), True)[1], name="worker"))
    assert agent.run("remember things") == "finished"
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert outputs[0].startswith("saved memory repo-rule (project)")
    assert "pytest before finishing" in outputs[1]
    assert "repo-rule" in outputs[2]
    assert outputs[3] == "deleted memory repo-rule"
    assert approvals == ["memory_write: repo-rule", "memory_delete: repo-rule"]
    audit = harness.ctx.get("memory").audit()
    assert [r["action"] for r in audit] == ["create", "delete"] and audit[0]["agent"] == "worker"


def test_write_denied_and_bad_slug(tmp_path):
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "w", "name": "memory_write", "arguments": json.dumps({"name": "Bad Slug", "description": "x", "content": "y"})}]),
        AssistantTurn(content="", tool_calls=[{"id": "w2", "name": "memory_write", "arguments": json.dumps({"name": "fine", "description": "x", "content": "y"})}]),
        AssistantTurn(content="done"),
    ]
    harness, _adapter = build(tmp_path, turns)
    agent = Agent(harness.ctx, AgentOptions(approval_mode="prompt", prompt=lambda s: False))
    agent.run("x")
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "slug" in outputs[0] and "denied" in outputs[1]
    assert not os.path.exists(tmp_path / "fine.md")


def test_tool_context_carries_agent_name():
    ctx = ToolContext(cwd=".", approve=lambda s: True)
    assert ctx.agent == "main"
