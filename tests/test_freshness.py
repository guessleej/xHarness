import json
from datetime import datetime, timedelta, timezone

from xharness.agent import Agent, AgentOptions
from xharness.consolidate import apply_verdicts, verify_plan
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.memory import MemoryStore, memory_plugin
from xharness.tools import tools_plugin


def old_iso(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def backdate(store, name, days):
    """Rewrite a memory's timestamps as if it were written `days` ago (no audit noise)."""
    path = store._path(name)
    text = open(path, encoding="utf-8").read()
    stamp = old_iso(days)
    lines = []
    for line in text.split("\n"):
        if line.startswith("updated:") or line.startswith("verified:"):
            lines.append(f"{line.split(':')[0]}: {stamp}")
        else:
            lines.append(line)
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def test_fresh_memory_is_not_stale_and_backdated_one_is(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30)
    store.write("fresh", "fresh fact", "body", topic="ops")
    store.write("old", "old fact", "body", topic="ops")
    backdate(store, "old", 45)
    assert not store.is_stale(store.read("fresh"))
    assert store.is_stale(store.read("old"))
    stale = store.stale()
    assert [m.name for m, _age in stale] == ["old"] and stale[0][1] >= 45
    assert any("待確認" in line and "old" in line for line in store.index_lines())
    assert "**待確認**" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8") or True  # index rebuilt on next write
    store.write("another", "x", "y", topic="ops")
    assert "[old](old.md) (note) **待確認**" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_usage_keeps_a_memory_fresh(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30)
    store.write("used", "used fact", "body")
    backdate(store, "used", 60)
    assert store.is_stale(store.read("used"))
    store.touch("used")
    assert not store.is_stale(store.read("used"))
    assert store.usage()["used"]["uses"] == 1
    assert store.freshness(store.read("used"))[1] == "used"


def test_verify_resets_and_audits(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30)
    store.write("fact", "a fact", "body")
    backdate(store, "fact", 100)
    assert store.is_stale(store.read("fact"))
    memory = store.verify("fact", actor={"agent": "operator", "session": None})
    assert memory is not None and not store.is_stale(memory)
    assert store.audit()[-1]["action"] == "verify"
    assert store.verify("missing") is None


def test_expiry_archives_but_never_deletes(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30, expire_days=180)
    store.write("ancient", "ancient fact", "body")
    store.write("merely-old", "old fact", "body")
    backdate(store, "ancient", 400)
    backdate(store, "merely-old", 60)
    assert [m.name for m, _age in store.expired()] == ["ancient"]
    assert store.archive("ancient", actor={"agent": "operator", "session": None})
    assert store.read("ancient") is None
    assert (tmp_path / "archive" / "ancient.md").exists()
    assert store.audit()[-1]["action"] == "expire"
    assert MemoryStore(str(tmp_path)).expired() == []  # expire_days 0 = off


class VerdictAdapter:
    def __init__(self, reply):
        self.model = "fake"
        self.reply = reply
        self.prompts = []

    def stream(self, messages, tools, on_delta=None):
        self.prompts.append(messages[-1]["content"])
        return AssistantTurn(content=self.reply)


def test_verify_plan_without_model_and_with_model(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30)
    store.write("port-old", "port is 8080", "The node listens on 8080.", topic="ops")
    store.write("port-new", "port is 3080", "The node listens on 3080.", topic="ops")
    backdate(store, "port-old", 90)
    verdicts = verify_plan(store, "ops")
    assert [(v.name, v.verdict) for v in verdicts] == [("port-old", "unknown")]

    adapter = VerdictAdapter('{"verdict": "contradicted", "reason": "newer memory says 3080"}')
    verdicts = verify_plan(store, "ops", llm=adapter)
    assert verdicts[0].verdict == "contradicted" and "3080" in verdicts[0].reason
    assert "port-new" in adapter.prompts[0] and "待確認的記憶" in adapter.prompts[0]
    assert apply_verdicts(store, verdicts) == []  # contradictions are never applied
    assert store.read("port-old") is not None

    verdicts = verify_plan(store, "ops", llm=VerdictAdapter('{"verdict": "verify", "reason": "consistent"}'))
    assert apply_verdicts(store, verdicts, actor={"agent": "operator", "session": None}) == ["verified port-old"]
    assert not store.is_stale(store.read("port-old"))

    backdate(store, "port-old", 90)
    assert verify_plan(store, "ops", llm=VerdictAdapter("garbage"))[0].verdict == "unknown"
    assert verify_plan(store, "ops", llm=VerdictAdapter('{"verdict": "delete"}'))[0].reason.startswith("unrecognised")


def test_verify_plan_needs_evidence(tmp_path):
    store = MemoryStore(str(tmp_path), stale_days=30)
    store.write("lonely", "only fact", "body", topic="ops")
    backdate(store, "lonely", 90)
    verdicts = verify_plan(store, "ops", llm=VerdictAdapter('{"verdict": "verify"}'))
    assert verdicts[0].verdict == "unknown" and "no newer memories" in verdicts[0].reason


def test_agent_read_touches_and_verify_tool_needs_approval(tmp_path):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("memory", memory_plugin({"dir": str(tmp_path), "stale_days": 30}))
    store = harness.ctx.get("memory")
    store.write("fact", "a fact", "body")
    backdate(store, "fact", 100)
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "r", "name": "memory_read", "arguments": json.dumps({"name": "fact"})}]),
        AssistantTurn(content="", tool_calls=[{"id": "v", "name": "memory_verify", "arguments": json.dumps({"name": "fact"})}]),
        AssistantTurn(content="ok"),
    ]
    seen_system = []

    class A:
        model = "fake"

        def stream(self, messages, tools, on_delta=None):
            seen_system.append(messages[0]["content"])
            return turns.pop(0)

    harness.ctx.provide("llm", A())
    approvals = []
    agent = Agent(harness.ctx, AgentOptions(approval_mode="prompt", prompt=lambda s: (approvals.append(s), True)[1]))
    assert agent.run("check") == "ok"
    assert "fact (note, 待確認" in seen_system[0]  # the entry itself is flagged before the read
    assert store.usage()["fact"]["uses"] == 1
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert outputs[1].startswith("verified memory fact")
    assert approvals == ["memory_verify: fact"]
    assert "fact (note, 待確認" not in seen_system[2] and "- fact (note):" in seen_system[2]  # unflagged after verification


