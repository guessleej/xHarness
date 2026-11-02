"""The resident agent end to end, with a fake GitHub and fake conversations: an issue that mentions the agent
becomes a ticket, its worker acknowledges and reports, the main Session replies, and every write waits for approval."""

from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from xharness.superagent import github as gh
from xharness.superagent.model import key
from xharness.superagent.runtime import Superagent
from xharness.tools import ToolContext


class FakeClient(gh.Client):
    def __init__(self) -> None:
        super().__init__("https://api.github.com", lambda: "ghp_" + "x" * 36)
        self.routes: dict[str, Any] = {}
        self.posts: list[tuple[str, dict]] = []

    def request(self, method: str, path: str, body: Any = None, params: Any = None) -> tuple[Any, dict[str, str]]:
        if method == "POST":
            self.posts.append((path, body))
            return {"id": len(self.posts), "number": 99, "html_url": "https://github.com/acme/app/pull/99"}, {}
        for prefix, value in self.routes.items():
            if path.startswith(prefix):
                return (value(params) if callable(value) else value), {}
        return [], {}


class FakeHandle:
    def __init__(self, name: str, tools: list, script: Callable[["FakeHandle", str], str]) -> None:
        self.id = name
        self.tools = {t.name: t for t in tools}
        self.script = script
        self.seen: list[str] = []

    def call(self, name: str, **args: Any) -> str:
        result = self.tools[name].execute(args, ToolContext(cwd=".", approve=lambda _s: True))
        assert not result.is_error, result.output
        return result.output

    def run(self, text: str) -> str:
        self.seen.append(text)
        return self.script(self, text)

    def cancel(self) -> None:
        pass


class FakeHost:
    def __init__(self) -> None:
        self.approvals: list[str] = []
        self.allow = True
        self.handles: dict[str, FakeHandle] = {}
        self.main_script: Callable[[FakeHandle, str], str] = lambda h, t: "nothing"
        self.worker_script: Callable[[FakeHandle, str], str] = lambda h, t: "nothing"

    def open_conversation(self, name: str, cwd: str, system: str, tools: list, model: str = "", role: str = "worker") -> FakeHandle:
        script = self.main_script if name.startswith("resident") else self.worker_script
        handle = FakeHandle(name, tools, script)
        self.handles[name] = handle
        return handle

    def approve(self, summary: str) -> bool:
        self.approvals.append(summary)
        return self.allow

    def notify(self, event: str, text: str) -> None:
        pass


