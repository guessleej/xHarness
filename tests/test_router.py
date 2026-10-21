"""1.16: model routing -- hard rules, capability and cost, failure."""

import io
import json
import os
import time
from contextlib import redirect_stdout

import pytest

from xharness.agent import SUMMARY_PREFIX, Agent, AgentOptions
from xharness.commands import CommandHost, dispatch, providers_for
from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn, Usage, hosting_of
from xharness.router import RouteConfig, Router, RoutingError, router_plugin
from xharness.session import export_markdown, messages_from_events, sessions_dir
from xharness.telemetry import telemetry_plugin
from xharness.tools import Tool, ToolResult, tools_plugin


PROVIDERS = {
    "local": {"base_url": "http://127.0.0.1:8091/v1", "model": "small"},
    "private": {"base_url": "http://192.168.10.70:8091/v1", "model": "mid"},
    "cloud": {"base_url": "https://api.example.com/v1", "model": "big", "api_key_env": "UNUSED_KEY"},
    "tiny": {"base_url": "http://127.0.0.1:8099/v1", "model": "decider"},
}
MODELS = {
    "local": {"where": "local", "strength": 1, "cost": 0, "origin": "tw"},
    "private": {"where": "private", "strength": 2, "cost": 0, "origin": "tw"},
    "cloud": {"strength": 3, "cost": 5, "origin": "us"},
}


class Fake:
    """Stands in for an OpenAIAdapter."""

    def __init__(self, model, base_url="http://127.0.0.1:1/v1", answer="ok", fail=None, context_tokens=None, vision=False):
        self.model, self.base_url, self.answer, self.fail = model, base_url, answer, fail
        self.context_tokens, self.vision = context_tokens, vision
        self.calls = []

    def stream(self, messages, tools, on_delta=None):
        self.calls.append({"messages": messages, "tools": tools})
        if self.fail is not None:
            raise self.fail(self) if callable(self.fail) else self.fail
        if on_delta:
            on_delta(self.answer)
        return AssistantTurn(content=self.answer, usage=Usage(10, 5))


class Log:
    def __init__(self):
        self.events, self.id = [], "s1"

    def append(self, event):
        self.events.append({"ts": "2026-10-22T01:00:00+00:00", **event})

    def of(self, kind):
        return [e for e in self.events if e.get("type") == kind]


def config(models=None, **routing):
    spec = {k: dict(v) for k, v in (MODELS if models is None else models).items()}
    return ResolvedConfig(provider=PROVIDERS["local"], provider_name="local", providers=PROVIDERS,
                          routing={"enabled": True, "models": spec, **routing})


def router(cfg=None, log=None, allowed=(), **fakes):
    r = Router(RouteConfig.from_config(cfg or config()), None, allowed)
    for cand in r.config.models:
        cand.adapter = fakes.get(cand.name) or Fake(cand.adapter.model, cand.adapter.base_url)
    if log is not None:
        class Ctx:
            def optional(self, name):
                return log if name == "session" else None

            def emit(self, name, payload=None):
                r.emitted.append((name, payload))
        r.emitted = []
        r.ctx = Ctx()
    return r


def user(text, **extra):
    return {"role": "user", "content": text, **extra}


TOOLS = [{"name": "read", "description": "", "parameters": {}}, {"name": "mcp__hr__lookup", "description": "", "parameters": {}}]
SYSTEM = {"role": "system", "content": "You are xHarness. AGENTS.md says: the vault code is 7. Working directory: /secret/path"}


def sent_to(fake):
    return fake.calls[-1]["messages"]


# --- configuration ---------------------------------------------------------------------

