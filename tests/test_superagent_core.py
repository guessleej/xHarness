"""The resident agent's domain core: intake, gate, Eval, tickets, subscriptions, reports, replies."""

from __future__ import annotations

import pytest

from xharness.superagent.engine import Engine
from xharness.superagent.model import UserError, key, thread_key, ticket_stage
from xharness.superagent.policy import Policy, assessment_outcome, policy_effects, validate
from xharness.superagent.store import Store


class Clock:
    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> str:
        self.t += 1
        return f"2026-11-02T00:{self.t // 60:02d}:{self.t % 60:02d}.000Z"


class Ports:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.released: list[str] = []
        self.started: list[tuple[str, str]] = []
        self.delivered: list[str] = []
        self.stopped: list[tuple[str, str]] = []
        self.sent: dict[str, dict] = {}

    # assessor
    def request(self, candidate: dict) -> None:
        self.requests.append(candidate)

    def release(self, eval_id: str) -> None:
        self.released.append(eval_id)

    # executor
    def start(self, ticket: dict, assignment: dict) -> None:
        self.started.append((ticket["id"], assignment["id"]))

    def deliver(self, ticket: dict, assignment: dict) -> None:
        self.delivered.append(ticket["id"])

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None:
        self.stopped.append((ticket["id"], reason))


class Outbox:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}

    def send(self, message: dict) -> None:
        self.messages.setdefault(message["id"], {**message, "status": "pending"})

    def status(self, message_id: str) -> str | None:
        return self.messages.get(message_id, {}).get("status")

    def withdraw(self, message_id: str) -> None:
        if self.messages.get(message_id, {}).get("status") == "pending":
            self.messages[message_id]["status"] = "dropped"


def scope(source: str, meta: dict) -> dict:
    if meta.get("ignored"):
        return {"listened": False, "reason": "outsideScope"}
    return {"listened": True, "observed": bool(meta.get("all")), "row": meta.get("row", "issues")}


@pytest.fixture()
def world(tmp_path):
    settings = validate({"policy": {"sources": {"gh": {"allowed": [{"id": "alice"}], "bots": [{"id": "ci[bot]"}]}}}})
    state = {"settings": settings}
    ports, outbox = Ports(), Outbox()
    store = Store(str(tmp_path / "sa.db"))
    engine = Engine(store, policy=lambda: Policy(state["settings"], scope), scope=lambda t: scope(t["source"], t.get("meta", {})),
                    assessor=ports, executor=ports, outbox=outbox, now=Clock())
    yield engine, ports, outbox, store, state
    store.close()


def trig(tid: str, actor: str = "alice", thread: str = "issue-1", human: bool = True, addressed: str | None = "mention",
         text: str = "please fix the typo", **meta) -> dict:
    t = {"id": tid, "source": "gh", "threads": [{"source": "gh", "id": thread}], "actor": {"source": "gh", "id": actor},
         "human": human, "at": "2026-11-02T00:00:00Z", "text": text, "meta": meta,
         "channel": {"source": "gh", "thread": thread, "address": {"issue": thread}}}
    if addressed:
        t["addressed"] = addressed
    return t


def accept(engine: Engine, trigger_id: str, **extra) -> dict:
    return engine.decide(trigger_id, {"kind": "accept", "title": "Fix typo", "scope": "the README typo", "reason": "asked",
                                      "acknowledgement": "On it.", **extra})


def test_a_trigger_is_stored_once_and_a_filtered_one_never_reaches_eval(world):
    engine, ports, _, store, _ = world
    assert engine.receive(trig("t1")) == "stored"
    assert engine.receive(trig("t1")) == "duplicate"
    assert engine.receive(trig("t2", ignored=True)) == "filtered"
    assert store.disposition("t2")["filtered"]["code"] == "outsideScope"
    assert store.thread("gh:issue-1")["pending"] == ["t1"]


def test_an_explicit_request_becomes_a_ticket_with_a_frozen_mandate(world):
    engine, ports, outbox, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    assert len(ports.requests) == 1 and ports.requests[0]["strength"] == "explicit"
    decision = accept(engine, "t1")
    ticket = store.ticket(decision["ticketId"])
    assert ticket["mandate"]["strength"] == "explicit" and ticket["mandate"]["basis"]["via"] == "addressed"
    assert ports.started == [(ticket["id"], ticket["assignments"][0]["id"])]
    assert outbox.status(key("ack", "t1")) == "pending"
    assert ticket["id"] in store.thread("gh:issue-1")["subscriptions"]
    assert store.thread("gh:issue-1").get("evaluating") is None
    assert ticket_stage(ticket) == {"stage": "working", "action": "queued"}


