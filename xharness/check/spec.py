"""Check agent intake spec: one folder per agent, no change to the platform core.

  <agents_dir>/<id>/
    agent.toml          identity, version, triggers, required fields, risk, retry
    rules.py            the rules (see rulekit); checked before it is loaded
    tables.schema.toml  shape of the decision tables the agency fills in
    cases/*.json        validation cases: record + the rule ids the agency expects
    README.md           intake notes (part of the delivery)

Agents ship in xharness/check/agents/ and in every directory listed in
``[check] agents_dirs``; a later directory overrides an earlier one by id.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import rulekit

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "agents")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
RISKS = {"low": 0.0, "medium": 0.10, "high": 0.10}
TRIGGERS = {"sync", "batch"}
NEEDS = {"ocr", "model"}
COLUMN_TYPES = {"text", "number", "date", "bool"}
BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "input", "breakpoint", "memoryview", "help"}
SAFE_BUILTINS = {name: getattr(builtins, name) for name in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
    "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum",
    "tuple", "zip", "ValueError", "TypeError", "KeyError", "None", "True", "False")}


class SpecError(ValueError):
    pass


@dataclass
class Column:
    name: str
    type: str = "text"
    title: str = ""


@dataclass
class TableSpec:
    name: str
    title: str
    columns: list[Column]
    key: list[str]


@dataclass
class Case:
    name: str
    record: dict[str, Any]
    expect: list[str]


@dataclass
class AgentSpec:
    id: str
    name: str
    version: str
    path: str
    description: str = ""
    triggers: list[str] = field(default_factory=lambda: ["sync", "batch"])
    needs: list[str] = field(default_factory=list)
    business_key: str = "id"
    required: list[str] = field(default_factory=list)
    person_field: str = ""
    date_field: str = ""
    risk: str = "low"
    sample_rate: float = 0.0
    depends: list[str] = field(default_factory=list)
    retry_attempts: int = 3
    retry_backoff: float = 2.0
    rules: list[rulekit.Rule] = field(default_factory=list)
    tables: dict[str, TableSpec] = field(default_factory=dict)
    cases: list[Case] = field(default_factory=list)
    test_tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    assists: list[dict[str, Any]] = field(default_factory=list)
    digest: str = ""

    @property
    def is_async(self) -> bool:
        return bool(self.needs or self.assists)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "version": self.version, "description": self.description,
            "triggers": self.triggers, "needs": self.needs, "business_key": self.business_key,
            "required": self.required, "risk": self.risk, "sample_rate": self.sample_rate,
            "depends": self.depends, "digest": self.digest[:12],
            "assists": [{k: a.get(k) for k in ("id", "kind", "model", "outputs", "min_confidence")} for a in self.assists],
            "rules": [{"id": r.id, "title": r.title, "basis": r.basis} for r in self.rules],
            "tables": {name: {"title": t.title, "key": t.key,
                              "columns": [{"name": c.name, "type": c.type, "title": c.title or c.name} for c in t.columns]}
                       for name, t in self.tables.items()},
        }


def check_rules_source(source: str, filename: str = "rules.py") -> None:
    """Refuse imports outside rulekit.ALLOWED_IMPORTS, file/exec builtins and dunder access."""
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as error:
        raise SpecError(f"{filename}: 語法錯誤 line {error.lineno}: {error.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in rulekit.ALLOWED_IMPORTS:
                    raise SpecError(f"{filename}:{node.lineno} 不准 import {alias.name}（只允許 {sorted(rulekit.ALLOWED_IMPORTS)}）")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in rulekit.ALLOWED_IMPORTS:
                raise SpecError(f"{filename}:{node.lineno} 不准 from {node.module or '.'} import（只允許 {sorted(rulekit.ALLOWED_IMPORTS)}）")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise SpecError(f"{filename}:{node.lineno} 規則不准使用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SpecError(f"{filename}:{node.lineno} 規則不准存取 {node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise SpecError(f"{filename}:{node.lineno} 規則不准用 global／nonlocal")


def _guarded_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    if level or name not in rulekit.ALLOWED_IMPORTS:
        raise ImportError(f"rules may not import {name}")
    return __import__(name, globals_, locals_, fromlist, level)


def load_rules(path: str) -> list[rulekit.Rule]:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    check_rules_source(source, path)
    namespace: dict[str, Any] = {"__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import}, "__name__": "check_rules"}
    rulekit.take_collected()
    exec(compile(source, path, "exec"), namespace)  # nosec B102 -- source passed check_rules_source; restricted builtins
    rules = rulekit.take_collected()
    if not rules:
        raise SpecError(f"{path}: 沒有任何 @rule")
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise SpecError(f"{path}: 規則編號重複")
    return rules


def _toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _tables(path: str) -> dict[str, TableSpec]:
    if not os.path.exists(path):
        return {}
    tables: dict[str, TableSpec] = {}
    for name, spec in _toml(path).items():
        columns = [Column(str(c["name"]), str(c.get("type", "text")), str(c.get("title", ""))) for c in spec.get("columns", [])]
        for column in columns:
            if column.type not in COLUMN_TYPES:
                raise SpecError(f"{path}: 表 {name} 欄位 {column.name} 型別 {column.type} 不在 {sorted(COLUMN_TYPES)}")
        names = {c.name for c in columns}
        key = [str(k) for k in spec.get("key", [])]
        if not columns or not set(key) <= names:
            raise SpecError(f"{path}: 表 {name} 要有欄位，key 必須是欄位之一")
        tables[name] = TableSpec(name, str(spec.get("title", name)), columns, key)
    return tables


def load_agent(path: str) -> AgentSpec:
    meta_path = os.path.join(path, "agent.toml")
    if not os.path.exists(meta_path):
        raise SpecError(f"{path}: 缺 agent.toml")
    meta = _toml(meta_path)
    agent_id = str(meta.get("id", ""))
    if not ID_RE.match(agent_id):
        raise SpecError(f"{meta_path}: id 只能用小寫英數與連字號，2～48 字")
    for key in ("name", "version"):
        if not meta.get(key):
            raise SpecError(f"{meta_path}: 缺 {key}")
    triggers = [str(t) for t in meta.get("triggers", ["sync", "batch"])]
    needs = [str(n) for n in meta.get("needs", [])]
    risk = str(meta.get("risk", "low"))
    if not set(triggers) <= TRIGGERS or not set(needs) <= NEEDS or risk not in RISKS:
        raise SpecError(f"{meta_path}: triggers 限 {sorted(TRIGGERS)}、needs 限 {sorted(NEEDS)}、risk 限 {sorted(RISKS)}")
    # AI risk rule: medium and high risk scenarios keep at least 10% human sampling.
    sample_rate = max(float(meta.get("sample_rate", 0.0)), RISKS[risk])
    retry = meta.get("retry", {})
    rules_path = os.path.join(path, "rules.py")
    rules = load_rules(rules_path)
    digest = hashlib.sha256()
    for name in ("agent.toml", "rules.py", "tables.schema.toml"):
        file_path = os.path.join(path, name)
        if os.path.exists(file_path):
            with open(file_path, "rb") as handle:
                digest.update(handle.read())
    spec = AgentSpec(
        id=agent_id, name=str(meta["name"]), version=str(meta["version"]), path=path,
        description=str(meta.get("description", "")), triggers=triggers, needs=needs,
        business_key=str(meta.get("business_key", "id")), required=[str(f) for f in meta.get("required", [])],
        person_field=str(meta.get("person_field", "")), date_field=str(meta.get("date_field", "")),
        risk=risk, sample_rate=min(sample_rate, 1.0), depends=[str(d) for d in meta.get("depends", [])],
        retry_attempts=max(1, int(retry.get("attempts", 3))), retry_backoff=float(retry.get("backoff_seconds", 2.0)),
        rules=rules, tables=_tables(os.path.join(path, "tables.schema.toml")), digest=digest.hexdigest(),
        assists=[dict(a) for a in meta.get("assists", []) if isinstance(a, dict)],
    )
    cases_dir = os.path.join(path, "cases")
    if os.path.isdir(cases_dir):
        for name in sorted(os.listdir(cases_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(cases_dir, name), encoding="utf-8") as handle:
                data = json.load(handle)
            if name == "tables.json":
                spec.test_tables = data
                continue
            for index, item in enumerate(data if isinstance(data, list) else [data]):
                spec.cases.append(Case(str(item.get("name", f"{name}#{index}")), dict(item["record"]), [str(r) for r in item.get("expect", [])]))
    return spec


def discover(extra_dirs: list[str] | None = None) -> dict[str, AgentSpec]:
    """All agents by id. A broken agent is reported, not fatal for the others."""
    agents: dict[str, AgentSpec] = {}
    for base in [BUILTIN_DIR, *(extra_dirs or [])]:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if not os.path.isfile(os.path.join(path, "agent.toml")):
                continue
            try:
                spec = load_agent(path)
            except (SpecError, OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as error:
                print(f"[xharness check] 略過 {path}: {error}")
                continue
            agents[spec.id] = spec
    return agents