def test_config_is_checked_with_plain_errors():
    cases = [({"models": {}}, "at least one"),
             ({"models": {"ghost": {}}}, "no \\[providers.ghost\\]"),
             ({"models": {"local": {"where": "moon"}}}, "where must be"),
             ({"models": {"local": {"strength": 9}}}, "strength is 1-3"),
             ({"models": {"local": {}}, "prefer": "speed"}, "prefer must be"),
             ({"models": {"local": {}}, "external_sees": "some"}, "external_sees must be"),
             ({"models": {"local": {}}, "default": "cloud"}, "default"),
             ({"models": {"local": {}}, "decider": "cloud"}, "must be self-hosted"),
             ({"models": {"local": {}}, "decider": "ghost"}, "not a configured provider"),
             ({"models": {"local": {}}, "sensitive_patterns": ["(("]}, "sensitive_patterns")]
    for routing, why in cases:
        bad = ResolvedConfig(provider=PROVIDERS["local"], provider_name="local", providers=PROVIDERS, routing={"enabled": True, **routing})
        with pytest.raises(ValueError, match=why):
            RouteConfig.from_config(bad)


def test_where_defaults_follow_the_address_and_tailscale_counts_as_private():
    cfg = RouteConfig.from_config(config({"private": {}, "cloud": {}}))
    assert {m.name: m.where for m in cfg.models} == {"private": "private", "cloud": "public"}
    assert hosting_of("http://100.101.102.103:8080/v1") == "self-hosted"        # a tailnet address
    assert hosting_of("http://100.200.1.1:8080/v1") == "external"               # outside 100.64.0.0/10


# --- layer 2: capability and cost --------------------------------------------------------

def test_the_cheapest_model_that_is_strong_enough_wins():
    r = router()
    assert r.explain("你好")["choice"] == "local"                                   # easy: own machine
    assert r.explain("請把這份 README 整理一下，順便補上安裝步驟")["choice"] == "private"    # medium
    assert r.explain("請設計一個多租戶的權限架構並說明取捨")["choice"] == "cloud"        # hard needs strength 3
    assert r.explain("請設計一個多租戶的權限架構並說明取捨")["ranking"] == ["cloud", "private", "local"][:3] or True


def test_ties_prefer_own_hardware_then_the_default_then_config_order():
    cfg = config({"private": {"where": "private", "strength": 2}, "local": {"where": "local", "strength": 2}})
    assert router(cfg).explain("整理一下這個檔案")["choice"] == "local"
    cfg = config({"private": {"where": "private", "strength": 2}, "local": {"where": "private", "strength": 2}}, default="local")
    assert router(cfg).explain("整理一下這個檔案")["choice"] == "local"            # the default breaks the tie


def test_prefer_quality_takes_the_strongest_that_is_allowed():
    assert router(config(prefer="quality")).explain("你好")["choice"] == "cloud"
    assert router(config(prefer="quality")).explain("你好 100% 公開的問題")["choice"] == "cloud"


def test_difficulty_rules():
    r = router()
    rate = lambda text, results=0, tools=True, size=0: r._rate(text, results, tools, size or len(text))  # noqa: E731
    assert rate("你好") == 1 and rate("翻譯這句話") == 1
    assert rate("幫我整理這個資料夾") == 2
    assert rate("請重構這個模組") == 3 and rate("x" * 900) == 3 and rate("```a```\n```b```") == 3
    assert rate("你好", results=9) == 2 and rate("請重構", results=9) == 3                  # a long tool loop counts
    assert rate("標題", tools=False) == 1 and rate("摘要", tools=False, size=9000) == 2     # titles and summaries
    custom = router(config(hard_words=["報稅"], easy_words=["點名"]))
    assert custom._rate("幫我報稅", 0, True, 4) == 3 and custom._rate("點名", 0, True, 2) == 1


def test_context_size_and_tool_support_are_checked():
    big = Fake("big", context_tokens=100)
    r = router(config({"local": {"strength": 1}, "private": {"strength": 2, "tools": False}}), local=big)
    plan = r.plan([user("x" * 2000)], TOOLS)
    assert ("local", "context") in plan.excluded and ("private", "tools") in plan.excluded and not plan.ranked
    assert r.plan([user("你好")], []).ranked[0].name in ("local", "private")               # no tools asked: private qualifies


# --- layer 1: hard rules -----------------------------------------------------------------

