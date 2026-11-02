"""The Telegram group Source, the chat-length rule, and the resident agent's HTTP surface."""

from __future__ import annotations

import pytest

from xharness.superagent import api
from xharness.superagent import telegram_source as tg
from xharness.superagent.runtime import Superagent
from xharness.telegram import TelegramBridge


class Bridge:
    def __init__(self) -> None:
        self.intercept = None
        self.sent: list[dict] = []

    def api(self, method: str, params: dict) -> dict:
        if method == "getMe":
            return {"id": 42, "username": "xcloud_bot"}
        self.sent.append(params)
        return {"message_id": 500 + len(self.sent)}


class Host:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    def telegram(self) -> Bridge:
        return self.bridge

    def open_conversation(self, *a, **k):
        raise AssertionError("no conversation in these tests")

    def approve(self, summary: str) -> bool:
        return True

    def notify(self, event: str, text: str) -> None:
        pass


def message(mid: int, text: str, user: int = 7, reply_to: dict | None = None, chat: int = -100) -> dict:
    out = {"message_id": mid, "date": 1_800_000_000, "text": text, "chat": {"id": chat, "type": "supergroup"},
           "from": {"id": user, "is_bot": False, "first_name": "Ann"}}
    if reply_to:
        out["reply_to_message"] = reply_to
    return out


@pytest.fixture()
def agent(tmp_path):
    bridge = Bridge()
    sa = Superagent(str(tmp_path), Host(bridge))
    sa.save_settings({"sources": {"tg": {"kind": "telegram", "config": {"chats": [{"id": -100, "name": "研發群"}]}}},
                      "policy": {"sources": {"tg": {"allowed": [{"id": "7", "name": "Ann"}]}}}})
    sa.sources["tg"].poll()
    yield sa, bridge
    sa.stop()


def test_a_mention_in_a_listed_group_is_an_explicit_request_and_a_reply_chain_is_one_thread(agent):
    sa, bridge = agent
    assert bridge.intercept is not None
    first = message(10, "@xcloud_bot 幫我把 README 的錯字改掉")
    assert bridge.intercept(first) is True
    second = message(11, "還有標題", reply_to=message(10, "x"))
    bridge.intercept(second)
    triggers = sorted(sa.store.recent_triggers("", 10), key=lambda t: t["text"])
    assert {t["threads"][0]["id"] for t in triggers} == {"-100:10"}
    assert triggers[0].get("addressed") == "mention" or triggers[1].get("addressed") == "mention"
    sa.engine.evaluate_due()
    candidate = sa.engine.candidate(next(t["id"] for t in triggers if t.get("addressed")))
    assert candidate and candidate["strength"] == "explicit"


def test_other_chats_and_direct_messages_stay_with_the_bridge(agent):
    sa, bridge = agent
    assert bridge.intercept(message(1, "hello", chat=-999)) is False
    direct = message(2, "hi")
    direct["chat"]["type"] = "private"
    assert bridge.intercept(direct) is False


def test_a_reply_goes_to_the_message_that_asked_in_its_topic(agent):
    sa, bridge = agent
    source = sa.sources["tg"]
    status = source.send({"id": "m1", "text": "好了，改完了。", "channel": {"source": "tg", "thread": "tg:-100:10",
                                                                     "address": {"chat": "-100", "reply_to": "10", "topic": "5"}}})
    assert status == "sent"
    assert bridge.sent[-1]["reply_parameters"]["message_id"] == 10 and bridge.sent[-1]["message_thread_id"] == 5


def test_a_chat_reply_over_100_words_is_refused(agent):
    sa, bridge = agent
    bridge.intercept(message(20, "@xcloud_bot 請幫忙"))
    trigger = sa.store.recent_triggers("", 1)[0]
    with pytest.raises(ValueError):
        sa.check_length({"purpose": "answer", "triggerId": trigger["id"], "text": "字" * 260})
    sa.check_length({"purpose": "answer", "triggerId": trigger["id"], "text": "好了，已經改完。"})
    assert tg.words("改好了 done") == 3


def test_the_bridge_hands_group_messages_to_the_resident_agent_first(tmp_path):
    taken: list[dict] = []
    bridge = TelegramBridge(object(), {"allowed_chats": [1]}, api=lambda method, params: {})
    bridge.intercept = lambda m: taken.append(m) or True
    bridge.handle({"update_id": 1, "message": message(3, "@bot hi")})
    assert taken and taken[0]["message_id"] == 3


