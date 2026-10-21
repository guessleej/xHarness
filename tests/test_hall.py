"""1.15: halls -- team-against-team discussions across conversations and machines."""

import json
import threading
import time

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.discuss import discussion_plugin
from xharness.hall import HallClient, HallRegistry, parse_invite
from xharness.roles import roles_plugin
from xharness.telemetry import telemetry_plugin
from xharness.tools import ToolContext, tools_plugin
from xharness.web import start_server

from test_agents import Log, Router


def says(private, public):
    return lambda messages, tools: public if "public turn" in str(messages[-1]["content"]) else private


def harness(llm, log=None):
    h = Harness()
    h.use("tools", tools_plugin)
    h.use("telemetry", telemetry_plugin())
    h.use("roles", roles_plugin({}))
    h.use("discussion", discussion_plugin({"join_minutes": 0.1, "turn_minutes": 0.5}))
    h.ctx.provide("llm", llm)
    if log is not None:
        h.ctx.provide("session", log)
    return h


@pytest.fixture()
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path / "home"))
    llm = Router([("You are 甲1,", says("甲組內：先看詢問人次", "甲代表")),
                  ("You are 甲2,", says("甲組內：同意", "甲組公開：支持開夜間班")),
                  ("closing summary", "各組最後的立場：甲組支持；乙組反對")])
    log = Log()
    holder = {}

    def factory(resume, user=None):
        holder["h"] = harness(llm, log)
        return holder["h"]
    config = ResolvedConfig(provider={"base_url": "http://x/v1", "model": "fake"}, provider_name="fake")
    server, app, url = start_server(config, port=0, approval_mode="auto", harness_factory=factory)
    config.web = {"public_url": url}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, url, llm, log, holder
    server.shutdown()


def run_host(app, holder, tmp_path, teams, result):
    conv = app.create()
    tool = conv.harness.ctx.get("tools").get("discussion")
    result["conv"] = conv
    result["out"] = tool.execute({"topic": "要不要開夜間班", "mode": "teams", "rounds": 2, "teams": teams},
                                 ToolContext(cwd=str(tmp_path), approve=lambda s: True))


