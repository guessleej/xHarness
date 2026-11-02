"""The domain of the resident agent: its concepts and the rules each keeps.

The design follows dsh-superagent (github.com/deepseek-ai/dsh-superagent-prototype,
MIT) and its numbered documents; the names below are theirs so the two can be
read side by side.

  Source       a stream of events; remembers where it read up to
  Trigger      one immutable event, with its actor (in that Source's own ids)
  Thread       Triggers in time order (one PR, one chat topic); one Eval at a time
  Channel      the write side of a Thread (a comment, a reply in the topic)
  Policy       who may start work on each Source, the admission table, the rules,
               the accept mode (off / observe / auto); versioned
  Eval         gate (code) then assessment (model) of a batch of Triggers
  Decision     accept / stop / ignore / unavailable
  Mandate      what an accept grants, frozen on the Ticket: strength, scope,
               basis, Policy version. Message text never changes it
  Ticket       one unit of authorized work
  Assignment   a Ticket bound to one worker conversation
  Subscription Ticket <-> Thread: later Triggers go straight to the worker as
               context, no Eval, no new authority
  Input        what reaches a Ticket: Triggers, notes, Policy changes, text
  Report       progress / waiting / ready / blocked / failed / dropped
  Attention    a question on a Ticket waiting for a person

Aggregates are plain dicts (they are stored as JSON). Times are ISO strings in
UTC, so they compare as text.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

Doc = dict[str, Any]

REPORT_STATES = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
DECISION_KINDS = ("ignore", "accept", "stop", "unavailable")
OUTCOMES = ("completed", "merged", "closed", "dropped", "stopped")
ACCEPT_MODES = ("off", "observe", "auto")


def key(*values: str) -> str:
    """A stable id from content: equal inputs give the same id (idempotence)."""
    return hashlib.sha256(json.dumps(list(values), ensure_ascii=False).encode()).hexdigest()[:32]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_time(value: str) -> str:
    """Normalize a timestamp so stored times compare as text; anything unparsable is returned as is."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def thread_key(ref: Doc) -> str:
    return f"{ref['source']}:{ref['id']}"


# Reasons are codes plus values, rendered for the model and the page.
REASONS = {
    "said": "{text}",
    "timeout": "Assessment timed out",
    "policyChanged": "Policy changed during assessment",
    "noAdmissible": "No admissible trigger",
    "startedTicket": "Already started ticket {ticket}",
    "acceptedBefore": "Trigger {trigger} was accepted before",
    "routed": "Routed to ticket {ticket}",
    "retried": "Retried by a person",
    "hostRestarted": "xHarness restarted",
    "stopUnknown": "Stop target is unknown or already ended",
    "stopNotExplicit": "A stop needs an explicit request from a sender on the list",
    "directionUnsettled": "Implementation intent or target is not established, or a design decision is still required",
    "noRuleMatched": "No enabled rule matched",
    "ignoredFromCard": "Ignored from its card, without a rule assessment",
    "observed": "Observe mode: would accept ({text})",
    "not_human": "Not a person, and not a listed bot",
    "not_a_member": "The sender is not on the Source's list",
    "no_mention": "The agent was not addressed",
    "admission_ignored": "The admission table ignores this message",
}


def said(text: str) -> Doc:
    return {"code": "said", "values": {"text": text}}


def reason_text(reasons: Iterable[Doc] | Doc | None) -> str:
    items = [reasons] if isinstance(reasons, dict) else list(reasons or [])
    out = []
    for reason in items:
        text = REASONS.get(reason.get("code", ""), reason.get("code", ""))
        for name, value in (reason.get("values") or {}).items():
            text = text.replace("{" + name + "}", str(value))
        out.append(f"{reason['triggerId']}: {text}" if reason.get("triggerId") else text)
    return "; ".join(out)


def settled(decision: Doc) -> bool:
    """Whether a Decision settled what a person's message asks for (a stop or a routing leaves it to a ticket)."""
    return decision["kind"] not in ("stop", "unavailable") and not any(
        r.get("code") in ("stopNotExplicit", "stopUnknown", "routed") for r in decision.get("reason", []))


# Tickets

def live(ticket: Doc) -> bool:
    return not ticket.get("ended")


def latest_assignment(ticket: Doc) -> Doc | None:
    return ticket["assignments"][-1] if ticket["assignments"] else None


def running_assignment(ticket: Doc) -> Doc | None:
    return next((a for a in ticket["assignments"] if a["state"] == "running"), None)


def latest_report(ticket: Doc) -> Doc | None:
    for assignment in reversed(ticket["assignments"]):
        if assignment.get("report"):
            return assignment["report"]
    return None


def read_until(ticket: Doc, progress: bool = True) -> str:
    """Until when the worker had a turn to read what reached it: its latest report or turn end."""
    report = None
    for assignment in reversed(ticket["assignments"]):
        if assignment.get("report") and (progress or assignment["report"]["state"] != "progress"):
            report = assignment["report"]
            break
    return max([report["at"] if report else ""] + [a.get("idleAt") or "" for a in ticket["assignments"]])


def waits_for_person(ticket: Doc) -> bool:
    """Its latest Assignment reported blocked or failed and no person wrote to it since."""
    assignment = latest_assignment(ticket)
    report = assignment.get("report") if assignment else None
    if not report or report["state"] not in ("blocked", "failed"):
        return False
    return not any(i["kind"] == "trigger" and i.get("human") and not i.get("context") and i["at"] > report["at"]
                   for i in ticket["inputs"])


