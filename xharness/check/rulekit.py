"""What a check agent's rules.py may use: the @rule decorator and a few helpers.

A rule is a plain function ``(record, tables) -> dict | None``. Return None when
the record passes; return a dict when the person should be told something. The
dict fills the rule's message template and may carry:

  fields     the record fields the finding is based on (list of dotted paths)
  evidence   where in the documents it was seen ([{doc, page, field, value}])
  level      "notice" (default) or "manual" (cannot decide, send to a person)
  fingerprint  values that identify the same real-world event across agents
               (person, date, amount ...); equal fingerprints merge into one case

Rules see only the record and the decision tables; spec.load_rules() refuses a
rules.py that imports anything outside ALLOWED_IMPORTS or touches open/exec/
__import__, so a rule pack cannot reach the network or the disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

ALLOWED_IMPORTS = {"datetime", "decimal", "math", "re", "xharness.check.rulekit"}


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    basis: str
    message: str
    func: Callable[[dict[str, Any], "Tables"], Any]


_collected: list[Rule] = []


def rule(rule_id: str, *, title: str, basis: str, message: str) -> Callable:
    """Register a rule. ``basis`` is the regulation or approved table it rests on."""

    def wrap(func: Callable) -> Callable:
        _collected.append(Rule(rule_id, title, basis, message, func))
        return func

    return wrap


def take_collected() -> list[Rule]:
    rules = list(_collected)
    _collected.clear()
    return rules


@dataclass
class Tables:
    """Decision tables as of the record's data basis date. Values are set by the agency."""

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)

    def get(self, name: str) -> list[dict[str, Any]]:
        return self.rows.get(name, [])

    def find(self, name: str, **match: Any) -> dict[str, Any] | None:
        for row in self.get(name):
            if all(str(row.get(key, "")) == str(value) for key, value in match.items()):
                return row
        return None


def dig(record: dict[str, Any], path: str, default: Any = None) -> Any:
    """``dig(rec, "trips.0.amount")`` -- read a dotted path without KeyError."""
    value: Any = record
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default
    return value


def number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