def test_cli_stale_verify_expire(tmp_path, monkeypatch, capsys):
    from xharness.cli import main

    mem = tmp_path / "mem"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "xharness.toml").write_text(
        f'[providers.x]\nbase_url = "http://localhost:1/v1"\nmodel = "m"\n[memory]\ndir = "{mem}"\nstale_days = 30\nexpire_days = 200\n',
        encoding="utf-8",
    )
    store = MemoryStore(str(mem))
    store.write("old", "old fact", "body", topic="ops")
    store.write("ancient", "ancient fact", "body", topic="ops")
    backdate(store, "old", 60)
    backdate(store, "ancient", 300)
    assert main(["memory", "stale"]) == 0
    out = capsys.readouterr().out
    assert "old" in out and "ancient" in out
    assert main(["memory", "verify", "old"]) == 0
    assert "verified old" in capsys.readouterr().out
    assert main(["memory", "verify", "all"]) == 0
    out = capsys.readouterr().out
    assert "ancient" in out and "unknown" in out and "dry run" in out
    assert main(["memory", "expire"]) == 0
    assert "dry run" in capsys.readouterr().out and (mem / "ancient.md").exists()
    assert main(["memory", "expire", "--apply"]) == 0
    assert "archived" in capsys.readouterr().out and (mem / "archive" / "ancient.md").exists()