def automatic(item: Doc | None) -> bool:
    """An Input no person wrote: CI's, a bot's, the main Session's note."""
    return bool(item) and (item["kind"] == "note" or (item["kind"] == "trigger" and not item.get("human")))


def ticket_stage(ticket: Doc) -> Doc:
    """What the ticket waits for; the first matching row wins (person first, so the page lists those first)."""
    if ticket.get("ended"):
        return {"stage": "ended"}
    if ticket.get("delivery"):
        return {"stage": "person", "action": "delivery"}
    if ticket.get("attention"):
        return {"stage": "person", "action": "answer"}
    assignment = latest_assignment(ticket)
    report = (assignment or {}).get("report", {}) or {}
    state = report.get("state")
    if state == "ready":
        return {"stage": "person", "action": "review"}
    if state in ("blocked", "failed") and waits_for_person(ticket):
        return {"stage": "person", "action": state}
    if not assignment or assignment["state"] == "pending":
        return {"stage": "working", "action": "queued"}
    if state == "waiting" or assignment["state"] == "ended":
        return {"stage": "idle"}
    return {"stage": "working"}


def can_retry(ticket: Doc) -> bool:
    return live(ticket) and (waits_for_person(ticket) or bool(ticket.get("delivery")))


def reached(ticket: Doc, trigger: Doc, thread: Callable[[str], Doc | None], context: bool = True) -> bool:
    """Whether a Trigger reached a ticket; with context False, not only on a Thread it follows for context."""
    if ticket["origin"]["triggerId"] == trigger["id"]:
        return True
    as_input = any(i["kind"] == "trigger" and i["triggerId"] == trigger["id"] for i in ticket["inputs"])
    for ref in trigger["threads"]:
        current = thread(thread_key(ref))
        if current and ticket["id"] in current["subscriptions"] and (context or ticket["id"] not in current.get("context", [])):
            return True
    if not as_input:
        return False
    return context or not all(ticket["id"] in ((thread(thread_key(ref)) or {}).get("context") or []) for ref in trigger["threads"])


def followup_owners(trigger: Doc, tickets: Callable[[str], Doc | None], threads: Callable[[str], Doc | None],
                    disposition: Callable[[str], Doc | None]) -> list[str]:
    """The tickets whose worker decides a person's follow-up: those it reached on their own Threads that can
    still decide it and did not hand it back. While a ticket owns it, the message takes no Eval."""
    if not trigger.get("human"):
        return []
    record = disposition(trigger["id"])
    if not record or record.get("requeued"):
        return []
    returned = {h["ticketId"] for h in record.get("handbacks", [])}
    reached_ids = [t for t in record.get("fanout", []) if t not in returned and t not in record.get("context", [])]
    primary = threads(thread_key(trigger["threads"][0])) or {}
    if not reached_ids or trigger["id"] in ((primary.get("evaluating") or {}).get("triggerIds") or []):
        return []
    owners = []
    for ticket_id in reached_ids:
        ticket = tickets(ticket_id)
        if not ticket:
            continue
        if live(ticket):
            owners.append(ticket_id)
            continue
        waiting = any(i["kind"] == "trigger" and i["triggerId"] == trigger["id"] and i["id"] in ticket["undelivered"]
                      for i in ticket["inputs"])
        if (ticket.get("ended") or {}).get("by") != "human" and waiting:
            owners.append(ticket_id)
    return owners


class UserError(Exception):
    """A refused request, with a code the tools and the page explain."""

    MESSAGES = {
        "unknownTrigger": "No such trigger",
        "unknownTicket": "No such ticket",
        "ticketEnded": "The ticket has ended",
        "ticketStopped": "A person stopped this ticket",
        "mandateRevoked": "The current Policy no longer allows this ticket's work",
        "workerDecides": "The worker of ticket(s) {tickets} decides this message; only a stop may be routed",
        "notAdmissible": "The gate does not admit this message ({reason})",
        "notExplicit": "Only an explicit request may be accepted directly; inferred work needs an assessment in auto mode",
        "evalFinished": "Eval {eval} has finished",
        "notRetryable": "The ticket is not waiting for a retry",
        "notReached": "That message did not reach this ticket",
        "handedBack": "This ticket already handed that message back",
        "handedBackHere": "This ticket's worker handed that message back; route it elsewhere",
        "sourceThread": "A ticket cannot drop the Thread it started on",
        "followedThread": "A ticket cannot drop a Thread its Source made it follow",
        "notFollowing": "The ticket does not follow that Thread",
        "questionWithResult": "A question cannot come with a ready result or an answer",
        "askerWithoutQuestion": "asked_by needs a question",
        "notAsker": "asked_by must name a person's message on this ticket",
        "notAnswerable": "answers must name a person's message that reached this ticket",
        "unknownAssignment": "The ticket has no assignment",
        "assignmentEnded": "The assignment has ended",
        "ackEnded": "The ticket has ended",
        "ackNoChannel": "That message has no conversation to reply in",
        "ackNotPerson": "Only a person's message is acknowledged",
        "notReady": "A result goes out only once the ticket is ready or ended",
        "notAttention": "That is not the ticket's open question",
        "notQuestion": "An answer replies to a person's message",
        "noAnswer": "The worker has not answered that message",
        "noChannel": "There is no conversation to reply in",
        "unknownProblem": "No such problem",
    }

    def __init__(self, code: str, values: dict[str, str] | None = None) -> None:
        self.code = code
        self.values = values or {}
        text = self.MESSAGES.get(code, code)
        for name, value in self.values.items():
            text = text.replace("{" + name + "}", str(value))
        super().__init__(text)