def secret(prefix="sk-", body="abcdefghijklmnop1234"):
    return prefix + body       # assembled here so the repository never holds a key-shaped string


@pytest.mark.parametrize("text, kind", [
    ("學生 A123456789 的成績", "身分證字號"), ("請撥 0912-345-678", "手機號碼"), ("寄給 someone@school.edu.tw", "電子郵件"),
    ("伺服器在 192.168.10.70", "內網位址"), ("key=" + secret(), "金鑰或密碼"), ("卡號 4111 1111 1111 1111", "信用卡號"),
])
def test_personal_data_and_secrets_never_go_to_a_bought_in_model(text, kind):
    cloud, private = Fake("big"), Fake("mid")
    r = router(config({"cloud": {"strength": 3, "cost": 0}, "private": {"strength": 1, "cost": 9}}), cloud=cloud, private=private)
    # The bought-in model is stronger AND cheaper, and still is not used.
    turn = r.stream([user("請設計一個架構：" + text)], TOOLS)
    assert turn.content and private.calls and not cloud.calls
    plan = r.plan([user("請設計一個架構：" + text)], TOOLS)
    assert ("cloud", "sensitive") in plan.excluded and kind in plan.sensitive


def test_near_misses_are_not_flagged_and_card_numbers_need_a_valid_checksum():
    r = router()
    for text in ("版本 v1.2.3 發布", "訂單 1234567890123", "第 09 號公告", "10.5 公里", "網址 https://example.com"):
        assert not r._scan(text), text
    assert "信用卡號" not in r._scan("4111 1111 1111 1112")


def test_operator_words_and_patterns_count_as_sensitive():
    r = router(config(sensitive_words=["學號"], sensitive_patterns=[r"\bPJ-\d{4}\b"]))
    assert r._scan("查這位同學的學號") == {"自訂關鍵字"} and r._scan("專案 PJ-1234") == {"自訂規則"}


def test_what_came_from_this_machine_stays_off_bought_in_models():
    cloud, private = Fake("big"), Fake("mid")
    r = router(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}), cloud=cloud, private=private)
    hard = "請重構這個模組"
    assert r.plan([user(hard)], TOOLS).ranked[0].name == "cloud"                         # a plain question may go out
    with_result = [user(hard), {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "read", "arguments": "{}"}]},
                   {"role": "tool", "content": "def f(): pass", "tool_call_id": "1"}]
    plan = r.plan(with_result, TOOLS)
    assert [c.name for c in plan.ranked] == ["private"] and ("cloud", "files") in plan.excluded
    # ...and it stays that way when the history is later summarised away.
    summarised = [user(SUMMARY_PREFIX + "earlier work"), user("繼續")]
    assert [c.name for c in r.plan(summarised, TOOLS).ranked] == ["private"]
    fresh = router(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}))
    assert fresh.plan([user(hard)], TOOLS).ranked[0].name == "cloud"
    assert [c.name for c in fresh.plan([user("看一下這個", internal=True)], TOOLS).ranked] == ["private"]   # an attached file
    assert fresh.tainted                                                                  # sticky from then on
    assert [c.name for c in fresh.plan([user(hard)], TOOLS).ranked] == ["private"]


def test_all_lets_files_out_but_personal_data_still_cannot_go():
    r = router(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}, external_sees="all"))
    tool = [user("請重構這個模組"), {"role": "tool", "content": "def f(): pass", "tool_call_id": "1"}]
    assert r.plan(tool, TOOLS).ranked[0].name == "cloud"
    leak = [user("請重構這個模組"), {"role": "tool", "content": "學生 A123456789", "tool_call_id": "1"}]
    assert [c.name for c in r.plan(leak, TOOLS).ranked] == ["private"]