def wait_for(predicate, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_team_joins_from_another_conversation_over_http(host, tmp_path):
    app, url, host_llm, host_log, holder = host
    result = {}
    worker = threading.Thread(target=run_host, args=(app, holder, tmp_path, [
        {"name": "甲組", "members": ["甲1", "甲2"], "spokesperson": "甲2"}, {"name": "乙組", "remote": True}], result))
    worker.start()
    conv = wait_for(lambda: result.get("conv"))
    invite = wait_for(lambda: next((e["invite"] for e in conv.events if e.get("state") == "invite"), None))
    assert invite.startswith("xhall:") and parse_invite(invite)["u"] == url.rstrip("/")

    guest_llm = Router([("You are 乙1,", says("乙組內：成本太高", "乙代表")),
                        ("You are 乙2,", says("乙組內：同意", "乙組公開：反對開夜間班"))])
    guest_log = Log()
    guest = harness(guest_llm, guest_log)
    out = guest.ctx.get("tools").get("discussion_join").execute(
        {"invite": invite, "team": "乙組", "members": ["乙1", "乙2"], "spokesperson": "乙2"},
        ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    worker.join(30)
    assert not out.is_error, out.output
    assert not result["out"].is_error, result["out"].output

    host_turns = [(t["round"], t["team"], t["scope"], t["speaker"]) for t in host_log.of("discussion-turn")]
    assert (1, "乙組", "plenary", "乙2") in host_turns and (2, "乙組", "plenary", "乙2") in host_turns
    remote = [t for t in host_log.of("discussion-turn") if t["team"] == "乙組"]
    assert all(t["scope"] == "plenary" and t.get("remote") for t in remote) and remote[0]["text"] == "乙組公開：反對開夜間班"
    # Internal talk stays on its own side, in both directions.
    host_text = json.dumps(host_log.events, ensure_ascii=False) + json.dumps(host_llm.calls, ensure_ascii=False)
    guest_text = json.dumps(guest_log.events, ensure_ascii=False) + json.dumps(guest_llm.calls, ensure_ascii=False)
    assert "乙組內" not in host_text and "甲組內" not in guest_text
    assert "甲組公開：支持開夜間班" in guest_text            # public statements cross
    # The guest's second-round internal talk had heard the first-round public statements.
    later = [c for c in guest_llm.asked("You are 乙1,")][-1]["messages"]
    assert any("甲組公開" in str(m.get("content")) for m in later if m["role"] == "user")
    # Both sides end with the host's summary; the invite never reached the session log.
    assert "甲組支持" in host_log.of("discussion-end")[0]["summary"] and "甲組支持" in out.output
    assert parse_invite(invite)["s"] not in host_text
    # The door closed when the discussion started.
    late = HallClient(parse_invite(invite))
    with pytest.raises(RuntimeError, match="no longer taking teams"):
        late.join("丙組", ["丙1"], "丙1", "", "")


def test_nobody_joins_in_time(host, tmp_path):
    app, url, host_llm, host_log, holder = host
    result = {}
    run_host(app, holder, tmp_path, [{"name": "甲組", "members": ["甲1"]}, {"name": "乙組", "remote": True}], result)
    assert "加入的組別不足兩組" in result["out"].output
    assert any("沒有在時限內加入" in n["text"] for n in host_log.of("discussion-note"))


def test_hall_credentials_are_scoped():
    reg = HallRegistry()
    hall = reg.create("t", 1, "", "http://h", ["乙組"], [{"name": "甲組", "members": ["a"]}], 60)
    other = reg.create("t", 1, "", "http://h", ["X"], [], 60)
    assert reg.handle("POST", hall.id, "join", "wrong", {"team": "乙組", "members": ["b"]}, {})[0] == 403
    assert reg.handle("POST", hall.id, "join", hall.secret, {"team": "乙組", "members": []}, {})[0] == 400
    status, joined = reg.handle("POST", hall.id, "join", hall.secret, {"team": "乙組", "members": ["b"]}, {})
    assert status == 200 and joined["token"] and joined["token"] != hall.secret
    token = joined["token"]
    assert reg.handle("GET", other.id, "next", token, {}, {"wait": ["0"]})[0] == 401        # a team token opens one hall
    assert reg.handle("GET", hall.id, "next", hall.secret, {}, {"wait": ["0"]})[0] == 401   # the invite cannot act as a team
    assert reg.handle("POST", hall.id, "statement", token, {"round": 1, "text": "x"}, {})[0] == 409  # not asked yet
    hall.start()
    hall.ask("乙組", 1)
    nxt = reg.handle("GET", hall.id, "next", token, {}, {"wait": ["0"]})[1]
    assert nxt["state"] == "turn" and nxt["round"] == 1
    assert reg.handle("POST", hall.id, "statement", token, {"round": 2, "text": "x"}, {})[0] == 409  # wrong round
    assert reg.handle("POST", hall.id, "statement", token, {"round": 1, "text": "y" * 99_999}, {})[0] == 200
    assert len(hall.statement("乙組", 1)["text"]) == 6_000                                    # capped
    assert reg.handle("POST", hall.id, "statement", token, {"round": 1, "text": "again"}, {})[0] == 409  # once a round
    assert reg.handle("POST", hall.id, "join", hall.secret, {"team": "乙組", "members": ["b"]}, {})[0] == 409
    assert "token" not in json.dumps(hall.public_view())                                     # no secrets in views


def test_one_open_seat_takes_the_joining_teams_own_name():
    reg = HallRegistry()
    hall = reg.create("t", 1, "", "http://h", ["另一組"], [{"name": "甲組", "members": ["a"]}], 60)
    status, joined = reg.handle("POST", hall.id, "join", hall.secret, {"team": "學務組", "members": ["b"]}, {})
    assert status == 200 and joined["team"] == "學務組" and "學務組" in hall.seats
    reg2 = HallRegistry()
    hall2 = reg2.create("t", 1, "", "http://h", ["A", "B"], [], 60)
    assert reg2.handle("POST", hall2.id, "join", hall2.secret, {"team": "C", "members": ["c"]}, {})[0] == 409


def test_bad_invites_are_explained():
    for code, why in [("hello", "starts with xhall:"), ("xhall:!!!", "damaged"),
                      ("xhall:eyJ1IjoiZmlsZTovL3gifQ", "incomplete")]:
        with pytest.raises(ValueError, match=why):
            parse_invite(code)
    import base64
    bad = "xhall:" + base64.urlsafe_b64encode(json.dumps({"u": "file:///etc", "h": "a", "s": "b"}).encode()).decode()
    with pytest.raises(ValueError, match="http"):
        parse_invite(bad)


def test_a_cli_conversation_cannot_host_but_says_why(tmp_path):
    log = Log()
    h = harness(Router([("You are", "說"), ("closing summary", "x")]), log)
    out = h.ctx.get("tools").get("discussion").execute(
        {"topic": "t", "teams": [{"name": "A", "members": ["a"]}, {"name": "B", "remote": True}]},
        ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    assert "只有網頁介面可以" in out.output


def test_a_team_without_members_is_told_how_to_be_remote(tmp_path):
    h = harness(Router([]))
    out = h.ctx.get("tools").get("discussion").execute(
        {"topic": "t", "mode": "teams", "teams": [{"name": "教務組", "members": ["正方"]},
                                                  {"name": "學務組", "members": [{"name": ""}]}]},
        ToolContext(cwd=str(tmp_path), approve=lambda s: True))
    assert out.is_error and '"remote": true' in out.output and "學務組" in out.output
