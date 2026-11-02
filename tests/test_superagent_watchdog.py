"""The Watchdog heals a dead worker, spots a loop and a stall, and tells a person once."""

from __future__ import annotations

import threading

from xharness.superagent.runtime import Superagent, Worker


class Handle:
    id = "w"

    def __init__(self) -> None:
        self.calls = ["bash:ls"] * 8

    def run(self, text: str) -> str:
        return ""

    def cancel(self) -> None:
        pass

    def recent_calls(self) -> list[str]:
        return self.calls


class Host:
    def __init__(self) -> None:
        self.notices: list[str] = []

    def open_conversation(self, *a, **k):
        return Handle()

    def approve(self, summary: str) -> bool:
        return True

    def notify(self, event: str, text: str) -> None:
        self.notices.append(text)


def running_ticket(sa: Superagent) -> str:
    sa.engine.receive({"id": "t", "source": "x", "threads": [{"source": "x", "id": "1"}], "actor": {"source": "x", "id": "a"},
                       "human": True, "addressed": "mention", "at": "2099-01-01T00:00:00Z", "text": "do it", "meta": {}})
    sa.sources["x"] = type("S", (), {"scope": lambda self, m: {"listened": True, "observed": False, "row": "r"},
                                     "health": {"state": "ready"}})()
    sa.settings["policy"]["sources"]["x"] = {"allowed": [{"id": "a", "name": ""}], "bots": [], "admission": {}}
    sa.engine.reevaluate("t")
    sa.engine.evaluate_due()
    ticket_id = sa.engine.decide("t", {"kind": "accept", "title": "Do it", "scope": "it", "reason": "asked"})["ticketId"]
    sa.engine.assignment_started(ticket_id, sa.store.ticket(ticket_id)["assignments"][0]["id"])
    return ticket_id


def test_a_dead_worker_is_launched_again_and_a_person_is_told_once(tmp_path):
    host = Host()
    sa = Superagent(str(tmp_path), host, sources={})
    ticket_id = running_ticket(sa)
    found = sa.watchdog.check()
    assert any(f["kind"] == "healed" for f in found)
    assert any(e["kind"] == "launch" and e["key"] == ticket_id for e in sa.store.effects())
    sa.watchdog.check()
    assert len(host.notices) == 1


def test_a_looping_or_stalled_worker_is_reported_and_asked_for_status(tmp_path):
    host = Host()
    sa = Superagent(str(tmp_path), host, sources={})
    ticket_id = running_ticket(sa)
    worker = Worker(sa, ticket_id, Handle(), str(tmp_path))
    sa.workers[ticket_id] = worker
    worker.busy = True
    ticket = sa.store.ticket(ticket_id)
    ticket["updatedAt"] = "2000-01-01T00:00:00.000Z"
    sa.store.save_ticket(ticket)
    kinds = {f["kind"] for f in sa.watchdog.check()}
    assert {"loop", "stall"} <= kinds
    assert "沒有進展" in host.notices[0] and "迴圈" in host.notices[0]
    worker.stop()
    assert isinstance(worker.thread, threading.Thread)