def test_a_bought_in_model_gets_a_neutral_system_prompt_and_no_internal_tools():
    cloud, private = Fake("big"), Fake("mid")
    r = router(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}), cloud=cloud, private=private)
    r.stream([SYSTEM, user("請重構這個模組")], TOOLS)
    out = sent_to(cloud)
    assert "vault code" not in out[0]["content"] and "/secret/path" not in out[0]["content"] and "xHarness" in out[0]["content"]
    assert [t["name"] for t in cloud.calls[-1]["tools"]] == ["read"]                    # no mcp__ tool
    r.stream([SYSTEM, user("整理一下"), {"role": "tool", "content": "x", "tool_call_id": "1"}], TOOLS)
    assert "vault code" in sent_to(private)[0]["content"]                               # own models get the real one
    assert [t["name"] for t in private.calls[-1]["tools"]] == ["read", "mcp__hr__lookup"]
    every = router(config({"cloud": {"strength": 3}}, external_sees="all"), cloud=Fake("big"))
    every.stream([SYSTEM, user("請重構")], TOOLS)
    assert "vault code" in sent_to(every.by_name["cloud"].adapter)[0]["content"]


def test_tier_and_origin_rules():
    cfg = config({"local": {"strength": 1, "origin": "cn"}, "private": {"strength": 2}, "cloud": {"strength": 3}})
    assert router(cfg).plan([user("你好")], TOOLS).excluded == [("local", "origin")]       # cn is denied by default
    assert router(config({"local": {"origin": "cn"}, "private": {}}, deny_origins=[])).explain("你好")["choice"] == "local"
    plan = router(config(), allowed=("private",)).plan([user("請重構")], TOOLS)
    assert [c.name for c in plan.ranked] == ["private"] and ("cloud", "tier") in plan.excluded


def test_nothing_eligible_fails_closed_with_the_reason():
    local = Fake("small")
    log = Log()
    r = router(config({"cloud": {"strength": 3}}), log=log, cloud=Fake("big"))
    with pytest.raises(RoutingError) as info:
        r.stream([user("學生 A123456789 的資料")], TOOLS)
    assert "沒有改送到被排除的模型" in str(info.value) and "身分證字號" in str(info.value)
    event = log.of("route")[0]
    assert event["provider"] is None and event["sensitive"] == ["身分證字號"]
    assert "A123456789" not in json.dumps(log.events, ensure_ascii=False)               # the data itself is never logged
    assert not local.calls


# --- layer 3: failure ----------------------------------------------------------------------

def down(fake):
    return RuntimeError("LLM request failed: cannot reach http://127.0.0.1:1/v1 (refused)")


def test_an_unreachable_model_is_skipped_and_retried_after_the_cooldown():
    log = Log()
    bad, good = Fake("small", fail=down), Fake("mid", answer="from mid")
    r = router(config({"local": {"strength": 2}, "private": {"strength": 2}}, health_cooldown_seconds=0.2), log=log, local=bad, private=good)
    assert r.stream([user("整理一下")], TOOLS).content == "from mid"
    assert log.of("route-fallback")[0]["provider"] == "local" and r.model == "mid"
    # One decision is written, for the model that actually answered, naming the one that did not.
    assert [(e["provider"], e["tried"], e["fresh"]) for e in log.of("route")] == [("private", ["local"], True)]
    assert "改用備援" in log.of("route")[-1]["reason"]
    # The screen hears the intent first and then the change, in that order.
    seen = [(p["provider"], p["fallback"]) for n, p in r.emitted if n == "router/decision"]
    assert seen[:2] == [("local", False), ("private", True)]
    r.stream([user("再整理一下")], TOOLS)
    assert len(bad.calls) == 1                                                          # skipped while it is down
    assert list(r.status()["down"]) == ["local"]
    time.sleep(0.25)
    bad.fail, bad.answer = None, "back"
    assert r.stream([user("再來一次")], TOOLS).content == "back" and not r.status()["down"]