def test_a_follow_up_goes_straight_to_the_subscribed_worker_without_an_eval(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.assignment_started(ticket_id, store.ticket(ticket_id)["assignments"][0]["id"])
    engine.receive(trig("t2", actor="bob", addressed=None, text="also the second paragraph"))
    ticket = store.ticket(ticket_id)
    assert any(i["kind"] == "trigger" and i["triggerId"] == "t2" for i in ticket["inputs"])
    assert store.thread("gh:issue-1")["pending"] == []          # the worker decides it, no Eval
    assert ticket_id in ports.delivered


def test_ci_on_a_subscribed_thread_reaches_the_ticket_and_its_eval_is_ignored(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.receive(trig("ci1", actor="", human=False, addressed=None, text="unit tests failed"))
    assert any(i["triggerId"] == "ci1" for i in store.ticket(ticket_id)["inputs"] if i["kind"] == "trigger")
    engine.evaluate_due()
    decision = store.decision(store.disposition("ci1")["decisionId"])
    assert decision["kind"] == "ignore" and decision["reason"][0]["code"] == "not_human"


def test_an_unlisted_person_who_addresses_the_agent_is_ignored_by_default(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1", actor="mallory"))
    engine.evaluate_due()
    assert ports.requests == []
    assert store.decision(store.disposition("t1")["decisionId"])["reason"][0]["code"] == "not_a_member"
    with pytest.raises(UserError):
        accept(engine, "t1")


def test_observe_mode_records_what_it_would_accept_and_accepts_nothing(world):
    engine, ports, _, store, state = world
    settings = state["settings"]
    outcome = assessment_outcome({
        "decision": "accept", "title": "Fix typo", "scope": "README", "reason": "a typo was reported",
        "intent_evidence": "please fix", "rule_assessments": [{"rule_id": "copy", "matched": True, "reason": "typo"}],
        "direction": {"implementation_requested": True, "target_basis": "specified", "requires_design_decision": False,
                      "evidence": "fix the typo"}}, settings, "inferred")
    assert outcome["kind"] == "ignore" and outcome["audit"]["would_accept"] is True
    with pytest.raises(ValueError):
        assessment_outcome({"decision": "ignore", "rule_assessments": []}, settings, "inferred")


def test_a_listed_bot_is_inferred_at_most_and_needs_auto_mode_with_a_matched_rule(world):
    engine, ports, _, store, state = world
    engine.receive(trig("b1", actor="ci[bot]", human=False, text="@agent the build broke"))
    engine.evaluate_due()
    assert ports.requests[0]["strength"] == "inferred"
    with pytest.raises(UserError):
        accept(engine, "b1", rules=["copy"])               # observe mode
    state["settings"] = validate({**state["settings"], "eval": {"mode": "auto"}})
    # The Policy changed, so the Eval in progress is reassessed under the new version.
    engine.policy_changed("x", "y")
    engine.evaluate_due()
    decision = accept(engine, "b1", rules=["copy"])
    assert store.ticket(decision["ticketId"])["mandate"]["basis"]["via"] == "bot"


def test_a_ready_report_ends_the_ticket_and_a_followed_pull_request_waits_for_its_merge(world):
    engine, ports, outbox, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.assignment_started(ticket_id, store.ticket(ticket_id)["assignments"][0]["id"])
    engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-7"}, own=True)
    engine.report(ticket_id, {"state": "ready", "summary": "PR #7 opened"})
    assert store.ticket(ticket_id).get("ended") is None    # waits for the PR
    assert ticket_stage(store.ticket(ticket_id)) == {"stage": "person", "action": "review"}
    engine.thread_closed({"source": "gh", "id": "pr-7"}, "merged", "2026-11-02T01:00:00Z")
    assert store.ticket(ticket_id)["ended"]["outcome"] == "merged"
    reply = engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Merged."})
    assert outbox.messages[reply["id"]]["channel"]["thread"] == "issue-1"


def test_a_stopped_ticket_receives_nothing_and_cannot_be_attached_to(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.stop_ticket(ticket_id, "not needed")
    engine.receive(trig("t2", addressed=None, text="one more thing"))
    assert not any(i.get("triggerId") == "t2" for i in store.ticket(ticket_id)["inputs"])
    with pytest.raises(UserError):
        engine.attach(ticket_id, "t2")


def test_a_person_resumes_an_ended_ticket_under_its_mandate_without_an_eval(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.assignment_started(ticket_id, store.ticket(ticket_id)["assignments"][0]["id"])
    engine.report(ticket_id, {"state": "ready", "summary": "done"})
    assert store.ticket(ticket_id)["ended"]["outcome"] == "completed"
    engine.receive(trig("t3", addressed=None, text="the heading too please"))
    ticket = store.ticket(ticket_id)
    assert ticket.get("ended") is None and len(ticket["assignments"]) == 2
    assert ticket["inputs"][-1]["kind"] == "resumed"


def test_a_revoked_sender_cannot_resume_and_the_message_goes_to_eval(world):
    engine, ports, _, store, state = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.assignment_started(ticket_id, store.ticket(ticket_id)["assignments"][0]["id"])
    engine.report(ticket_id, {"state": "ready", "summary": "done"})
    state["settings"] = validate({**state["settings"], "policy": {"sources": {"gh": {"allowed": []}}}})
    engine.receive(trig("t4", addressed=None, text="and again"))
    assert store.ticket(ticket_id).get("ended")
    assert "t4" in store.thread("gh:issue-1")["pending"]


def test_an_expired_eval_is_requeued_twice_then_unavailable(world):
    engine, ports, _, store, state = world
    state["settings"]["eval"]["timeoutSeconds"] = 30
    engine.receive(trig("t1"))
    for _ in range(3):
        engine.evaluate_due()
        eval_id = store.thread("gh:issue-1")["evaluating"]["id"]
        engine.candidate_delivered(eval_id)
        thread = store.thread("gh:issue-1")
        thread["evaluating"]["deadline"] = "2000-01-01T00:00:00.000Z"
        store.save_thread(thread)
        engine.evaluate_due()
    decision = store.decision(store.disposition("t1")["decisionId"])
    assert decision["kind"] == "unavailable" and decision["reason"][0]["code"] == "timeout"


def test_a_restart_restarts_a_busy_assignment_with_a_recovery_note(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    first = store.ticket(ticket_id)["assignments"][0]["id"]
    engine.assignment_started(ticket_id, first)
    engine.assignment_interrupted(ticket_id, first)
    ticket = store.ticket(ticket_id)
    assert ticket["assignments"][0]["endedReason"] == "hostRestarted"
    assert ticket["inputs"][-1]["kind"] == "text" and "restarted" in ticket["inputs"][-1]["text"]


def test_a_question_waits_for_a_person_and_automatic_inputs_wait_behind_a_block(world):
    engine, ports, outbox, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.assignment_started(ticket_id, store.ticket(ticket_id)["assignments"][0]["id"])
    engine.report(ticket_id, {"state": "waiting", "summary": "which file?", "question": "README or docs?", "askedBy": "t1"})
    assert ticket_stage(store.ticket(ticket_id)) == {"stage": "person", "action": "answer"}
    engine.reply({"purpose": "attention", "ticketId": ticket_id, "question": "README or docs?", "text": "README or docs?"})
    engine.report(ticket_id, {"state": "blocked", "summary": "no access", "question": None})
    engine.receive(trig("ci2", actor="", human=False, addressed=None, text="lint failed"))
    ticket = store.ticket(ticket_id)
    ci_input = next(i for i in ticket["inputs"] if i.get("triggerId") == "ci2")
    assert ci_input["id"] not in ticket["undelivered"]
    assert ticket_stage(ticket) == {"stage": "person", "action": "blocked"}


def test_a_worker_hands_back_a_message_beyond_its_ticket(world):
    engine, ports, _, store, _ = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.receive(trig("t5", addressed=None, text="separately, can you redesign the homepage?"))
    assert engine.handback(ticket_id, "t5", "a different piece of work") is True
    assert "t5" in store.thread("gh:issue-1")["pending"]
    with pytest.raises(UserError):
        engine.handback(ticket_id, "t5", "again")


def test_policy_effects_name_the_rules_that_changed(world):
    engine, ports, _, store, state = world
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1", rules=["copy"])["ticketId"]
    old = state["settings"]
    rules = [{**r, "enabled": False} if r["id"] == "copy" else r for r in old["policy"]["rules"]]
    new = validate({**old, "policy": {**old["policy"], "rules": rules}})
    effects = policy_effects(store.tickets_open(), old, new, lambda s: scope)
    assert effects[ticket_id]["rules"][0]["change"] == "disabled"


def test_thread_key_and_key_are_stable():
    assert thread_key({"source": "gh", "id": "x"}) == "gh:x"
    assert key("a", "b") == key("a", "b") != key("a", "c")


def test_without_direct_delivery_a_follow_up_goes_through_the_main_session(world):
    engine, ports, _, store, _ = world
    engine.direct = False
    engine.receive(trig("t1"))
    engine.evaluate_due()
    ticket_id = accept(engine, "t1")["ticketId"]
    engine.receive(trig("t2", addressed=None, text="also the title"))
    assert not any(i.get("triggerId") == "t2" for i in store.ticket(ticket_id)["inputs"])
    engine.evaluate_due()
    assert ports.requests[-1]["triggers"][0]["id"] == "t2"           # a candidate for the main Session
    engine.attach(ticket_id, "t2")
    assert any(i.get("triggerId") == "t2" for i in store.ticket(ticket_id)["inputs"])