def test_the_http_surface_lists_settles_and_guards(agent):
    sa, bridge = agent
    status, overview = api.get(sa, [], {})
    assert status == 200 and overview["person"] == [] and "tg" in overview["sources"]
    status, settings = api.get(sa, ["settings"], {})
    assert settings["versions"][0]["revision"] == 1
    bridge.intercept(message(30, "@xcloud_bot 改錯字"))
    sa.engine.evaluate_due()
    trigger = sa.store.recent_triggers("", 1)[0]
    ticket_id = sa.engine.decide(trigger["id"], {"kind": "accept", "title": "改錯字", "scope": "README", "reason": "asked"})["ticketId"]
    assert api.get(sa, ["ticket", ticket_id[:10]], {})[1]["title"] == "改錯字"
    assert api.get(sa, ["threads"], {})[1]["threads"][0]["triggers"][0]["where"] == "accept"
    assert api.post(sa, {"action": "stop", "ticket": ticket_id}, "admin")[0] == 200
    assert sa.store.ticket(ticket_id)["ended"]["by"] == "human"
    assert api.post(sa, {"action": "retry", "ticket": ticket_id}, "admin")[0] == 400
    assert api.post(sa, {"action": "save", "settings": {"eval": {"mode": "sometimes"}}}, "admin")[0] == 400
    assert api.post(sa, {"action": "pause"}, "admin")[1]["paused"] is True


def test_the_security_check_warns_when_writes_skip_approval(agent):
    from xharness.seccheck import _superagent_items
    sa, bridge = agent
    items = {i["id"]: i for i in _superagent_items(sa)}
    assert items["superagent-writes"]["state"] == "pass"
    sa.save_settings({**sa.settings, "writes": "auto"})
    assert {i["id"]: i for i in _superagent_items(sa)}["superagent-writes"]["state"] == "warn"


def test_ready_needs_a_pull_request_once_the_code_changed(tmp_path):
    import subprocess

    from xharness.superagent import github as gh

    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args], cwd=cwd, check=True, capture_output=True)
    origin, work = tmp_path / "origin", tmp_path / "work"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "README.md").write_text("Instalation\n")
    git("add", ".", cwd=origin)
    git("commit", "-qm", "init", cwd=origin)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)

    sa = Superagent(str(tmp_path / "home"), Host(Bridge()), sources={})
    sa.sources["gh"] = type("GH", (), {"kind": "github", "id": "gh", "config": {"defaultBranch": "main"},
                                       "scope": lambda self, m: {"listened": True, "observed": False, "row": "r"},
                                       "thread_ref": lambda self, n: {"source": "gh", "id": str(n)}})()
    sa.settings["policy"]["sources"]["gh"] = {"allowed": [{"id": "a", "name": ""}], "bots": [], "admission": {}}
    sa.engine.receive({"id": "r", "source": "gh", "threads": [{"source": "gh", "id": "1"}], "actor": {"source": "gh", "id": "a"},
                       "human": True, "addressed": "mention", "at": "2099-01-01T00:00:00Z", "text": "fix", "meta": {}})
    sa.engine.evaluate_due()
    ticket_id = sa.engine.decide("r", {"kind": "accept", "title": "t", "scope": "s", "reason": "asked"})["ticketId"]
    ticket = sa.store.ticket(ticket_id)
    ticket["execution"] = {"name": "fix", "directory": str(work)}
    sa.store.save_ticket(ticket)

    sa.ready_check(ticket_id)                                   # nothing changed: ready is allowed
    (work / "README.md").write_text("Installation\n")
    with pytest.raises(ValueError, match="uncommitted"):
        sa.ready_check(ticket_id)
    git("commit", "-qam", "fix", cwd=work)
    with pytest.raises(ValueError, match="no pull request"):
        sa.ready_check(ticket_id)
    sa.engine.bind_thread(ticket_id, {"source": "gh", "id": "2"}, own=True)
    sa.ready_check(ticket_id)
    assert gh.git(["log", "--oneline", "origin/main..HEAD"], str(work)).strip()


def test_removing_the_telegram_source_hands_group_messages_back_to_the_bridge(agent):
    sa, bridge = agent
    assert bridge.intercept is not None
    sa.save_settings({**sa.settings, "sources": {}})
    assert bridge.intercept is None


def test_a_reply_to_the_bots_answer_continues_the_original_thread(agent):
    sa, bridge = agent
    bridge.intercept(message(10, "@xcloud_bot 幫我把 README 的錯字改掉"))
    source = sa.sources["tg"]
    status = source.send({"channel": {"source": "tg", "thread": "tg:-100:10", "address": {"chat": "-100", "reply_to": "10"}},
                          "text": "好，處理中"})
    assert status == "sent"
    bot_message = {**message(501, "好，處理中", user=42), "from": {"id": 42, "is_bot": True}}
    bridge.intercept(message(12, "標題也改", reply_to=bot_message))
    threads = {t["threads"][0]["id"] for t in sa.store.recent_triggers("", 10)}
    assert threads == {"-100:10"}