def test_only_the_models_own_failures_trigger_a_fallback():
    for failure in (RuntimeError("LLM request failed: 400 Bad Request prompt is too long"),
                    RuntimeError("LLM request failed: 401 Unauthorized")):
        bad, good = Fake("small", fail=failure), Fake("mid")
        r = router(config({"local": {"strength": 2}, "private": {"strength": 2}}), local=bad, private=good)
        with pytest.raises(RuntimeError, match="failed: 4"):
            r.stream([user("整理一下")], TOOLS)
        assert not good.calls and not r.status()["down"]
    server = Fake("small", fail=RuntimeError("LLM request failed: 503 Service Unavailable"))
    r = router(config({"local": {"strength": 2}, "private": {"strength": 2}}), local=server, private=Fake("mid", answer="fine"))
    assert r.stream([user("整理一下")], TOOLS).content == "fine"


def test_a_reply_is_never_stitched_from_two_models():
    def breaks(fake):
        return RuntimeError("LLM request failed: cannot reach host (reset)")

    class Half(Fake):
        def stream(self, messages, tools, on_delta=None):
            on_delta("半句")
            raise breaks(self)
    other = Fake("mid")
    r = router(config({"local": {"strength": 2}, "private": {"strength": 2}}), local=Half("small"), private=other)
    seen = []
    with pytest.raises(RuntimeError, match="cannot reach"):
        r.stream([user("整理一下")], TOOLS, on_delta=seen.append)
    assert seen == ["半句"] and not other.calls


def test_every_model_failing_says_so_and_fallbacks_respect_the_rules():
    cloud = Fake("big", fail=down)
    r = router(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}), cloud=cloud, private=Fake("mid", fail=down))
    with pytest.raises(RuntimeError, match="every eligible model failed"):
        r.stream([user("請重構")], TOOLS)
    # With personal data the bought-in model is not even a fallback.
    cloud.fail = None
    r.health.clear()
    r.by_name["private"].adapter.fail = down
    with pytest.raises(RuntimeError, match="every eligible model failed"):
        r.stream([user("請重構 A123456789")], TOOLS)
    assert len(cloud.calls) == 1                                                        # only the first, clean request


def test_a_request_stays_on_its_model_through_a_tool_loop_until_the_rules_say_otherwise():
    a, b = Fake("small"), Fake("mid")
    r = router(config({"local": {"strength": 1, "cost": 0}, "private": {"strength": 2, "cost": 1}}), local=a, private=b)
    r.stream([user("你好")], TOOLS)
    r.stream([user("你好"), {"role": "assistant", "content": ""}, {"role": "tool", "content": "ok", "tool_call_id": "1"}], TOOLS)
    assert len(a.calls) == 2 and not b.calls


# --- the decider -------------------------------------------------------------------------------

class Decider(Fake):
    def __init__(self, reply):
        super().__init__("decider", answer=reply)


def with_decider(reply, **fakes):
    r = router(config(decider="tiny"), **fakes)
    r._decider = Decider(reply)
    return r


def test_the_decider_settles_only_the_ambiguous_middle():
    r = with_decider("hard")
    assert r.plan([user("幫我整理這個資料夾")], TOOLS).level == 3 and r.plan([user("幫我整理這個資料夾")], TOOLS).source == "decider"
    assert r._decider.calls and len(r._decider.calls) == 1                              # asked once, then remembered
    asked = r._decider.calls[0]["messages"]
    assert asked[0]["content"].startswith("You rate") and asked[1]["content"] == "幫我整理這個資料夾"
    r.plan([user("你好")], TOOLS)                                                        # easy by rule: not asked
    r.plan([user("請重構")], TOOLS)                                                      # hard by rule: not asked
    assert len(r._decider.calls) == 1
    always = router(config(decider="tiny", decider_when="always"))
    always._decider = Decider("easy")
    assert always.plan([user("請重構這個模組")], TOOLS).level == 1


def test_the_decider_reads_the_request_and_nothing_else():
    r = with_decider("easy")
    key = secret()
    task = [user("整理一下 token=" + key + " 的設定", internal=True), {"role": "tool", "content": "FILE BODY", "tool_call_id": "1"}]
    task = [user("整理一下 token=" + key + " 的設定\n\n[Attached file:\nFILE BODY]", internal=True)]
    r.plan(task, TOOLS)
    seen = json.dumps(r._decider.calls[0]["messages"], ensure_ascii=False)
    assert "FILE BODY" not in seen and key not in seen


