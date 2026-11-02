"""The Engine: the one entry point that changes Triggers, Threads, Decisions,
Tickets and Assignments, each call inside one transaction.

Its rules follow dsh-superagent's core (intake, evals, tickets, assignments,
replies). What happens after a decision -- asking the main Session, starting a
worker, sending a message -- goes through ports that only store the work
(`store.effect_add`), so it is done after the transaction commits and retried
when it fails.

Ports (duck-typed):
  policy()     -> Policy: version, paused, eval_timeout, gate(), revalidate(), admits_inferred()
  scope(t)     -> {"listened": bool, "observed": bool, "row": str|None, "reason": str}
  assessor     .request(candidate) / .release(eval_id)
  executor     .start(ticket, assignment) / .deliver(ticket, assignment) / .stop(ticket, assignment, reason)
  outbox       .send(message) / .status(id) / .withdraw(id)
  emit(type, data)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from .model import (Doc, UserError, automatic, can_retry, followup_owners, iso_time, key, latest_assignment,
                    live, now_iso, read_until, reached, running_assignment, said, settled, thread_key, waits_for_person)
from .store import Store

#: How many times in a row an expired Eval queues its Triggers again before it is recorded unavailable.
REQUEUES = 2
STATUS_REQUEST = ("Report the current ticket status with ticket_report. This is a status-only request: do not resume "
                  "the work or perform outside actions.")
RECOVERY_NOTE = ("xHarness restarted during this ticket. Check the working directory, the git history and the "
                 "conversation on its Source before continuing. Do not repeat an outside action that already happened. "
                 "Finish the original assignment and report its status.")


def _shift(iso: str, seconds: float) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seconds(a: str, b: str) -> float:
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (parse(a) - parse(b)).total_seconds()


def _reasons(reason: Any) -> list[Doc]:
    return [reason] if isinstance(reason, dict) else [said(str(reason))]


def _stronger(a: str, b: str) -> str:
    return "explicit" if "explicit" in (a, b) else "inferred"


class Engine:
    def __init__(self, store: Store, *, policy: Callable[[], Any], scope: Callable[[Doc], Doc], assessor: Any,
                 executor: Any, outbox: Any, emit: Callable[[str, Doc], None] | None = None,
                 now: Callable[[], str] | None = None, prepare_ticket: Callable[[Doc, Doc], Doc] | None = None) -> None:
        self.repo = store
        self.policy = policy
        self.scope = scope
        self.assessor = assessor
        self.executor = executor
        self.outbox = outbox
        self._emit = emit
        self._now = now or now_iso
        self.prepare_ticket = prepare_ticket
        #: Subscriptions deliver a person's follow-up straight to its ticket's worker. Off only for the
        #: ablation that measures what that saves: every follow-up then goes through the main Session.
        self.direct = True

    # Shared helpers

    def now(self) -> str:
        return self._now()

    def emit(self, kind: str, data: Doc) -> None:
        if self._emit:
            self._emit(kind, data)

    def owners(self, trigger: Doc) -> list[str]:
        return followup_owners(trigger, self.repo.ticket, self.repo.thread, self.repo.disposition)

    def left_to_workers(self, trigger: Doc) -> None:
        owners = self.owners(trigger)
        if owners:
            raise UserError("workerDecides", {"tickets": ", ".join(owners)})

    def require(self, ticket_id: str) -> Doc:
        ticket = self.repo.ticket(ticket_id)
        if not ticket:
            raise UserError("unknownTicket")
        return ticket

    def thread(self, ref: Doc) -> Doc:
        k = thread_key(ref)
        return self.repo.thread(k) or {"key": k, "ref": {"source": ref["source"], "id": ref["id"]}, "createdAt": self.now(),
                                       "subscriptions": [], "correlated": [], "pending": [], "meta": {}}

    def tickets_on(self, thread: Doc) -> list[Doc]:
        return [t for t in (self.repo.ticket(i) for i in thread["subscriptions"]) if t]

    def origin(self, ticket: Doc) -> Doc | None:
        return self.repo.trigger(ticket["origin"]["triggerId"])

    def reaches(self, ticket: Doc, trigger: Doc, context: bool = True) -> bool:
        return reached(ticket, trigger, self.repo.thread, context)

    def disposition_of(self, trigger_id: str) -> Doc:
        return self.repo.disposition(trigger_id) or {"fanout": [], "at": self.now()}

    def queue_for_eval(self, trigger: Doc) -> bool:
        primary = self.thread(trigger["threads"][0])
        if trigger["id"] in primary["pending"] or trigger["id"] in ((primary.get("evaluating") or {}).get("triggerIds") or []):
            return False
        primary["pending"].append(trigger["id"])
        self.repo.save_thread(primary)
        return True

    def hand_back(self, ticket_id: str, trigger: Doc, reason: str) -> bool:
        decided = ticket_id in self.owners(trigger)
        record = self.disposition_of(trigger["id"])
        record["handbacks"] = [*record.get("handbacks", []), {"ticketId": ticket_id, "reason": reason, "at": self.now()}]
        self.repo.save_disposition(trigger["id"], record)
        return decided and not self.owners(trigger) and self.queue_for_eval(trigger)

    def take_up(self, ticket_id: str, trigger_id: str) -> None:
        record = self.repo.disposition(trigger_id)
        if record and ticket_id not in record.get("acked", []):
            record["acked"] = [*record.get("acked", []), ticket_id]
            self.repo.save_disposition(trigger_id, record)

    # Sources

    def receive(self, received: Doc) -> str:
        """Store a Trigger once, fan it out to the tickets subscribed to its Threads, and queue it for Eval
        unless a ticket's worker decides it. Returns stored / duplicate / filtered."""
        trigger = {**received, "at": iso_time(received["at"])}
        if not trigger.get("threads"):
            raise ValueError("a trigger must belong to at least one thread")
        with self.repo.atomic():
            if self.repo.trigger(trigger["id"]):
                return "duplicate"
            at = self.now()
            listened = self.scope(trigger)
            self.repo.save_trigger(trigger)
            if not listened.get("listened"):
                self.repo.save_disposition(trigger["id"], {"filtered": {"code": listened.get("reason", "filtered")},
                                                           "fanout": [], "at": at})
                return "filtered"
            keys = [thread_key(ref) for ref in trigger["threads"]]
            reach: dict[str, tuple[Doc, bool]] = {}
            for ref in trigger["threads"]:
                thread = self.thread(ref)
                if not thread.get("lastAt") or trigger["at"] > thread["lastAt"]:
                    thread["lastAt"] = trigger["at"]
                thread["correlated"] = sorted(set(thread["correlated"]) | {k for k in keys if k != thread["key"]})
                for ticket_id in (thread["subscriptions"] if self.direct or not trigger.get("human") else []):
                    ticket = self.repo.ticket(ticket_id)
                    followed = ticket_id in thread.get("context", [])
                    # Only a person's message on a Thread of its own resumes an ended ticket; a stopped one gets nothing.
                    resumes = trigger.get("human") and (ticket or {}).get("ended", {}).get("by") != "human" and not followed
                    if not ticket or (not live(ticket) and not resumes):
                        continue
                    before = reach.get(ticket_id)
                    reach[ticket_id] = (ticket, followed and (before[1] if before else True))
                self.repo.save_thread(thread)
            for ticket_id, (ticket, followed) in reach.items():
                item = {"id": key("input", ticket_id, "trigger", trigger["id"]), "kind": "trigger", "triggerId": trigger["id"], "at": at}
                if trigger.get("human"):
                    item["human"] = True
                if followed:
                    item["context"] = True
                self.add_input(self.require(ticket_id), item)
            fanout = list(reach)
            context = [t for t in fanout if reach[t][1]]
            record: Doc = {"fanout": fanout, "at": at}
            if context:
                record["context"] = context
            self.repo.save_disposition(trigger["id"], record)
            if not self.owners(trigger):
                self.queue_for_eval(trigger)
            for ticket_id in fanout:
                self.wake(ticket_id)
            self.emit("trigger.received", {"triggerId": trigger["id"], "threads": keys})
            return "stored"

    def bind_thread(self, ticket_id: str, ref: Doc, own: bool) -> Doc:
        """Make an open ticket follow a Thread, such as the pull request it opened (`own`)."""
        with self.repo.atomic():
            if not live(self.require(ticket_id)):
                raise UserError("ticketEnded")
            self._subscribe(ticket_id, ref)
            ticket = self.require(ticket_id)
            followed = thread_key(ref)
            follows = ticket.setdefault("follows", [])
            follow = next((f for f in follows if f["thread"] == followed), None)
            if follow:
                follow["own"] = follow["own"] or own
                follow.pop("closed", None)
            else:
                follows.append({"thread": followed, "own": own})
            ticket["updatedAt"] = self.now()
            self.repo.save_ticket(ticket)
            return ticket

    def thread_closed(self, ref: Doc, outcome: str, at: str) -> None:
        """A Thread open tickets follow closed (a pull request merged or closed)."""
        at = iso_time(at)
        closed = thread_key(ref)
        with self.repo.atomic():
            for ticket in self.repo.tickets_open():
                follow = next((f for f in ticket.get("follows", []) if f["thread"] == closed and not f.get("closed")), None)
                if not follow:
                    continue
                follow["closed"] = {"at": at, "outcome": outcome}
                self.repo.save_ticket(ticket)
                done = self.follows_done(ticket)
                if not done or self._unfinished(ticket, at):
                    continue
                latest = latest_assignment(ticket)
                resumed = bool(latest) and latest["createdAt"] > at
                self.end(ticket["id"], "executor", done["outcome"], self.now() if resumed else done["at"])

    @staticmethod
    def follows_done(ticket: Doc) -> Doc | None:
        follows = ticket.get("follows", [])
        own = [f for f in follows if f["own"]]
        counted = own or follows
        if not counted or any(not f.get("closed") for f in counted):
            return None
        return sorted((f["closed"] for f in counted), key=lambda c: c["at"])[-1]

    # The clock

    def evaluate_due(self) -> None:
        """Expire overdue Evals, resume ended tickets a person wrote to while paused, start the Evals that wait."""
        policy = self.policy()
        for thread in self.repo.threads_active():
            if thread.get("evaluating"):
                with self.repo.atomic():
                    current = self.repo.thread(thread["key"])
                    if current and current.get("evaluating"):
                        self._due(current, policy)
        if policy.paused:
            return
        for ticket in self.repo.tickets_active():
            if not live(ticket) and ticket.get("undelivered"):
                with self.repo.atomic():
                    self.wake(ticket["id"])
        for thread in self.repo.threads_active():
            if thread.get("pending") and not thread.get("evaluating"):
                with self.repo.atomic():
                    current = self.repo.thread(thread["key"])
                    if current and current.get("pending") and not current.get("evaluating"):
                        self._start_eval(current["key"], policy)

    # Evals

    def _due(self, thread: Doc, policy: Any) -> None:
        evaluation = thread["evaluating"]
        if policy.paused and evaluation.get("pausedAt"):
            return
        now = self.now()
        if policy.paused:
            evaluation["pausedAt"] = now
        elif evaluation.get("pausedAt"):
            evaluation["deadline"] = _shift(evaluation["deadline"], _seconds(now, evaluation.pop("pausedAt")))
        elif evaluation.get("delivered") and evaluation["deadline"] <= now:
            self._expire(thread)
            return
        else:
            return
        self.repo.save_thread(thread)

    def candidate_delivered(self, eval_id: str) -> None:
        """The candidate reached the main Session: its deadline counts from now."""
        with self.repo.atomic():
            policy = self.policy()
            for thread in self.repo.threads_active():
                evaluation = thread.get("evaluating")
                if evaluation and evaluation["id"] == eval_id and not evaluation.get("delivered"):
                    now = self.now()
                    evaluation["delivered"] = now
                    evaluation["deadline"] = _shift(now, policy.eval_timeout)
                    self.repo.save_thread(thread)

    def _expire(self, thread: Doc) -> None:
        expired = thread.get("expired", 0) + 1
        if expired > REQUEUES:
            self._finish(thread, thread["evaluating"], {"kind": "unavailable", "reason": [{"code": "timeout"}]})
            return
        evaluation = thread.pop("evaluating")
        thread["expired"] = expired
        ids = evaluation["triggerIds"]
        thread["pending"] = ids + [i for i in thread["pending"] if i not in ids]
        self.repo.save_thread(thread)
        self.assessor.release(evaluation["id"])

    def _start_eval(self, key_value: str, policy: Any) -> None:
        thread = self.repo.thread(key_value)
        at = self.now()
        # A follow-up a ticket still decides is that ticket's: it leaves the queue without a Decision.
        followups = [i for i in thread["pending"] if (t := self.repo.trigger(i)) and self.owners(t)]
        if followups:
            thread["pending"] = [i for i in thread["pending"] if i not in followups]
            self.repo.save_thread(thread)
            for trigger_id in followups:
                self.emit("trigger.followup", {"triggerId": trigger_id})
            if not thread["pending"]:
                return
        accepted = self._accepted(thread["key"])
        started = [i for i in thread["pending"] if i in accepted]
        if started:
            reasons = [{"code": "startedTicket", "triggerId": i, "values": {"ticket": accepted[i]["ticketId"]}} for i in started]
            self._ignore(thread, started, reasons, policy, at)
        triggers = [t for t in (self.repo.trigger(i) for i in thread["pending"]) if t]
        tickets = self.tickets_on(thread)
        reasons: list[Doc] = []
        strongest: Doc | None = None
        for trigger in triggers:
            gate = policy.gate(trigger, thread, tickets)
            if not gate["admissible"]:
                reasons.append({"code": gate["reason"], "triggerId": trigger["id"]})
                continue
            later = strongest is not None and gate["strength"] == strongest["strength"] and trigger["at"] >= strongest["trigger"]["at"]
            weaker = strongest is None or _stronger(strongest["strength"], gate["strength"]) != strongest["strength"]
            if weaker or later:
                strongest = {"trigger": trigger, "strength": gate["strength"], "basis": gate["basis"]}
        if strongest is None:
            if thread["pending"]:
                self._ignore(thread, list(thread["pending"]), reasons or [{"code": "noAdmissible"}], policy, at)
            self.repo.save_thread(thread)
            return
        eval_id = key("eval", thread["key"], policy.version, at, *thread["pending"])
        thread["evaluating"] = {
            "id": eval_id, "threadKey": thread["key"], "triggerIds": thread["pending"], "primary": strongest["trigger"]["id"],
            "strength": strongest["strength"], "actor": strongest["trigger"]["actor"], "basis": strongest["basis"],
            "policyVersion": policy.version, "startedAt": at, "deadline": _shift(at, policy.eval_timeout)}
        thread["pending"] = []
        self.repo.save_thread(thread)
        for trigger in triggers:
            record = self.disposition_of(trigger["id"])
            record["evalId"] = eval_id
            self.repo.save_disposition(trigger["id"], record)
        self.emit("eval.started", {"evalId": eval_id, "threadKey": thread["key"], "strength": strongest["strength"]})
        self.assessor.request(self._candidate_of(thread))

    def candidate(self, trigger_id: str) -> Doc | None:
        thread = self._holding(trigger_id)
        return self._candidate_of(thread) if thread else None

    def _candidate_of(self, thread: Doc) -> Doc:
        evaluation = thread["evaluating"]
        triggers = [t for t in (self.repo.trigger(i) for i in evaluation["triggerIds"]) if t]
        return {"evalId": evaluation["id"], "thread": thread, "triggers": triggers, "strength": evaluation["strength"],
                "policyVersion": evaluation["policyVersion"], "tickets": self.tickets_on(thread)}

    def _holding(self, trigger_id: str) -> Doc | None:
        trigger = self.repo.trigger(trigger_id)
        thread = trigger and self.repo.thread(thread_key(trigger["threads"][0]))
        if thread and trigger_id in ((thread.get("evaluating") or {}).get("triggerIds") or []):
            return thread
        return None

    def _accepted(self, key_value: str) -> dict[str, Doc]:
        accepted: dict[str, Doc] = {}
        for decision in self.repo.decisions_on(key_value):
            if decision["kind"] != "accept" or not decision.get("ticketId"):
                continue
            for trigger_id in decision["triggerIds"]:
                accepted.setdefault(trigger_id, decision)
        return accepted

    def decide(self, trigger_id: str, outcome: Doc) -> Doc:
        """Decide a Trigger: the Eval that holds it, or a direct Eval of it alone. outcome kind is
        accept (title, scope, reason, rules?, acknowledgement?, name?), stop (ticketId, reason), ignore or unavailable."""
        with self.repo.atomic():
            trigger = self.repo.trigger(trigger_id)
            if not trigger:
                raise UserError("unknownTrigger")
            held = self._holding(trigger_id)
            if outcome.get("evalId") and (not held or held["evaluating"]["id"] != outcome["evalId"]):
                raise UserError("evalFinished", {"eval": outcome["evalId"]})
            earlier = self._accepted(thread_key(trigger["threads"][0])).get(trigger_id) if outcome["kind"] == "accept" else None
            if earlier:
                others = [i for i in (held or {}).get("evaluating", {}).get("triggerIds", []) if i != trigger_id] if held else []
                if held and others:
                    self._reassess(held, {"code": "acceptedBefore", "values": {"trigger": trigger_id}}, [trigger_id])
                elif held:
                    self._finish(held, held["evaluating"], {"kind": "ignore", "reason": [
                        {"code": "startedTicket", "triggerId": trigger_id, "values": {"ticket": earlier["ticketId"]}}]})
                return earlier
            # A follow-up that reached a ticket is its worker's: the main Session may only stop the work on it.
            if outcome["kind"] != "stop":
                self.left_to_workers(trigger)
            thread = held or self.thread(trigger["threads"][0])
            evaluation = held["evaluating"] if held else self._direct_eval(
                thread, trigger, outcome["kind"], outcome.get("ticketId") if outcome["kind"] == "stop" else None)
            if outcome["kind"] == "accept" and evaluation["strength"] != "explicit" \
                    and not self.policy().admits_inferred(outcome.get("rules") or []):
                raise UserError("notExplicit")
            return self._decide_eval(thread, evaluation, outcome)

    def reevaluate(self, trigger_id: str) -> bool:
        """A person sends a stored Trigger to Eval again; False while an Eval holds it."""
        with self.repo.atomic():
            trigger = self.repo.trigger(trigger_id)
            if not trigger:
                raise UserError("unknownTrigger")
            if self._holding(trigger_id):
                return False
            self.queue_for_eval(trigger)
            record = self.disposition_of(trigger_id)
            record["requeued"] = True
            self.repo.save_disposition(trigger_id, record)
            self.emit("trigger.requeued", {"triggerId": trigger_id})
            return True

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        """A worker hands back a message that asks for more than its ticket; it goes to Eval once no ticket decides it."""
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            if not any(i["kind"] == "trigger" and i["triggerId"] == trigger_id for i in ticket["inputs"]):
                raise UserError("notReached")
            if any(h["ticketId"] == ticket_id for h in self.disposition_of(trigger_id).get("handbacks", [])):
                raise UserError("handedBack")
            return self.hand_back(ticket_id, self.repo.trigger(trigger_id), reason)

    def dismiss(self, decision_id: str) -> Doc:
        with self.repo.atomic():
            decision = self.repo.decision(decision_id)
            if not decision or decision["kind"] != "unavailable":
                raise UserError("unknownProblem")
            decision["dismissed"] = True
            self.repo.save_decision(decision)
            return decision

    def policy_changed(self, old: str, new: str, effects: dict[str, Doc] | None = None) -> None:
        """Tell each open ticket the change bears on, oldest first; Evals in progress start again under the new version."""
        with self.repo.atomic():
            at = self.now()
            for ticket in self.repo.tickets_open():
                effect = (effects or {}).get(ticket["id"])
                if effect:
                    self.add_input(ticket, {"id": key("input", ticket["id"], "policy", old, new, at), "kind": "policy-changed",
                                            "from": old, "to": new, "at": at, "effect": effect})
            for thread in self.repo.threads_active():
                if thread.get("evaluating"):
                    self._reassess(thread, {"code": "policyChanged"})
            self.emit("policy.changed", {"from": old, "to": new})

    def _reassess(self, thread: Doc, reason: Doc, without: list[str] | None = None) -> Doc:
        ids = [i for i in thread["evaluating"]["triggerIds"] if i not in (without or [])]
        decision = self._finish(thread, thread["evaluating"], {"kind": "unavailable", "reason": [reason]})
        current = self.repo.thread(thread["key"])
        current["pending"] = ids + [i for i in current["pending"] if i not in ids]
        self.repo.save_thread(current)
        return decision

    def _ignore(self, thread: Doc, ids: list[str], reason: list[Doc], policy: Any, at: str) -> None:
        evaluation = {"id": key("eval", thread["key"], "ignored", policy.version, at, *ids), "threadKey": thread["key"],
                      "triggerIds": ids, "strength": "inferred", "actor": {"source": thread["ref"]["source"], "id": ""},
                      "basis": {"via": "followup"}, "policyVersion": policy.version, "startedAt": at, "deadline": at}
        self._record(thread, evaluation, {"kind": "ignore", "reason": reason})
        thread["pending"] = [i for i in thread["pending"] if i not in ids]
        thread.pop("expired", None)

    def _direct_eval(self, thread: Doc, trigger: Doc, kind: str, ticket_id: str | None) -> Doc:
        policy = self.policy()
        gate = policy.gate(trigger, thread, self.tickets_on(thread))
        if not gate["admissible"]:
            raise UserError("notAdmissible", {"reason": gate["reason"]})
        at = self.now()
        if trigger["id"] in thread["pending"]:
            thread["pending"] = [i for i in thread["pending"] if i != trigger["id"]]
            self.repo.save_thread(thread)
        eval_id = key("eval", thread["key"], "direct", trigger["id"], kind, *([ticket_id] if ticket_id else []), at)
        return {"id": eval_id, "threadKey": thread["key"], "triggerIds": [trigger["id"]], "primary": trigger["id"],
                "strength": gate["strength"], "actor": trigger["actor"], "basis": gate["basis"],
                "policyVersion": policy.version, "startedAt": at, "deadline": at}

    def _decide_eval(self, thread: Doc, evaluation: Doc, outcome: Doc) -> Doc:
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        if self.policy().version != evaluation["policyVersion"]:
            if (thread.get("evaluating") or {}).get("id") == evaluation["id"]:
                return self._reassess(thread, {"code": "policyChanged"})
            return self._finish(thread, evaluation, {"kind": "unavailable", "reason": [{"code": "policyChanged"}], **audit})
        kind = outcome["kind"]
        if kind in ("ignore", "unavailable"):
            unassessed = [{"code": "ignoredFromCard"}] if kind == "ignore" and evaluation["strength"] != "explicit" \
                and not outcome.get("audit") else []
            return self._finish(thread, evaluation, {"kind": kind, "reason": unassessed + _reasons(outcome.get("reason", "")), **audit})
        if kind == "stop":
            ticket = self.repo.ticket(outcome.get("ticketId", ""))
            if not ticket or not live(ticket):
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopUnknown"}], **audit})
            if evaluation["strength"] != "explicit":
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopNotExplicit"}], **audit})
            decision = self._finish(thread, evaluation, {"kind": "stop", "reason": _reasons(outcome.get("reason", "")),
                                                         "ticketId": ticket["id"], **audit})
            text = outcome.get("reason") if isinstance(outcome.get("reason"), str) else None
            self.end(ticket["id"], "human", "stopped", self.now(), text)
            return decision
        return self._accept(thread, evaluation, outcome)

    def _accept(self, thread: Doc, evaluation: Doc, outcome: Doc) -> Doc:
        at = self.now()
        basis = {**evaluation["basis"], **({"rules": outcome["rules"]} if outcome.get("rules") else {})}
        mandate = {"strength": evaluation["strength"], "policyVersion": evaluation["policyVersion"], "basis": basis,
                   "actor": evaluation["actor"], "scope": outcome["scope"]}
        ticket_id = key("ticket", evaluation["id"])
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        decision = self._finish(thread, evaluation, {"kind": "accept", "reason": _reasons(outcome.get("reason", "")), **audit,
                                                     "mandate": mandate, "ticketId": ticket_id})
        self._create(ticket_id, decision["id"], mandate, evaluation, outcome, at)
        return decision

    def _finish(self, thread: Doc, evaluation: Doc, outcome: Doc) -> Doc:
        decision = self._record(thread, evaluation, outcome)
        if (thread.get("evaluating") or {}).get("id") != evaluation["id"]:
            return decision
        current = self.repo.thread(thread["key"]) or thread
        current.pop("evaluating", None)
        current.pop("expired", None)
        thread.pop("evaluating", None)
        thread.pop("expired", None)
        self.repo.save_thread(current)
        self.assessor.release(evaluation["id"])
        return decision

    def _record(self, thread: Doc, evaluation: Doc, outcome: Doc) -> Doc:
        at = self.now()
        decision = {"id": key("decision", evaluation["id"]), "evalId": evaluation["id"], "threadKey": thread["key"],
                    "triggerIds": evaluation["triggerIds"], "policyVersion": evaluation["policyVersion"], "at": at,
                    "kind": outcome["kind"], "reason": outcome["reason"], "strength": evaluation["strength"]}
        for name in ("ticketId", "mandate", "audit"):
            if outcome.get(name):
                decision[name] = outcome[name]
        self.repo.save_decision(decision)
        for trigger_id in evaluation["triggerIds"]:
            record = self.disposition_of(trigger_id)
            record.update({"evalId": evaluation["id"], "decisionId": decision["id"]})
            self.repo.save_disposition(trigger_id, record)
        self.emit("decision.recorded", {"decisionId": decision["id"], "kind": decision["kind"], "threadKey": thread["key"]})
        return decision

    # Tickets

    def _create(self, ticket_id: str, decision_id: str, mandate: Doc, evaluation: Doc, outcome: Doc, at: str) -> None:
        trigger_ids = evaluation["triggerIds"]
        primary = self.repo.trigger(evaluation.get("primary") or trigger_ids[-1])
        ticket = {"id": ticket_id, "title": outcome["title"], "decisionId": decision_id, "mandate": mandate, "channels": [],
                  "assignments": [], "inputs": [], "undelivered": [], "createdAt": at, "updatedAt": at,
                  "origin": {"triggerId": primary["id"] if primary else trigger_ids[0], "triggerIds": trigger_ids}}
        for trigger_id in trigger_ids:
            channel = (self.repo.trigger(trigger_id) or {}).get("channel")
            if channel and channel not in ticket["channels"]:
                ticket["channels"].append(channel)
        if self.prepare_ticket:
            ticket = self.prepare_ticket(ticket, outcome)
        assignment = self._new_assignment(ticket)
        self.repo.save_ticket(ticket)
        for trigger_id in trigger_ids:
            for ref in (self.repo.trigger(trigger_id) or {}).get("threads", []):
                self._subscribe(ticket_id, ref)
        if outcome.get("acknowledgement") and primary:
            self._acknowledge_accepted(ticket_id, primary, outcome["acknowledgement"])
        self.executor.start(self.require(ticket_id), assignment)
        self.emit("ticket.created", {"ticketId": ticket_id, "decisionId": decision_id, "strength": mandate["strength"]})

    def attach(self, ticket_id: str, trigger_id: str, note: str = "") -> Doc:
        """The main Session attaches a message to the ticket it points at; the ticket follows its Threads."""
        with self.repo.atomic():
            if any(h["ticketId"] == ticket_id for h in self.disposition_of(trigger_id).get("handbacks", [])):
                raise UserError("handedBackHere")
            trigger = self.repo.trigger(trigger_id)
            if not trigger:
                raise UserError("unknownTrigger")
            self.left_to_workers(trigger)
            current = self.require(ticket_id)
            if (current.get("ended") or {}).get("by") == "human":
                raise UserError("ticketStopped")
            if not live(current) and not self.policy().revalidate(current["mandate"]):
                raise UserError("mandateRevoked")
            primary = self.repo.thread(thread_key(trigger["threads"][0]))
            if primary and trigger_id in primary["pending"]:
                primary["pending"] = [i for i in primary["pending"] if i != trigger_id]
                self.repo.save_thread(primary)
            self._attach(ticket_id, trigger, note)
            held = self._holding(trigger_id)
            reason = {"code": "routed", "values": {"ticket": ticket_id}}
            if held and len(held["evaluating"]["triggerIds"]) == 1:
                self._finish(held, held["evaluating"], {"kind": "ignore", "reason": [reason]})
            elif held:
                self._reassess(held, {**reason, "triggerId": trigger_id}, [trigger_id])
            self.wake(ticket_id)
            return self.require(ticket_id)

    def _attach(self, ticket_id: str, trigger: Doc, note: str) -> None:
        for ref in trigger["threads"]:
            self._subscribe(ticket_id, ref)
        current = self.disposition_of(trigger["id"])
        context = current.get("context") or []
        requeued = current.get("requeued")
        record = {k: v for k, v in current.items() if k not in ("context", "requeued")}
        record["fanout"] = sorted(set(record.get("fanout", [])) | {ticket_id})
        followed = [i for i in context if i != ticket_id]
        if followed:
            record["context"] = followed
        self.repo.save_disposition(trigger["id"], record)
        ticket = self.require(ticket_id)
        input_id = key("input", ticket_id, "trigger", trigger["id"])
        existing = next((i for i in ticket["inputs"] if i["id"] == input_id), None)
        at = self.now()

        def make(item_id: str) -> Doc:
            item = {"id": item_id, "kind": "trigger", "triggerId": trigger["id"], "at": at}
            if note:
                item["note"] = note
            if trigger.get("human"):
                item["human"] = True
            return item
        if not existing:
            self.add_input(ticket, make(input_id))
        elif note and input_id in ticket["undelivered"]:
            existing["note"] = note
            ticket["updatedAt"] = at
            self.repo.save_ticket(ticket)
        elif requeued or ticket_id not in current.get("fanout", []) or ticket_id in context or not live(ticket):
            self.add_input(ticket, make(key("input", ticket_id, "trigger", trigger["id"], "attached", at)))
        elif note:
            self.add_input(ticket, make(key("input", ticket_id, "trigger", trigger["id"], "note", note)))

    def request_status(self, ticket_id: str) -> bool:
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            running = running_assignment(ticket)
            if not live(ticket) or ((running or {}).get("report") or {}).get("state") != "waiting":
                return False
            self.add_input(ticket, {"id": f"status:{ticket_id}:{ticket['updatedAt']}", "kind": "text", "text": STATUS_REQUEST,
                                    "at": self.now()})
            return True

    def note(self, ticket_id: str, text: str) -> Doc:
        """A fact the main Session gives a ticket's worker: context, which grants nothing."""
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            if not live(ticket):
                raise UserError("ticketEnded")
            at = self.now()
            self.add_input(ticket, {"id": key("input", ticket_id, "note", at, text), "kind": "note", "text": text, "at": at})
            return self.require(ticket_id)

    def stop_ticket(self, ticket_id: str, reason: str = "") -> Doc:
        """A person stops a ticket: it ends and receives nothing more."""
        with self.repo.atomic():
            self.end(ticket_id, "human", "stopped", self.now(), reason or None)
            return self.require(ticket_id)

    def retry_ticket(self, ticket_id: str) -> Doc:
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            latest = latest_assignment(ticket)
            if not can_retry(ticket) or not latest:
                raise UserError("notRetryable")
            self._end_assignment(ticket, latest, "retried")
            self._requeue_untaken(ticket, latest)
            assignment = self._new_assignment(ticket)
            ticket["updatedAt"] = self.now()
            self.repo.save_ticket(ticket)
            self.executor.stop(ticket, latest, "retried")
            self.executor.start(ticket, assignment)
            return ticket

    def unsubscribe(self, thread: str) -> bool:
        """A person drops every ticket's Subscription to a Thread."""
        with self.repo.atomic():
            return self._drop_subscriptions(thread, "person")

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        """A worker found an attached Thread unrelated: only its ticket's Subscription drops."""
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            source = self.origin(ticket)
            if source and any(thread_key(ref) == thread for ref in source["threads"]):
                raise UserError("sourceThread")
            if any(f["thread"] == thread for f in ticket.get("follows", [])):
                raise UserError("followedThread")
            if ticket_id not in (self.repo.thread(thread) or {}).get("subscriptions", []):
                raise UserError("notFollowing")
            self._drop_subscriptions(thread, "executor", ticket_id, reason)
            for trigger in self._triggers_of(ticket):
                if any(thread_key(ref) == thread for ref in trigger["threads"]) and ticket_id in self.owners(trigger):
                    self.hand_back(ticket_id, trigger, reason)

    def follow_for_context(self, ticket_id: str, ref: Doc) -> bool:
        with self.repo.atomic():
            if not live(self.require(ticket_id)):
                raise UserError("ticketEnded")
            if ticket_id in self.thread(ref)["subscriptions"]:
                return False
            self._subscribe(ticket_id, ref)
            thread = self.thread(ref)
            thread["context"] = [*thread.get("context", []), ticket_id]
            self.repo.save_thread(thread)
            return True

    def _drop_subscriptions(self, thread: str, by: str, ticket_id: str | None = None, reason: str = "") -> bool:
        current = self.repo.thread(thread)
        tickets = [i for i in (current or {}).get("subscriptions", []) if ticket_id is None or i == ticket_id]
        if not current or not tickets:
            return False
        current["subscriptions"] = [i for i in current["subscriptions"] if i not in tickets]
        self.repo.save_thread(current)
        for tid in tickets:
            ticket = self.repo.ticket(tid)
            if ticket and any(f["thread"] == thread for f in ticket.get("follows", [])):
                ticket["follows"] = [f for f in ticket["follows"] if f["thread"] != thread]
                if not ticket["follows"]:
                    ticket.pop("follows")
                self.repo.save_ticket(ticket)
        for tid in tickets:
            self.emit("thread.unsubscribed", {"threadKey": thread, "ticketId": tid, "by": by, **({"reason": reason} if reason else {})})
        return True

    def add_input(self, ticket: Doc, item: Doc) -> None:
        """Add an Input once and have the Executor deliver it; automated Inputs wait while a person is waited for."""
        if any(i["id"] == item["id"] for i in ticket["inputs"]):
            return
        ticket["inputs"].append(item)
        ticket["updatedAt"] = self.now()
        held = live(ticket) and automatic(item) and waits_for_person(ticket)
        if not held:
            ticket["undelivered"].append(item["id"])
        self.repo.save_ticket(ticket)
        if not live(ticket) or held:
            return
        running, latest = running_assignment(ticket), latest_assignment(ticket)
        if running:
            self.executor.deliver(ticket, running)
        elif latest and latest["state"] == "pending" and not waits_for_person(ticket):
            self.executor.start(ticket, latest)

    def wake(self, ticket_id: str) -> None:
        """Resume an ended ticket under its unchanged Mandate when a person's message waits for it: no Eval."""
        ticket = self.repo.ticket(ticket_id)
        if not ticket or live(ticket) or (ticket.get("ended") or {}).get("by") == "human" or not ticket["undelivered"]:
            return
        policy = self.policy()
        if policy.paused:
            return
        if not policy.revalidate(ticket["mandate"]):
            self._refuse(ticket)
            return
        first = next((i for i in (self._input(ticket, x) for x in ticket["undelivered"])
                      if i and i["kind"] == "trigger" and i.get("human")), None)
        if not first:
            return
        at = self.now()
        ticket.pop("ended", None)
        ticket["updatedAt"] = at
        assignment = self._new_assignment(ticket)
        resumed = {"id": key("input", ticket_id, "resumed", first["triggerId"]), "kind": "resumed", "triggerId": first["triggerId"], "at": at}
        ticket["inputs"].append(resumed)
        ticket["undelivered"].insert(0, resumed["id"])
        self.repo.save_ticket(ticket)
        self.executor.start(ticket, assignment)
        self.emit("ticket.resumed", {"ticketId": ticket_id, "triggerId": first["triggerId"]})

    @staticmethod
    def _input(ticket: Doc, input_id: str) -> Doc | None:
        return next((i for i in ticket["inputs"] if i["id"] == input_id), None)

    def _triggers_of(self, ticket: Doc, ids: list[str] | None = None) -> list[Doc]:
        out = []
        for item in ticket["inputs"]:
            if item["kind"] == "trigger" and (ids is None or item["id"] in ids):
                trigger = self.repo.trigger(item["triggerId"])
                if trigger:
                    out.append(trigger)
        return out

    def _taken_up(self, ticket: Doc, trigger: Doc) -> bool:
        acked = (self.repo.disposition(trigger["id"]) or {}).get("acked")
        return ticket["id"] in acked if acked is not None else self.outbox.status(key("ack", trigger["id"])) is not None

    def _untaken(self, ticket: Doc, ids: list[str]) -> list[Doc]:
        return [t for t in self._triggers_of(ticket, ids) if ticket["id"] in self.owners(t) and not self._taken_up(ticket, t)]

    def _let_go(self, ticket_id: str, waiting: list[Doc], left: list[Doc]) -> None:
        for trigger in waiting + left:
            record = self.repo.disposition(trigger["id"])
            if record and ticket_id in record.get("fanout", []):
                record["fanout"] = [i for i in record["fanout"] if i != ticket_id]
                self.repo.save_disposition(trigger["id"], record)
        for trigger in left:
            record = self.repo.disposition(trigger["id"]) or {}
            decision = self.repo.decision(record["decisionId"]) if record.get("decisionId") else None
            stopped = bool(decision) and decision["kind"] == "stop" and decision.get("ticketId") == ticket_id \
                and not record.get("handbacks")
            decided = bool(decision) and (settled(decision) or stopped)
            if not self.owners(trigger) and not record.get("acked") and not decided:
                self.queue_for_eval(trigger)

    def _refuse(self, ticket: Doc) -> None:
        waiting = self._triggers_of(ticket, ticket["undelivered"])
        left = self._untaken(ticket, ticket["undelivered"])
        ticket["undelivered"] = []
        self.repo.save_ticket(ticket)
        self._let_go(ticket["id"], waiting, left)

    def _restart(self, ticket: Doc, running: Doc, reason: str, note: str) -> None:
        self._end_assignment(ticket, running, reason)
        self._requeue_untaken(ticket, running)
        assignment = self._new_assignment(ticket)
        at = self.now()
        item = {"id": key("input", ticket["id"], "restart", assignment["id"]), "kind": "text", "text": note, "at": at}
        ticket["inputs"].append(item)
        ticket["undelivered"].append(item["id"])
        ticket["updatedAt"] = at
        self.repo.save_ticket(ticket)
        self.executor.start(ticket, assignment)

    @staticmethod
    def _requeue_untaken(ticket: Doc, replaced: Doc) -> None:
        untaken = [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger" and "readAt" in i and i["readAt"] is None
                   and i.get("deliveredTo") == replaced["id"] and i["id"] not in ticket["undelivered"]]
        ticket["undelivered"] = untaken + ticket["undelivered"]

    def end(self, ticket_id: str, by: str, outcome: str, at: str | None = None, reason: str | None = None) -> None:
        """End a ticket; it lets go of messages its worker never read, which go to Eval if no one else decides them."""
        at = at or self.now()
        ticket = self.require(ticket_id)
        if not live(ticket):
            return
        stopping = [a for a in ticket["assignments"] if a["state"] != "ended"]
        until = read_until(ticket)
        waiting = self._triggers_of(ticket, ticket["undelivered"])

        def read_before(item: Doc) -> bool:
            if "readAt" not in item:
                delivered = item.get("deliveredAt")
                return (delivered if delivered and delivered > item["at"] else item["at"]) <= until
            return bool(item["readAt"]) and item["readAt"] < until
        # A message attached again after a resume arrives as a new Input: once any copy was read, it was read.
        read = {i["triggerId"] for i in ticket["inputs"] if i["kind"] == "trigger" and i["id"] not in ticket["undelivered"]
                and read_before(i)}
        left = self._untaken(ticket, [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger" and i["triggerId"] not in read
                                      and (not read_before(i) or i["id"] in ticket["undelivered"])])
        ticket["ended"] = {"at": at, "by": by, "outcome": outcome, **({"reason": reason} if reason and reason != outcome else {})}
        ticket["undelivered"] = []
        ticket["updatedAt"] = self.now()
        ticket.pop("attention", None)
        ticket.pop("delivery", None)
        for assignment in stopping:
            assignment["state"] = "ended"
            assignment["endedAt"] = at
        self.repo.save_ticket(ticket)
        self._let_go(ticket_id, waiting, left)
        self.emit("ticket.ended", {"ticketId": ticket_id, "by": by, "outcome": outcome, **({"reason": reason} if reason else {})})
        for assignment in stopping:
            self.executor.stop(ticket, assignment, reason or outcome)

    def _unfinished(self, ticket: Doc, at: str) -> bool:
        until = read_until(ticket, False)
        unread = [i["id"] for i in ticket["inputs"] if i["kind"] == "trigger"
                  and not ((i.get("readAt") and i["readAt"] < until) if "readAt" in i else i["at"] <= until)]
        taken = any(ticket["id"] in self.owners(t) and self._taken_up(ticket, t) for t in self._triggers_of(ticket, unread))
        latest = latest_assignment(ticket)
        if taken or not (latest and latest["createdAt"] > at):
            return taken
        report = latest.get("report")
        later = any(i["kind"] != "policy-changed" and i["at"] > (report or {}).get("at", "") for i in ticket["inputs"])
        return (report or {}).get("state") != "ready" or bool(ticket["undelivered"]) or later

    def _new_assignment(self, ticket: Doc) -> Doc:
        assignment = {"id": key("assignment", ticket["id"], str(len(ticket["assignments"]))), "state": "pending", "createdAt": self.now()}
        ticket["assignments"].append(assignment)
        ticket.pop("delivery", None)
        return assignment

    def _end_assignment(self, ticket: Doc, assignment: Doc, reason: str) -> None:
        assignment.update({"state": "ended", "endedAt": self.now(), "endedReason": reason})
        self.emit("assignment.ended", {"ticketId": ticket["id"], "assignmentId": assignment["id"], "reason": reason})

    def _subscribe(self, ticket_id: str, ref: Doc) -> None:
        thread = self.thread(ref)
        if ticket_id not in thread["subscriptions"]:
            thread["subscriptions"].append(ticket_id)
            self.emit("thread.subscribed", {"threadKey": thread["key"], "ticketId": ticket_id})
        if ticket_id in thread.get("context", []):
            thread["context"] = [i for i in thread["context"] if i != ticket_id]
        self.repo.save_thread(thread)

    # The Executor's facts

    def _running(self, ticket_id: str, assignment_id: str) -> Doc | None:
        ticket = self.require(ticket_id)
        running = running_assignment(ticket)
        return ticket if live(ticket) and running and running["id"] == assignment_id else None

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            assignment = next((a for a in ticket["assignments"] if a["id"] == assignment_id), None)
            if not assignment or assignment["state"] == "ended":
                return
            assignment["state"] = "running"
            assignment.setdefault("startedAt", self.now())
            ticket["updatedAt"] = self.now()
            self.repo.save_ticket(ticket)
            if live(ticket) and ticket["undelivered"]:
                self.executor.deliver(ticket, assignment)
            self.emit("assignment.started", {"ticketId": ticket_id, "assignmentId": assignment_id})

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        with self.repo.atomic():
            ticket = self._running(ticket_id, assignment_id)
            if not ticket:
                return
            ticket["undelivered"] = [i for i in ticket["undelivered"] if i not in input_ids]
            ticket["updatedAt"] = self.now()
            running_assignment(ticket)["deliveredAt"] = ticket["updatedAt"]
            for item in ticket["inputs"]:
                if item["kind"] == "trigger" and item["id"] in input_ids:
                    item.setdefault("readAt", None)
                    item["deliveredTo"] = assignment_id
            ticket.pop("delivery", None)
            self.repo.save_ticket(ticket)

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            if not live(ticket):
                return
            for item in ticket["inputs"]:
                if item["kind"] == "trigger" and item["id"] in input_ids and not item.get("readAt"):
                    item["readAt"] = self.now()
            self.repo.save_ticket(ticket)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        with self.repo.atomic():
            ticket = self._running(ticket_id, assignment_id)
            if ticket:
                running_assignment(ticket)["idleAt"] = self.now()
                self.repo.save_ticket(ticket)

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        with self.repo.atomic():
            ticket = self._running(ticket_id, assignment_id)
            if ticket:
                ticket["delivery"] = {"error": error[:300], "at": self.now()}
                self.repo.save_ticket(ticket)

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        """A host restart cut an Assignment off: start it again with a recovery note, unless it had settled."""
        with self.repo.atomic():
            ticket = self._running(ticket_id, assignment_id)
            running = ticket and running_assignment(ticket)
            if not ticket or not running:
                return
            report = running.get("report")
            busy = bool(running.get("deliveredAt")) and bool(report) and running["deliveredAt"] >= report["at"]
            done = bool(report) and report["state"] in ("waiting", "ready", "blocked", "failed") and not busy
            if not done:
                self._restart(ticket, running, "hostRestarted", RECOVERY_NOTE)
            elif ticket["undelivered"]:
                self.executor.deliver(ticket, running)

    def report(self, ticket_id: str, report: Doc) -> Doc:
        """A worker's report on its ticket. A question for a person is asked, kept or cleared; ready or dropped ends it."""
        with self.repo.atomic():
            ticket = self.require(ticket_id)
            if not live(ticket):
                raise UserError("ticketEnded")
            question, asked_by, answers = report.get("question"), report.get("askedBy"), report.get("answers")
            if question and (report["state"] == "ready" or answers):
                raise UserError("questionWithResult")
            if asked_by and not question:
                raise UserError("askerWithoutQuestion")

            def person(trigger_id: str, channel: bool) -> bool:
                trigger = self.repo.trigger(trigger_id)
                return bool(trigger) and bool(trigger.get("human")) and (not channel or bool(trigger.get("channel"))) \
                    and self.reaches(ticket, trigger, False)
            if asked_by and not person(asked_by, True):
                raise UserError("notAsker")
            if answers and not person(answers, False):
                raise UserError("notAnswerable")
            assignment = running_assignment(ticket) or latest_assignment(ticket)
            if not assignment:
                raise UserError("unknownAssignment")
            if assignment["state"] == "ended":
                raise UserError("assignmentEnded")
            at = self.now()
            previous = ticket.get("attention")
            if question and question != (previous or {}).get("question"):
                asked = {"id": key("attention", ticket_id, question, at), "question": question,
                         **({"triggerId": asked_by} if asked_by else {})}
            else:
                asked = previous
            attention = None if "question" in report and question is None or report["state"] == "ready" else asked
            if attention:
                ticket["attention"] = attention
            else:
                ticket.pop("attention", None)
            if previous and previous["id"] != (attention or {}).get("id"):
                self.outbox.withdraw(key("attention-reply", previous["id"]))
            assignment["report"] = {"state": report["state"], "summary": report.get("summary", ""), "at": at}
            ticket["updatedAt"] = at
            if answers and answers not in ticket.get("answers", []):
                ticket["answers"] = [*ticket.get("answers", []), answers]
            if answers:
                self.take_up(ticket_id, answers)
            if report["state"] in ("blocked", "failed"):
                ticket["undelivered"] = [i for i in ticket["undelivered"] if not automatic(self._input(ticket, i))]
            self.repo.save_ticket(ticket)
            ready = "completed" if not ticket.get("follows") else (self.follows_done(ticket) or {}).get("outcome")
            if report["state"] == "dropped":
                outcome = (self.follows_done(ticket) or {}).get("outcome") or "dropped"
            elif report["state"] == "ready":
                outcome = ready
            else:
                outcome = None
            if outcome:
                self.end(ticket_id, "executor", outcome, at)
            return self.require(ticket_id)

    # Replies: each message by the rule that allows it, under an id that keeps it from going out twice.

    def _acknowledge_accepted(self, ticket_id: str, trigger: Doc, text: str) -> None:
        if trigger.get("channel") and trigger.get("human"):
            self.outbox.send({"id": key("ack", trigger["id"]), "channel": trigger["channel"], "text": text,
                              "kind": "acknowledgement", "ticketId": ticket_id})

    def reply(self, request: Doc) -> Doc:
        """purpose: acknowledgement (ticketId, triggerId) / answer / result / attention (question); text."""
        with self.repo.atomic():
            if request["purpose"] == "acknowledgement":
                return self._acknowledge(request)
            return self._answer(request)

    def _acknowledge(self, request: Doc) -> Doc:
        ticket = self.require(request["ticketId"])
        if not live(ticket):
            raise UserError("ackEnded")
        trigger = self.repo.trigger(request["triggerId"])
        if not trigger:
            raise UserError("unknownTrigger")
        if not self.reaches(ticket, trigger, False):
            raise UserError("notReached")
        if not trigger.get("channel"):
            raise UserError("ackNoChannel")
        message_id = key("ack", trigger["id"])
        existing = self.outbox.status(message_id)
        if existing is None and not trigger.get("human"):
            raise UserError("ackNotPerson")
        if existing is None:
            self.outbox.send({"id": message_id, "channel": trigger["channel"], "text": request["text"],
                              "kind": "acknowledgement", "ticketId": ticket["id"]})
        self.take_up(ticket["id"], trigger["id"])
        return {"id": message_id, "status": self.outbox.status(message_id) or "pending", "reused": existing is not None}

    def _answer(self, request: Doc) -> Doc:
        purpose = request["purpose"]
        trigger = self.repo.trigger(request["triggerId"]) if request.get("triggerId") else None
        if request.get("triggerId") and not trigger:
            raise UserError("unknownTrigger")
        if request.get("ticketId"):
            ticket = self.require(request["ticketId"])
        elif trigger:
            ticket = next((t for t in self.repo.tickets(limit=500) if self.reaches(t, trigger)), None)
        else:
            ticket = None
        if purpose == "result":
            outcome = (ticket or {}).get("ended", {}).get("outcome") if ticket else None
            reported = bool(ticket) and live(ticket) and ((latest_assignment(ticket) or {}).get("report") or {}).get("state") == "ready"
            if not ticket or not (outcome in ("merged", "completed", "dropped") or reported):
                raise UserError("notReady")
        attention = (ticket or {}).get("attention") if purpose == "attention" else None
        if purpose == "attention" and (not attention or attention["question"] != request.get("question")):
            raise UserError("notAttention")
        if purpose == "answer" and not (trigger or {}).get("human"):
            raise UserError("notQuestion")
        message_id = key("attention-reply", attention["id"]) if attention else \
            key("reply", (trigger or {}).get("id") or (ticket or {}).get("id", ""), request["text"])
        existing = self.outbox.status(message_id)
        ready = not ticket or not trigger or trigger["id"] in ticket.get("answers", [])
        if purpose == "answer" and not ready and existing is None:
            raise UserError("noAnswer")
        asker = self.repo.trigger(attention["triggerId"]) if attention and attention.get("triggerId") else None
        if purpose == "answer":
            channel = trigger.get("channel")
        else:
            channel = (asker or {}).get("channel") or (self._source_channel(ticket) if ticket else None)
        if not channel:
            raise UserError("noChannel")
        self.outbox.send({"id": message_id, "channel": channel, "text": request["text"], "kind": "reply",
                          **({"ticketId": ticket["id"]} if ticket else {})})
        if purpose == "answer" and ticket and trigger and trigger["id"] in ticket.get("answers", []):
            ticket["answers"] = [i for i in ticket["answers"] if i != trigger["id"]]
            self.repo.save_ticket(ticket)
        return {"id": message_id, "status": self.outbox.status(message_id) or "pending", "reused": existing is not None}

    def _source_channel(self, ticket: Doc) -> Doc | None:
        return (self.origin(ticket) or {}).get("channel") or (ticket["channels"][0] if ticket["channels"] else None)
