"""Settings, the Policy built from them, and the checks an assessment must pass.

Who may start work is configured per Source, in that Source's own ids; a
person's accounts on two Sources are never mapped to each other. The gate is
code, never a model: an allowed sender who addresses the agent makes an
explicit request; every other message goes through the Source's admission
table, where each receiving scope (a GitHub scope, a Telegram group) has three
cells:

  listedUnaddressed     on the list, not addressing the agent   inferred | ignore     (default inferred)
  unlistedAddressed     not on the list, addressing the agent   explicit | inferred | ignore (default ignore)
  unlistedUnaddressed   neither                                 inferred | ignore     (default ignore)

Cells for unaddressed messages apply only where the Source receives every
message. Inferred candidates need an assessment against the team's rules and
are accepted only in `auto` mode; `observe` records what would have been
accepted; `off` admits nothing inferred.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .model import ACCEPT_MODES, Doc, key

POLICY_REVISION = "mandate-v3"
CELLS = ("listedUnaddressed", "unlistedAddressed", "unlistedUnaddressed")
CELL_OPTIONS = {"listedUnaddressed": ("inferred", "ignore"), "unlistedAddressed": ("explicit", "inferred", "ignore"),
                "unlistedUnaddressed": ("inferred", "ignore")}
CELL_DEFAULTS = {"listedUnaddressed": "inferred", "unlistedAddressed": "ignore", "unlistedUnaddressed": "ignore"}
STRENGTH = {"ignore": 0, "inferred": 1, "explicit": 2}
RULE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
WRITE_MODES = ("approve", "auto")

DEFAULT_RULES = [
    {"id": "copy", "name": "Copy fixes", "enabled": True,
     "instruction": "Explicit requests to correct a typo, wording, punctuation or an existing translation. Preserve "
                    "product promises and business meaning."},
    {"id": "ci", "name": "Broken build on the default branch", "enabled": False,
     "instruction": "The default branch's CI failed. Reproduce the failure; when the cause is a clear local defect, "
                    "fix it and open a pull request. When the cause needs a design decision, observe only."},
]

DEFAULTS: Doc = {
    "paused": False,
    "sources": {},
    "policy": {"rules": DEFAULT_RULES, "sources": {}},
    "eval": {"mode": "observe", "timeoutSeconds": 120, "model": ""},
    "executor": {"model": "", "maxWorkers": 3, "check": "", "maxTurns": 80},
    # xHarness: what a Channel write (a comment, a pull request, a group reply) needs.
    "writes": "approve",
}


class SettingsError(ValueError):
    pass


def _text(value: Any, name: str, limit: int, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise SettingsError(f"{name} must be text of at most {limit} characters")
    return value.strip()


def _entries(value: Any, name: str) -> list[Doc]:
    if not isinstance(value, list):
        raise SettingsError(f"{name} must be a list")
    out = []
    for entry in value:
        if not isinstance(entry, dict):
            raise SettingsError(f"{name} entries are {{id, name}}")
        out.append({"id": _text(entry.get("id"), f"{name}.id", 200), "name": _text(entry.get("name", ""), f"{name}.name", 100, True)})
    return out


def validate(raw: Any) -> Doc:
    """Settings with defaults filled in; raises SettingsError naming the first bad field."""
    if not isinstance(raw, dict):
        raise SettingsError("settings must be an object")
    settings = copy.deepcopy(DEFAULTS)
    settings["paused"] = bool(raw.get("paused", False))
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise SettingsError("sources must be an object")
    for sid, instance in sources.items():
        if not SOURCE_ID.match(sid) or not isinstance(instance, dict):
            raise SettingsError(f"source id {sid!r}: lower-case letters, digits and dashes")
        kind = _text(instance.get("kind", ""), f"sources.{sid}.kind", 40)
        config = instance.get("config", {})
        if not isinstance(config, dict):
            raise SettingsError(f"sources.{sid}.config must be an object")
        settings["sources"][sid] = {"kind": kind, "enabled": bool(instance.get("enabled", True)), "config": config}
    policy = raw.get("policy", {})
    if not isinstance(policy, dict):
        raise SettingsError("policy must be an object")
    if "rules" in policy:
        rules, seen = [], set()
        for rule in policy["rules"] if isinstance(policy["rules"], list) else []:
            rid = rule.get("id") if isinstance(rule, dict) else None
            if not isinstance(rid, str) or not RULE_ID.match(rid) or rid in seen:
                raise SettingsError(f"rule id {rid!r} is invalid or repeated")
            seen.add(rid)
            rules.append({"id": rid, "name": _text(rule.get("name"), "rule name", 80), "enabled": bool(rule.get("enabled", True)),
                          "instruction": _text(rule.get("instruction"), "rule instruction", 2000)})
        settings["policy"]["rules"] = rules
    for sid, part in (policy.get("sources") or {}).items():
        if not isinstance(part, dict):
            raise SettingsError(f"policy.sources.{sid} must be an object")
        admission = {}
        for row, cells in (part.get("admission") or {}).items():
            if not isinstance(cells, dict):
                raise SettingsError(f"admission row {row!r} must be an object")
            admission[str(row)] = {}
            for cell, value in cells.items():
                if cell not in CELLS or value not in CELL_OPTIONS[cell]:
                    raise SettingsError(f"admission {row}.{cell}: one of {', '.join(CELL_OPTIONS.get(cell, ()))}")
                admission[str(row)][cell] = value
        settings["policy"]["sources"][sid] = {"allowed": _entries(part.get("allowed", []), "allowed"),
                                              "bots": _entries(part.get("bots", []), "bots"), "admission": admission}
    evaluation = raw.get("eval", {})
    mode = evaluation.get("mode", "observe")
    if mode not in ACCEPT_MODES:
        raise SettingsError("eval.mode: off, observe or auto")
    timeout = evaluation.get("timeoutSeconds", 120)
    if not isinstance(timeout, int) or not 30 <= timeout <= 1800:
        raise SettingsError("eval.timeoutSeconds: 30 to 1800")
    settings["eval"] = {"mode": mode, "timeoutSeconds": timeout, "model": str(evaluation.get("model") or "")}
    executor = raw.get("executor", {})
    workers = executor.get("maxWorkers", 3)
    turns = executor.get("maxTurns", 80)
    if not isinstance(workers, int) or not 1 <= workers <= 16:
        raise SettingsError("executor.maxWorkers: 1 to 16")
    if not isinstance(turns, int) or not 5 <= turns <= 400:
        raise SettingsError("executor.maxTurns: 5 to 400")
    settings["executor"] = {"model": str(executor.get("model") or ""), "maxWorkers": workers,
                            "check": _text(executor.get("check", ""), "executor.check", 500, True), "maxTurns": turns}
    writes = raw.get("writes", "approve")
    if writes not in WRITE_MODES:
        raise SettingsError("writes: approve or auto")
    settings["writes"] = writes
    return settings


def policy_version(settings: Doc) -> str:
    """Only what bears on authorization: models, concurrency and pausing leave Decisions valid."""
    return key(POLICY_REVISION, json.dumps({"rules": settings["policy"]["rules"], "mode": settings["eval"]["mode"],
                                            "sources": settings["policy"]["sources"]}, sort_keys=True))


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def allowed_sender(settings: Doc, actor: Doc) -> Doc | None:
    part = settings["policy"]["sources"].get(actor.get("source", ""), {})
    return next((e for e in part.get("allowed", []) if _same(e["id"], actor.get("id", ""))), None)


def listed_bot(settings: Doc, actor: Doc) -> Doc | None:
    part = settings["policy"]["sources"].get(actor.get("source", ""), {})
    return next((e for e in part.get("bots", []) if _same(e["id"], actor.get("id", ""))), None)


def _admission(settings: Doc, source: str, row: str | None, cell: str) -> str:
    if row is None:
        return CELL_DEFAULTS[cell]
    return settings["policy"]["sources"].get(source, {}).get("admission", {}).get(row, {}).get(cell) or CELL_DEFAULTS[cell]


def _participating(thread: Doc, tickets: list[Doc]) -> bool:
    return any(i not in thread.get("context", []) for i in [*thread.get("subscriptions", []), *(t["id"] for t in tickets)])


@dataclass
class Policy:
    settings: Doc
    scope: Callable[[str, Doc], Doc]
    version: str = ""

    def __post_init__(self) -> None:
        self.version = policy_version(self.settings)

    @property
    def paused(self) -> bool:
        return bool(self.settings["paused"])

    @property
    def eval_timeout(self) -> float:
        return float(self.settings["eval"]["timeoutSeconds"])

    @property
    def mode(self) -> str:
        return self.settings["eval"]["mode"]

    def _rules_on(self, rules: list[str] | None) -> bool:
        enabled = {r["id"] for r in self.settings["policy"]["rules"] if r["enabled"]}
        return all(r in enabled for r in rules or [])

    def admits_inferred(self, rules: list[str]) -> bool:
        return self.mode == "auto" and bool(rules) and self._rules_on(rules)

    def _machine_gate(self, trigger: Doc) -> Doc:
        """Bots and CI: only a listed bot (inferred at most) or a senderless event the table admits."""
        actor = trigger["actor"]
        bot = listed_bot(self.settings, actor) if actor.get("id") else None
        if actor.get("id") and not bot:
            return {"admissible": False, "reason": "not_human"}
        scope = self.scope(trigger["source"], trigger.get("meta", {}))
        if not scope.get("listened") or not (trigger.get("addressed") or scope.get("observed")):
            return {"admissible": False, "reason": "not_human"}
        recorded = {"source": trigger["source"], "scope": trigger.get("meta", {}), **({"row": scope["row"]} if scope.get("row") else {})}
        addressed = {"addressed": trigger["addressed"]} if trigger.get("addressed") else {}
        if bot:
            if self.mode == "off":
                return {"admissible": False, "reason": "admission_ignored"}
            return {"admissible": True, "strength": "inferred", "basis": {**recorded, "via": "bot", "sender": bot["name"] or bot["id"],
                                                                         **addressed, "observation": {"mode": self.mode}}}
        cell = "unlistedUnaddressed"
        if _admission(self.settings, trigger["source"], scope.get("row"), cell) != "inferred" or self.mode == "off":
            return {"admissible": False, "reason": "admission_ignored"}
        return {"admissible": True, "strength": "inferred",
                "basis": {**recorded, "via": "admission", "cell": cell, "observation": {"mode": self.mode}}}

    def gate(self, trigger: Doc, thread: Doc, tickets: list[Doc]) -> Doc:
        if not trigger.get("human"):
            return self._machine_gate(trigger)
        allowed = allowed_sender(self.settings, trigger["actor"])
        scope = self.scope(trigger["source"], trigger.get("meta", {}))
        row = scope.get("row") if scope.get("listened") else None
        recorded = {"source": trigger["source"], "scope": trigger.get("meta", {}), **({"row": row} if row else {}),
                    **({"sender": allowed["name"] or allowed["id"]} if allowed else {})}
        addressed = trigger.get("addressed")
        if allowed and addressed:
            return {"admissible": True, "strength": "explicit", "basis": {**recorded, "via": "addressed", "addressed": addressed}}
        cell = ("listedUnaddressed" if not addressed else "") if allowed else ("unlistedAddressed" if addressed else "unlistedUnaddressed")
        receivable = bool(addressed) or (scope.get("listened") and scope.get("observed"))
        admission = _admission(self.settings, trigger["source"], row, cell) if receivable else "ignore"
        basis = {**recorded, "via": "admission", "cell": cell, **({"addressed": addressed} if addressed else {})}
        if admission == "explicit":
            return {"admissible": True, "strength": "explicit", "basis": basis}
        # A direct-message thread belongs to its sender; inferred follow-ups need a mode other than off.
        if not (addressed == "direct" and not allowed) and (allowed or self.mode != "off") and _participating(thread, tickets):
            return {"admissible": True, "strength": "explicit" if allowed else "inferred", "basis": {**recorded, "via": "followup"}}
        if admission == "inferred" and self.mode != "off":
            return {"admissible": True, "strength": "inferred", "basis": {**basis, "observation": {"mode": self.mode}}}
        reason = "not_a_member" if not allowed else ("admission_ignored" if receivable and admission == "ignore" else "no_mention")
        return {"admissible": False, "reason": reason}

    def revalidate(self, mandate: Doc) -> bool:
        """Whether a Mandate still holds under this Policy, checked before an ended ticket resumes."""
        basis = mandate["basis"]
        source = basis.get("source")
        current = self.scope(source, basis["scope"]) if source and basis.get("scope") is not None else None
        if current is not None and not current.get("listened"):
            return False
        via = basis["via"]
        if via == "followup":
            return mandate["strength"] == "explicit" or (self.mode == "auto" and self._rules_on(basis.get("rules")))
        if via == "bot" and not listed_bot(self.settings, mandate["actor"]):
            return False
        if via == "admission":
            cell = basis["cell"]
            listed = bool(allowed_sender(self.settings, mandate["actor"]))
            if cell == "unlistedAddressed" and listed:
                return True
            now = "listedUnaddressed" if cell == "unlistedUnaddressed" and listed else cell
            if now == "listedUnaddressed" and not listed:
                return False
            row = current.get("row") if current and current.get("listened") else None
            if STRENGTH[_admission(self.settings, source or "", row, now)] < STRENGTH[mandate["strength"]]:
                return False
            if mandate["strength"] == "explicit":
                return True
        elif via != "bot" and not allowed_sender(self.settings, mandate["actor"]):
            return False
        if via in ("admission", "listed", "bot") and basis.get("observation"):
            return self.mode == "auto" and self._rules_on(basis.get("rules"))
        return True


def policy_effects(tickets: list[Doc], old: Doc, new: Doc, scope: Callable[[Doc], Callable[[str, Doc], Doc]]) -> dict[str, Doc]:
    """How a settings change bears on each open ticket: its matched rules changed or were disabled, inferred work
    is no longer accepted, or its requester may no longer start work."""
    before, after = Policy(old, scope(old)), Policy(new, scope(new))
    effects: dict[str, Doc] = {}
    for ticket in tickets:
        rules = []
        for rid in ticket["mandate"]["basis"].get("rules") or []:
            was = next((r for r in old["policy"]["rules"] if r["id"] == rid), None)
            now = next((r for r in new["policy"]["rules"] if r["id"] == rid), None)
            if not now or not now["enabled"]:
                if was and was["enabled"]:
                    rules.append({"id": rid, "name": was["name"], "change": "disabled"})
            elif was != now:
                rules.append({"id": rid, "name": now["name"], "change": "changed", "instruction": now["instruction"]})
        effect: Doc = {"rules": rules} if rules else {}
        if before.revalidate(ticket["mandate"]) and not after.revalidate(ticket["mandate"]):
            as_before = {**new, "eval": old["eval"], "policy": {**new["policy"], "rules": old["policy"]["rules"]}}
            if not Policy(as_before, scope(new)).revalidate(ticket["mandate"]):
                effect.update({"requesterAllowed": False, "requester": ticket["mandate"]["actor"]})
            elif ticket["mandate"]["strength"] == "inferred" and old["eval"]["mode"] == "auto" and new["eval"]["mode"] != "auto":
                effect["proactive"] = False
        if effect:
            effects[ticket["id"]] = effect
    return effects


def direction_allowed(direction: Any) -> bool:
    """Implementation was asked for, the target is settled, and no design decision is open."""
    return isinstance(direction, dict) and direction.get("implementation_requested") is True \
        and direction.get("target_basis") in ("specified", "existing-standard") \
        and direction.get("requires_design_decision") is False and bool(str(direction.get("evidence", "")).strip())


def assessment_outcome(assessment: Doc, settings: Doc, strength: str) -> Doc:
    """Turn the main Session's assessment into an outcome. Every enabled rule is judged exactly once; an accept
    needs a settled direction, a matched rule and intent evidence; inferred work outside auto mode is recorded
    as "would accept" and ignored."""
    enabled = [r for r in settings["policy"]["rules"] if r["enabled"]]
    judged = assessment.get("rule_assessments") or []
    ids = [j.get("rule_id") for j in judged]
    if len(judged) != len(enabled) or len(set(ids)) != len(ids) or any(r["id"] not in ids for r in enabled):
        raise ValueError("judge every enabled rule exactly once: " + ", ".join(r["id"] for r in enabled))
    matches = [{"rule_id": j["rule_id"], "reason": j.get("reason", "")} for j in judged if j.get("matched")]
    audit = {"matches": matches, "rule_assessments": judged, "direction": assessment.get("direction")}
    if assessment.get("intent_evidence"):
        audit["intent_evidence"] = assessment["intent_evidence"]
    decision = assessment.get("decision")
    if decision != "accept":
        return {"kind": decision if decision in ("ignore", "unavailable") else "ignore", "reason": assessment.get("reason", ""), "audit": audit}
    if not direction_allowed(assessment.get("direction")):
        return {"kind": "ignore", "audit": audit, "reason": {"code": "directionUnsettled"}}
    if not matches:
        return {"kind": "ignore", "audit": audit, "reason": {"code": "noRuleMatched"}}
    if not assessment.get("intent_evidence"):
        raise ValueError("an accept needs intent_evidence")
    if strength == "inferred" and settings["eval"]["mode"] != "auto":
        audit.update({"would_accept": True, "proposal": {"title": assessment.get("title"), "scope": assessment.get("scope")}})
        return {"kind": "ignore", "audit": audit, "reason": {"code": "observed", "values": {"text": assessment.get("reason", "")}}}
    return {"kind": "accept", "title": assessment.get("title", ""), "scope": assessment.get("scope", ""),
            "reason": assessment.get("reason", ""), "rules": [m["rule_id"] for m in matches], "audit": audit,
            **({"acknowledgement": assessment["acknowledgement"]} if assessment.get("acknowledgement") else {})}