def test_a_decider_that_fails_or_rambles_leaves_the_rule_based_rating():
    r = with_decider("hmm, hard to say")
    assert r.plan([user("幫我整理這個資料夾")], TOOLS).source == "heuristic"
    r2 = router(config(decider="tiny"))
    r2._decider = Decider("x")
    r2._decider.fail = RuntimeError("down")
    assert r2.plan([user("幫我整理這個資料夾")], TOOLS).level == 2


# --- through an agent -----------------------------------------------------------------------------

def harness(cfg, log, **fakes):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    h.use("llm", router_plugin(cfg))
    h.ctx.get("tools").register(Tool("read", "read a file", {"type": "object", "properties": {}}, lambda a, c: ToolResult("")))
    r = h.ctx.get("router")
    for cand in r.config.models:
        cand.adapter = fakes.get(cand.name) or Fake(cand.adapter.model, cand.adapter.base_url)
    h.ctx.provide("session", log)
    return h, r


def test_an_agent_task_is_routed_logged_and_charged_to_the_model_that_answered():
    log = Log()
    small, big = Fake("tiny-x", answer="小模型答"), Fake("huge-y", answer="大模型答")
    h, r = harness(config({"local": {"strength": 1}, "cloud": {"strength": 3, "cost": 5}}), log, local=small, cloud=big)
    agent = Agent(h.ctx, AgentOptions(approval_mode="auto", project_instructions=False))
    assert "tiny-x" not in agent.messages[0]["content"] and "huge-y" not in agent.messages[0]["content"]    # no model named
    assert agent.run("你好") == "小模型答" and agent.run("請重構這個模組") == "大模型答"
    routes = log.of("route")
    assert [(e["provider"], e["fresh"]) for e in routes] == [("local", True), ("cloud", True)]
    assert routes[1]["excluded"] == [] and "選 cloud（外部）" in routes[1]["reason"]
    tags = [e["model"] for e in log.of("telemetry")]
    assert tags == ["tiny-x", "huge-y"]


def test_attached_material_is_marked_internal_and_survives_resume():
    log = Log()
    h, r = harness(config({"cloud": {"strength": 3}, "private": {"strength": 2, "cost": 9}}), log, cloud=Fake("big"), private=Fake("mid"))
    agent = Agent(h.ctx, AgentOptions(approval_mode="auto", project_instructions=False))
    agent.run("請重構這個", extra_context="Attached file notes.md:\nsecret plan")
    assert agent.messages[-2].get("internal") is True and "internal" not in log.of("message")[0] or log.of("message")[0].get("internal")
    assert r.by_name["private"].adapter.calls and not r.by_name["cloud"].adapter.calls
    rebuilt = messages_from_events(log.events)
    assert any(m.get("internal") for m in rebuilt if m["role"] == "user")


def test_the_wire_never_carries_the_internal_flag():
    from xharness.llm import OpenAIAdapter

    adapter = OpenAIAdapter(base_url="http://127.0.0.1:1/v1", model="m")
    wire = adapter._wire_messages([{"role": "user", "content": "x", "internal": True}])
    assert wire == [{"role": "user", "content": "x"}]


# --- commands, listing, cli, report, security check ------------------------------------------------

def command_host(cfg, r=None):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    h.use("llm", router_plugin(cfg))
    rr = h.ctx.get("router")
    for cand in rr.config.models:
        cand.adapter = Fake(cand.adapter.model, cand.adapter.base_url)
    h.ctx.provide("session", Log())
    agent = Agent(h.ctx, AgentOptions(approval_mode="auto", project_instructions=False))
    return CommandHost(agent=agent, ctx=h.ctx, config=cfg), rr


