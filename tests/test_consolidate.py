import json

from xharness.agent import Agent, AgentOptions
from xharness.consolidate import apply_plan, build_plan, deterministic_plan, find_duplicates, llm_plan
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.memory import MemoryStore, memory_plugin
from xharness.tools import tools_plugin


def seed(store):
    store.write("deploy-a", "Deploy target is gpu-farm", "Deploy to gpu-farm with rsync over the LAN.", kind="project", topic="ops")
    store.write("deploy-b", "Deploy target is gpu-farm", "Deploy to gpu-farm with rsync over the LAN. Use the xcloud user.", kind="project", topic="ops")
    store.write("editor", "User prefers vim", "vim with no plugins", kind="user", topic="ops")
    store.write("coffee", "User likes coffee", "black, no sugar", kind="user")


def test_topics_and_topic_pages(tmp_path):
    store = MemoryStore(str(tmp_path))
    seed(store)
    topics = store.topics()
    assert sorted(topics) == ["ops", "user"]
    assert [m.name for m in topics["ops"]] == ["deploy-a", "deploy-b", "editor"]
    page = (tmp_path / "topics" / "ops.md").read_text(encoding="utf-8")
    assert page.count("\n## ") == 3 and "rsync" in page
    index = store.index_text()
    assert index.startswith("[ops]") and "[user]" in index


def test_find_duplicates_and_deterministic_plan(tmp_path):
    store = MemoryStore(str(tmp_path))
    seed(store)
    memories = store.topics()["ops"]
    pairs = find_duplicates(memories)
    assert [(a.name, b.name) for a, b, _score in pairs] == [("deploy-a", "deploy-b")]
    plan = deterministic_plan("ops", memories)
    assert len(plan.merges) == 1 and plan.merges[0].into == "deploy-b" and plan.merges[0].from_names == ["deploy-a"]
    assert "near-duplicate" in plan.merges[0].reason
    assert "editor" not in plan.describe()


def test_apply_plan_merges_and_audits(tmp_path):
    store = MemoryStore(str(tmp_path))
    seed(store)
    plan = build_plan(store, "ops")
    done = apply_plan(store, plan, actor={"agent": "operator", "session": None})
    assert done == ["merged deploy-a into deploy-b"]
    assert store.read("deploy-a") is None
    merged = store.read("deploy-b")
    assert merged is not None and "xcloud user" in merged.body and merged.topic == "ops"
    assert "併入自 deploy-a" in merged.body and "較舊" in merged.body
    actions = [record["action"] for record in store.audit()]
    assert actions[-2:] == ["consolidate", "consolidate"]
    assert build_plan(store, "ops").empty
    assert build_plan(store, "missing").notes


class ProposingAdapter:
    def __init__(self, reply):
        self.model = "fake"
        self.reply = reply
        self.prompts = []

    def stream(self, messages, tools, on_delta=None):
        self.prompts.append(messages[-1]["content"])
        return AssistantTurn(content=self.reply)


def test_llm_plan_validates_names_and_merges_with_deterministic(tmp_path):
    store = MemoryStore(str(tmp_path))
    seed(store)
    reply = json.dumps(
        {
            "merges": [
                {"into": "editor", "from": ["coffee"], "description": "x", "content": "y"},  # coffee is not in this topic
                {"into": "editor", "from": ["deploy-a"], "description": "conflict", "content": "z"},  # overlaps duplicate merge
            ],
            "deletes": [{"name": "ghost"}, {"name": "editor", "reason": "stale"}],
            "notes": ["looked fine"],
        }
    )
    adapter = ProposingAdapter("Here is the plan:\n" + reply)
    memories = store.topics()["ops"]
    proposed = llm_plan("ops", memories, adapter)
    assert proposed.merges == [] or all(m.into in {"editor"} for m in proposed.merges)
    assert proposed.deletes == ["editor"]
    assert any("unknown" in note for note in proposed.notes)
    assert "deploy-a" in adapter.prompts[0] and "coffee" not in adapter.prompts[0]

    plan = build_plan(store, "ops", llm=adapter)
    assert [m.into for m in plan.merges] == ["deploy-b"]
    assert plan.deletes == ["editor"] and "looked fine" in plan.notes
    assert any("overlaps" in note for note in plan.notes)


def test_llm_plan_tolerates_garbage(tmp_path):
    store = MemoryStore(str(tmp_path))
    seed(store)
    memories = store.topics()["ops"]
    assert llm_plan("ops", memories, ProposingAdapter("no json here")).notes == ["model returned no JSON plan"]
    assert llm_plan("ops", memories, ProposingAdapter("{not json}")).notes == ["model returned invalid JSON"]


def test_agent_tool_dry_run_then_apply_with_approval(tmp_path):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("memory", memory_plugin({"dir": str(tmp_path)}))
    seed(harness.ctx.get("memory"))
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "c1", "name": "memory_consolidate", "arguments": json.dumps({"topic": "ops", "use_model": False})}]),
        AssistantTurn(content="", tool_calls=[{"id": "c2", "name": "memory_consolidate", "arguments": json.dumps({"topic": "ops", "use_model": False, "apply": True})}]),
        AssistantTurn(content="tidy"),
    ]
    harness.ctx.provide("llm", type("A", (), {"model": "fake", "stream": lambda self, m, t, on_delta=None: turns.pop(0)})())
    approvals = []
    agent = Agent(harness.ctx, AgentOptions(approval_mode="prompt", prompt=lambda s: (approvals.append(s), True)[1]))
    assert agent.run("tidy memory") == "tidy"
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "dry run" in outputs[0] and "merge deploy-a -> deploy-b" in outputs[0]
    assert "applied:" in outputs[1] and "merged deploy-a into deploy-b" in outputs[1]
    assert approvals == ["memory_consolidate: ops (1 merges, 0 deletes)"]
    assert harness.ctx.get("memory").read("deploy-a") is None


def test_cli_topics_and_consolidate(tmp_path, monkeypatch, capsys):
    from xharness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "xharness.toml").write_text(f'[providers.x]\nbase_url = "http://localhost:1/v1"\nmodel = "m"\n[memory]\ndir = "{tmp_path / "mem"}"\n', encoding="utf-8")
    seed(MemoryStore(str(tmp_path / "mem")))
    assert main(["memory", "topics"]) == 0
    out = capsys.readouterr().out
    assert "ops (3)" in out and "user (1)" in out
    assert main(["memory", "consolidate", "ops"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and MemoryStore(str(tmp_path / "mem")).read("deploy-a") is not None
    assert main(["memory", "consolidate", "all", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "applied: merged deploy-a into deploy-b" in out
    assert MemoryStore(str(tmp_path / "mem")).read("deploy-a") is None