def issue(number: int, login: str, body: str) -> dict:
    return {"number": number, "title": "Typo in README", "body": body, "user": {"login": login, "type": "User"},
            "created_at": "2099-01-01T00:00:00Z", "html_url": f"https://github.com/acme/app/issues/{number}", "state": "open"}


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    client = FakeClient()
    host = FakeHost()
    monkeypatch.setattr(gh, "Client", lambda api, token: client)
    sa = Superagent(str(tmp_path), host, token_reader=lambda sid, config: "ghp_" + "y" * 36)
    sa.save_settings({"sources": {"gh": {"kind": "github", "config": {"repository": "acme/app", "login": "xcloud-agent",
                                                                       "defaultBranch": "main"}}},
                      "policy": {"sources": {"gh": {"allowed": [{"id": "alice"}]}}}})
    monkeypatch.setattr(sa, "prepare_directory", lambda ticket: str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    client.routes = {
        "notifications": [{"repository": {"full_name": "acme/app"}, "subject": {"url": "https://api.github.com/repos/acme/app/issues/7"}}],
        "repos/acme/app/issues/7/comments": [],
        "repos/acme/app/issues/7": issue(7, "alice", "@xcloud-agent please fix the typo in the install section"),
    }
    yield sa, client, host
    sa.stop()


def settle(sa: Superagent, rounds: int = 40) -> None:
    for _ in range(rounds):
        sa.tick()
        time.sleep(0.02)
        while sa.main_busy or any(w.busy or not w.inbox.empty() for w in sa.workers.values()):
            time.sleep(0.02)
        for effect in sa.store.effects():
            if effect["due"] > time.time():
                sa.store.effect_defer(effect["id"], 0)


def test_a_mention_becomes_a_ticket_and_the_result_goes_back_after_approval(agent):
    sa, client, host = agent

    def main(handle: FakeHandle, text: str) -> str:
        if "candidate" in text:
            trigger = sa.store.recent_triggers("", 10)[0]["id"][:12]
            handle.call("superagent_route", decisions=[{"trigger": trigger, "kind": "accept", "title": "Fix README typo",
                                                        "name": "readme-typo", "scope": "the install section typo",
                                                        "acknowledgement": "On it: fixing the typo."}])
        if "report from" in text and "ready" in text:
            ticket = sa.store.tickets(limit=1)[0]["id"][:12]
            handle.call("superagent_reply", purpose="result", ticket=ticket, text="Fixed the typo; PR is up for review.")
        return "done"

    def worker(handle: FakeHandle, text: str) -> str:
        if "Assignment:" in text:
            handle.call("ticket_report", state="ready", summary="Typo fixed and verified.")
        return "finished"

    host.main_script, host.worker_script = main, worker
    settle(sa)
    tickets = sa.store.tickets()
    assert len(tickets) == 1 and tickets[0]["ended"]["outcome"] == "completed"
    assert tickets[0]["execution"]["name"] == "readme-typo"
    bodies = [body["body"] for path, body in client.posts]
    assert any("On it: fixing the typo." in b for b in bodies)            # the acknowledgement
    assert any("Fixed the typo" in b for b in bodies)                     # the result
    assert all("<!-- xharness:" in b for b in bodies)                     # idempotence markers
    assert len(host.approvals) == 2                                        # every write was approved


def test_a_declined_write_is_dropped_and_never_sent(agent):
    sa, client, host = agent
    host.allow = False

    def main(handle: FakeHandle, text: str) -> str:
        if "candidate" in text:
            trigger = sa.store.recent_triggers("", 10)[0]["id"][:12]
            handle.call("superagent_route", decisions=[{"trigger": trigger, "kind": "accept", "title": "Fix", "name": "fix",
                                                        "scope": "typo", "acknowledgement": "On it."}])
        return "ok"

    host.main_script = main
    settle(sa)
    assert client.posts == []
    message = sa.store.outbox_get(key("ack", sa.store.recent_triggers("", 10)[0]["id"]))
    assert message["status"] == "dropped" and message["reason"] == "denied"


def test_candidates_that_arrive_while_the_main_session_is_busy_go_in_one_turn(agent):
    sa, client, host = agent
    turns: list[str] = []
    host.main_script = lambda handle, text: turns.append(text) or "ok"
    sa.main_busy = True
    sa.engine.receive({"id": "a", "source": "gh", "threads": [{"source": "gh", "id": "1"}], "actor": {"source": "gh", "id": "alice"},
                       "human": True, "addressed": "mention", "at": "2099-01-01T00:00:00Z", "text": "one", "meta": {"row": "comments"}})
    sa.engine.receive({"id": "b", "source": "gh", "threads": [{"source": "gh", "id": "2"}], "actor": {"source": "gh", "id": "alice"},
                       "human": True, "addressed": "mention", "at": "2099-01-01T00:00:00Z", "text": "two", "meta": {"row": "comments"}})
    sa.engine.evaluate_due()
    sa.run_effects()
    assert turns == []
    sa.main_busy = False
    sa.run_effects()
    time.sleep(0.1)
    assert len(turns) == 1 and turns[0].startswith("2 new item(s)")


def test_the_token_never_appears_in_settings_or_errors(agent, tmp_path):
    sa, client, host = agent
    secret = "ghp_" + "z" * 36
    sa.save_settings({**sa.settings, "sources": {"gh": {"kind": "github", "config": {
        "repository": "acme/app", "login": "xcloud-agent", "token": secret}}}})
    assert secret not in str(sa.public_settings()) and sa.public_settings()["sources"]["gh"]["tokenSet"]
    assert secret not in str(sa.store.kv("settings"))
    assert gh.scrub(f"failed with {secret}") == "failed with [redacted]"


def test_a_ci_failure_on_a_subscribed_pull_request_reaches_its_worker_directly(agent):
    sa, client, host = agent
    sa.engine.receive({"id": "r", "source": "gh", "threads": [{"source": "gh", "id": "5"}], "actor": {"source": "gh", "id": "alice"},
                       "human": True, "addressed": "mention", "at": "2099-01-01T00:00:00Z", "text": "fix it",
                       "channel": {"source": "gh", "thread": "gh:5", "address": {"number": "5"}}, "meta": {"row": "comments"}})
    sa.engine.evaluate_due()
    ticket_id = sa.engine.decide("r", {"kind": "accept", "title": "t", "scope": "s", "reason": "asked"})["ticketId"]
    source = sa.sources["gh"]
    client.routes = {"repos/acme/app/issues/5/comments": [], "repos/acme/app/issues/5": {**issue(5, "alice", ""), "pull_request": {"url": "x"}},
                     "repos/acme/app/pulls/5/comments": [], "repos/acme/app/pulls/5/reviews": [],
                     "repos/acme/app/pulls/5": {"state": "open", "head": {"sha": "abc123"}, "html_url": "u"},
                     "repos/acme/app/commits/abc123/check-runs": {"check_runs": [
                         {"name": "unit", "status": "completed", "conclusion": "failure"}]}}
    source._subscribed()
    ticket = sa.store.ticket(ticket_id)
    ci = [sa.store.trigger(i["triggerId"]) for i in ticket["inputs"] if i["kind"] == "trigger"]
    assert any("CI failed" in t["text"] for t in ci)
    source._subscribed()                                    # the same CI result is not stored twice
    assert len([i for i in sa.store.ticket(ticket_id)["inputs"] if i["kind"] == "trigger"]) == len(ci)