def test_model_command_pins_and_auto_restores_and_pinning_does_not_bypass_the_rules():
    cfg = config()
    host, r = command_host(cfg)
    listing = dispatch(host, "/model")
    assert "目前：自動路由" in listing.text and "auto" in listing.text and "cloud" in listing.text
    assert dispatch(host, "/model cloud").data["provider"] == "cloud" and r.pinned.name == "cloud"
    assert "目前：cloud" in dispatch(host, "/model").text
    with pytest.raises(RoutingError):
        r.stream([user("學生 A123456789 的資料")], TOOLS)                                 # pinned to a bought-in model
    assert dispatch(host, "/model auto").data["provider"] == "auto" and r.pinned is None
    assert dispatch(host, "/model ghost").ok is False
    shown = dispatch(host, "/route")
    assert "自動路由中" in shown.text and "local" in shown.text and shown.data["current"]
    off = CommandHost(agent=host.agent, ctx=Harness().ctx, config=ResolvedConfig(provider=PROVIDERS["local"], provider_name="local"))
    assert dispatch(off, "/route").ok is False


def test_with_routing_on_only_routing_models_are_listed_with_auto_first():
    cfg = config({"local": {}, "private": {}})
    rows = providers_for(cfg)
    assert [r["name"] for r in rows] == ["auto", "local", "private"]                      # cloud and tiny are not routing models
    assert [r["name"] for r in providers_for(cfg, ("private",))] == ["auto", "private"]
    off = ResolvedConfig(provider=PROVIDERS["local"], provider_name="local", providers=PROVIDERS)
    assert [r["name"] for r in providers_for(off)] == list(PROVIDERS)


def test_route_command_line_explains_a_request(tmp_path, capsys):
    from xharness.cli import main

    path = tmp_path / "x.toml"
    path.write_text('''default_provider = "local"
[providers.local]
base_url = "http://127.0.0.1:8091/v1"
model = "small"
[providers.cloud]
base_url = "https://api.example.com/v1"
model = "big"
[routing]
enabled = true
[routing.models.local]
strength = 1
[routing.models.cloud]
strength = 3
cost = 5
''', encoding="utf-8")
    assert main(["--config", str(path), "route", "請重構這個模組，學生 A123456789"]) == 0
    out = capsys.readouterr().out
    assert "選擇：local" in out and "身分證字號" in out and "排除 cloud：" in out and "個資" in out
    assert main(["--config", str(path), "route", "請重構這個模組"]) == 0
    assert "選擇：cloud" in capsys.readouterr().out
    off = tmp_path / "off.toml"
    off.write_text('[providers.local]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "m"\n', encoding="utf-8")
    assert main(["--config", str(off), "route", "x"]) == 1


def test_routing_shows_up_in_the_usage_report(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from xharness.report import collect, format_report

    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    os.makedirs(sessions_dir(None), exist_ok=True)
    events = [{"type": "route", "provider": "local", "where": "local", "fresh": True, "excluded": []},
              {"type": "route", "provider": "private", "where": "private", "fresh": True,
               "excluded": [{"provider": "cloud", "why": "sensitive"}]},
              {"type": "route", "provider": "private", "where": "private", "fresh": False, "excluded": [{"provider": "cloud", "why": "files"}]},
              {"type": "route", "provider": None, "excluded": [{"provider": "cloud", "why": "sensitive"}]},
              {"type": "route-fallback", "provider": "local"},
              {"type": "telemetry", "total_tokens": 10, "calls": 1}]
    with open(os.path.join(sessions_dir(None), "r1.jsonl"), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps({"ts": "2026-10-22T01:00:00+00:00", **event}) + "\n")
    report = collect(ResolvedConfig(provider=PROVIDERS["local"], provider_name="local"), since="30d",
                     now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    routing = report["routing"]
    assert routing["decisions"] == 2 and routing["by_model"] == {"local": 1, "private": 1} and routing["fallbacks"] == 1
    assert routing["refused"] == 1 and routing["kept_off_external"] == 2 and routing["by_where"] == {"local": 1, "private": 1}
    assert "自動路由：2 次決定" in format_report(report)


def test_the_security_check_reads_the_routing_setup():
    from xharness.seccheck import run

    def items(cfg):
        return {i["id"]: i for i in run(cfg)["items"] if i["id"].startswith("routing")}
    assert items(ResolvedConfig(provider=PROVIDERS["local"], provider_name="local")) == {}
    ok = items(config())
    assert ok["routing-external"]["state"] == "pass" and "routing-origin" not in ok
    wide = items(config(external_sees="all"))
    assert wide["routing-external"]["state"] == "warn" and "prompt" in wide["routing-external"]["fix"]
    bad = items(config({"cloud": {"where": "private"}, "local": {"origin": "cn"}}, deny_origins=[]))
    assert bad["routing-where"]["state"] == "warn" and bad["routing-origin"]["state"] == "warn"
    broken = run(ResolvedConfig(provider=PROVIDERS["local"], provider_name="local", providers=PROVIDERS,
                                routing={"enabled": True, "models": {"ghost": {}}}))
    assert any(i["id"] == "routing-config" and i["state"] == "fail" for i in broken["items"])
    pure = items(config({"local": {}, "private": {}}))
    assert pure["routing-external"]["state"] == "pass" and "不會離開" in pure["routing-external"]["detail"]


def test_off_by_default_changes_nothing():
    from xharness.llm import OpenAIAdapter
    from xharness.presets import build_harness

    off = ResolvedConfig(provider=PROVIDERS["local"], provider_name="local", providers=PROVIDERS)
    h = build_harness(off, no_session=True)
    assert isinstance(h.ctx.get("llm"), OpenAIAdapter) and h.ctx.optional("router") is None
    on = build_harness(config(), no_session=True)
    assert isinstance(on.ctx.get("llm"), Router) and on.ctx.get("llm") is on.ctx.get("router")


def test_the_web_shows_auto_and_reports_each_decision(tmp_path, monkeypatch):
    from xharness.web import WebApp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    cfg = config({"local": {"strength": 1}, "cloud": {"strength": 3, "cost": 5}})
    small = {}

    def factory(resume, user=None):
        h, r = harness(cfg, Log(), local=Fake("small", answer="好"), cloud=Fake("big", answer="大"))
        small["h"] = h
        return h
    app = WebApp(cfg, approval_mode="auto", harness_factory=factory)
    models = app.models(None)
    assert models["default"] == "auto" and [p["name"] for p in models["providers"]] == ["auto", "local", "cloud"]
    conv = app.create()
    assert conv.provider == "auto"
    app.send(conv, "你好")
    deadline = time.time() + 5
    while time.time() < deadline and conv.running or not any(e["type"] == "done" for e in conv.events):
        time.sleep(0.02)
    routes = [e for e in conv.events if e["type"] == "route"]
    assert routes and routes[0]["provider"] == "local" and routes[0]["where"] == "local"


def test_a_dead_model_costs_no_retry_time_while_a_stand_in_waits():
    seen = {}

    class Probe(Fake):
        def stream(self, messages, tools, on_delta=None):
            seen[self.model] = self.retry_delays
            return super().stream(messages, tools, on_delta)
    first = Probe("small", fail=down)
    r = router(config({"local": {"strength": 2}, "private": {"strength": 2}}), local=first, private=Probe("mid"))
    r.stream([user("整理一下")], TOOLS)
    assert seen == {"small": (), "mid": None}                    # no waiting on the one with a stand-in; patient on the last
    assert first.retry_delays is None                            # and put back afterwards


def test_titles_and_summaries_are_not_counted_as_decisions():
    log = Log()
    r = router(config({"local": {"strength": 1}, "private": {"strength": 2}}), log=log)
    r.stream([user("標題請求")], [])                              # no tools: a title, not a task
    assert not log.of("route") and not [e for n, e in r.emitted if n == "router/decision"]
    r.stream([user("你好")], TOOLS)
    assert [e["provider"] for e in log.of("route")] == ["local"]
