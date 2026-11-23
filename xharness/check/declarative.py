"""Check agents designed on the web page: a JSON definition instead of a rules.py.

A rule is data, never code, so a designer on the page cannot run anything on the
server. Shape (all keys validated by validate()):

  {"id": "R-01", "title": "...", "basis": "...", "level": "notice"|"manual",
   "each": "legs",                       # optional: check every item of this list
   "when": <condition>,
   "values": {"cap": <operand>},          # optional: extra values for the message
   "message": "第{n}段 報支 {amount} 元，高於上限 {cap} 元"}

  condition  {"all": [...]} | {"any": [...]} | {"not": {...}}
             | {"left": <operand>, "op": "gt", "right": <operand>}
  operand    {"field": "amount"}           item field (inside "each"), else record field
             {"record": "trip_date"}       always the record
             {"value": 700}
             {"lookup": {"table": "fare_cap", "match": {"from": <operand>, ...}, "column": "cap"}}

Assist sub-agents ("assists") are stored with the definition; they run in the
document path in a later release and only ever produce fields, never findings.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import rulekit
from .spec import COLUMN_TYPES, ID_RE, NEEDS, RISKS, TRIGGERS, AgentSpec, Case, Column, SpecError, TableSpec

OPS = {
    "gt": "大於", "gte": "大於等於", "lt": "小於", "lte": "小於等於", "eq": "等於", "ne": "不等於",
    "contains": "包含", "empty": "是空的", "not_empty": "不是空的", "found": "查得到", "not_found": "查不到",
}
UNARY = {"empty", "not_empty", "found", "not_found"}
RULE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,15}$")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")
ASSIST_KINDS = {"extract": "擷取欄位", "compare": "比對條文或範本", "summarize": "摘要"}
MAX_RULES, MAX_DEPTH, MAX_CASES = 60, 6, 200
MISSING = object()


# --- validation ---------------------------------------------------------------

def _operand(op: Any, where: str, tables: dict[str, Any], depth: int = 0) -> None:
    if not isinstance(op, dict) or len(op) != 1:
        raise SpecError(f"{where}：運算元格式錯誤")
    kind, value = next(iter(op.items()))
    if kind in ("field", "record"):
        if not isinstance(value, str) or not value or len(value) > 120:
            raise SpecError(f"{where}：欄位名稱不對")
    elif kind == "value":
        if not isinstance(value, (str, int, float, bool)) or (isinstance(value, str) and len(value) > 500):
            raise SpecError(f"{where}：固定值只能是文字或數字")
    elif kind == "lookup":
        if depth > 1 or not isinstance(value, dict):
            raise SpecError(f"{where}：查表格式錯誤")
        table = value.get("table")
        if table not in tables:
            raise SpecError(f"{where}：沒有判定值表「{table}」")
        columns = {c["name"] for c in tables[table]["columns"]}
        if value.get("column") not in columns:
            raise SpecError(f"{where}：表「{table}」沒有欄位「{value.get('column')}」")
        match = value.get("match")
        if not isinstance(match, dict) or not match or not set(match) <= columns:
            raise SpecError(f"{where}：查表條件要對到表「{table}」的欄位")
        for column, inner in match.items():
            _operand(inner, f"{where}／{column}", tables, depth + 1)
    else:
        raise SpecError(f"{where}：不認得的運算元「{kind}」")


def _condition(cond: Any, where: str, tables: dict[str, Any], depth: int = 0) -> None:
    if depth > MAX_DEPTH or not isinstance(cond, dict):
        raise SpecError(f"{where}：條件太深或格式錯誤")
    if "all" in cond or "any" in cond:
        items = cond.get("all", cond.get("any"))
        if not isinstance(items, list) or not items or len(items) > 20:
            raise SpecError(f"{where}：「全部／任一」要有 1～20 個條件")
        for index, item in enumerate(items, 1):
            _condition(item, f"{where}-{index}", tables, depth + 1)
        return
    if "not" in cond:
        _condition(cond["not"], where, tables, depth + 1)
        return
    if cond.get("op") not in OPS:
        raise SpecError(f"{where}：比較方式要是 {', '.join(OPS)}")
    _operand(cond.get("left"), f"{where} 左邊", tables)
    if cond["op"] not in UNARY:
        _operand(cond.get("right"), f"{where} 右邊", tables)


def validate(definition: Any) -> dict[str, Any]:
    """Return a cleaned definition or raise SpecError with a sentence a designer can act on."""
    if not isinstance(definition, dict):
        raise SpecError("定義要是物件")
    d = definition
    if not ID_RE.match(str(d.get("id", ""))):
        raise SpecError("代號只能用小寫英數與連字號，2～48 字")
    if not str(d.get("name", "")).strip():
        raise SpecError("請填名稱")
    risk = str(d.get("risk", "low"))
    triggers = [str(t) for t in d.get("triggers", ["sync", "batch"])]
    needs = [str(n) for n in d.get("needs", [])]
    if risk not in RISKS or not triggers or not set(triggers) <= TRIGGERS or not set(needs) <= NEEDS:
        raise SpecError("風險、觸發方式或附件設定不對")
    fields = d.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 200:
        raise SpecError("欄位最多 200 個")
    for field in fields:
        if not isinstance(field, dict) or not str(field.get("name", "")) or field.get("type", "text") not in COLUMN_TYPES | {"list"}:
            raise SpecError("欄位要有名稱，型別限文字、數字、日期、是否、清單")
    business_key = str(d.get("business_key", ""))
    if not business_key:
        raise SpecError("請指定哪個欄位是業務單號")
    tables = d.get("tables", {})
    if not isinstance(tables, dict) or len(tables) > 20:
        raise SpecError("判定值表最多 20 張")
    for name, table in tables.items():
        if not NAME.match(name) or not isinstance(table, dict):
            raise SpecError(f"表代號「{name}」只能用英數與底線")
        columns = table.get("columns", [])
        if not columns or not all(isinstance(c, dict) and NAME.match(str(c.get("name", ""))) and c.get("type", "text") in COLUMN_TYPES
                                  for c in columns):
            raise SpecError(f"表「{name}」的欄位代號只能用英數與底線，型別限文字、數字、日期、是否")
        if not table.get("key") or not set(table["key"]) <= {c["name"] for c in columns}:
            raise SpecError(f"表「{name}」要指定鍵值欄位")
    rules = d.get("rules", [])
    if not isinstance(rules, list) or not rules or len(rules) > MAX_RULES:
        raise SpecError(f"至少要有 1 條規則，最多 {MAX_RULES} 條")
    seen = set()
    for index, rule in enumerate(rules, 1):
        where = f"第 {index} 條規則"
        if not isinstance(rule, dict) or not RULE_ID.match(str(rule.get("id", ""))) or rule["id"] in seen:
            raise SpecError(f"{where}：規則編號要是英文開頭、不重複（例 R-01）")
        seen.add(rule["id"])
        for key in ("title", "basis", "message"):
            if not str(rule.get(key, "")).strip():
                raise SpecError(f"{where}：請填{ {'title': '名稱', 'basis': '依據', 'message': '提示文字'}[key] }")
        if rule.get("level", "notice") not in ("notice", "manual"):
            raise SpecError(f"{where}：等級只能是提示或轉人工")
        _condition(rule.get("when"), where, tables)
        for name, operand in (rule.get("values") or {}).items():
            if not NAME.match(str(name)):
                raise SpecError(f"{where}：訊息變數「{name}」只能用英數與底線")
            _operand(operand, f"{where} 變數 {name}", tables)
    for assist in d.get("assists", []):
        if not isinstance(assist, dict) or assist.get("kind") not in ASSIST_KINDS or not NAME.match(str(assist.get("id", ""))):
            raise SpecError("輔助子Agent 要有代號，用途限擷取欄位、比對、摘要")
        outputs = assist.get("outputs", [])
        if not isinstance(outputs, list) or not all(NAME.match(str(o)) for o in outputs):
            raise SpecError(f"輔助子Agent「{assist['id']}」的產出欄位只能用英數與底線")
        if not str(assist.get("model", "")).strip():
            raise SpecError(f"輔助子Agent「{assist['id']}」要選模型")
        if not outputs:
            raise SpecError(f"輔助子Agent「{assist['id']}」至少要有一個產出欄位")
        confidence = float(assist.get("min_confidence", 0.8))
        if not 0 <= confidence <= 1:
            raise SpecError("信心門檻要在 0～1 之間")
    cases = d.get("cases", [])
    if not isinstance(cases, list) or len(cases) > MAX_CASES:
        raise SpecError(f"驗證案例最多 {MAX_CASES} 個")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("record"), dict) or not isinstance(case.get("expect", []), list):
            raise SpecError("每個驗證案例要有 record（物件）與 expect（規則編號清單）")
    if len(json.dumps(d, ensure_ascii=False)) > 500_000:
        raise SpecError("定義太大")
    return d


# --- evaluation ---------------------------------------------------------------

def _resolve(op: dict[str, Any], item: dict[str, Any], record: dict[str, Any], tables: rulekit.Tables) -> Any:
    kind, value = next(iter(op.items()))
    if kind == "value":
        return value
    if kind == "record":
        return rulekit.dig(record, value, MISSING)
    if kind == "field":
        found = rulekit.dig(item, value, MISSING)
        return found if found is not MISSING or item is record else rulekit.dig(record, value, MISSING)
    # lookup
    match = {column: _resolve(inner, item, record, tables) for column, inner in value["match"].items()}
    row = tables.find(value["table"], **{k: ("" if v is MISSING else v) for k, v in match.items()})
    return MISSING if row is None else row.get(value["column"], MISSING)


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "empty":
        return left is MISSING or left in ("", None, [])
    if op == "not_empty":
        return not (left is MISSING or left in ("", None, []))
    if op == "found":
        return left is not MISSING
    if op == "not_found":
        return left is MISSING
    if left is MISSING or right is MISSING:
        return False
    if op == "contains":
        return str(right) in (left if isinstance(left, list) else str(left))
    a, b = rulekit.number(left), rulekit.number(right)
    if op in ("eq", "ne"):
        same = (a == b) if a is not None and b is not None else str(left) == str(right)
        return same if op == "eq" else not same
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _test(cond: dict[str, Any], item: dict[str, Any], record: dict[str, Any], tables: rulekit.Tables) -> bool:
    if "all" in cond:
        return all(_test(c, item, record, tables) for c in cond["all"])
    if "any" in cond:
        return any(_test(c, item, record, tables) for c in cond["any"])
    if "not" in cond:
        return not _test(cond["not"], item, record, tables)
    left = _resolve(cond["left"], item, record, tables)
    right = MISSING if cond["op"] in UNARY else _resolve(cond["right"], item, record, tables)
    return _compare(left, cond["op"], right)


def _fields(cond: Any, prefix: str) -> list[str]:
    out: list[str] = []
    if isinstance(cond, dict):
        for key, value in cond.items():
            if key == "field" and isinstance(value, str):
                out.append(prefix + value)
            elif key == "record" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_fields(value, prefix))
    elif isinstance(cond, list):
        for value in cond:
            out.extend(_fields(value, prefix))
    return out


def _show(value: Any) -> Any:
    if value is MISSING or value is None:
        return "（無）"
    number = rulekit.number(value) if not isinstance(value, bool) else None
    if number is not None and not isinstance(value, str):
        return f"{number:,.0f}" if number == int(number) else f"{number:,}"
    if number is not None and isinstance(value, str) and re.fullmatch(r"-?[\d,]+(\.\d+)?", value):
        return f"{number:,.0f}" if number == int(number) else f"{number:,}"
    return value


def compile_rule(rule: dict[str, Any]) -> rulekit.Rule:
    each = str(rule.get("each", "") or "")

    def func(record: dict[str, Any], tables: rulekit.Tables) -> list[dict[str, Any]]:
        items = rulekit.dig(record, each, []) if each else [record]
        if not isinstance(items, list):
            return []
        out = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not _test(rule["when"], item, record, tables):
                continue
            prefix = f"{each}.{index}." if each else ""
            values = {k: _show(v) for k, v in record.items() if not isinstance(v, (dict, list))}
            values.update({k: _show(v) for k, v in item.items() if not isinstance(v, (dict, list))})
            values.update({name: _show(_resolve(op, item, record, tables)) for name, op in (rule.get("values") or {}).items()})
            values["n"] = index + 1
            fields = sorted(set(_fields(rule["when"], prefix))) or [each or "record"]
            out.append({**values, "fields": fields, "level": rule.get("level", "notice")})
        return out

    return rulekit.Rule(rule["id"], rule["title"], rule["basis"], rule["message"], func)


def digest(definition: dict[str, Any]) -> str:
    body = {k: v for k, v in definition.items() if k not in ("cases", "test_tables")}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def to_spec(definition: dict[str, Any], version: str) -> AgentSpec:
    d = validate(definition)
    tables = {name: TableSpec(name, str(t.get("title", name)),
                              [Column(str(c["name"]), str(c.get("type", "text")), str(c.get("title", ""))) for c in t["columns"]],
                              [str(k) for k in t["key"]]) for name, t in d.get("tables", {}).items()}
    risk = str(d.get("risk", "low"))
    return AgentSpec(
        id=d["id"], name=str(d["name"]).strip(), version=version, path="web", description=str(d.get("description", "")),
        triggers=[str(t) for t in d.get("triggers", ["sync", "batch"])], needs=[str(n) for n in d.get("needs", [])],
        business_key=str(d["business_key"]), required=[str(f) for f in d.get("required", [])],
        person_field=str(d.get("person_field", "")), date_field=str(d.get("date_field", "")),
        risk=risk, sample_rate=min(max(float(d.get("sample_rate", 0.0)), RISKS[risk]), 1.0),
        rules=[compile_rule(r) for r in d["rules"]], tables=tables,
        cases=[Case(str(c.get("name", f"案例{i + 1}")), dict(c["record"]), [str(r) for r in c.get("expect", [])])
               for i, c in enumerate(d.get("cases", []))],
        test_tables={k: list(v) for k, v in (d.get("test_tables") or {}).items()}, digest=digest(d),
        assists=[dict(a) for a in d.get("assists", [])],
    )


def fields_from_sample(sample: dict[str, Any]) -> list[dict[str, Any]]:
    """Step 2 helper: guess fields and types from one pasted record."""
    out = []
    for name, value in sample.items():
        if isinstance(value, list):
            inner = value[0] if value and isinstance(value[0], dict) else {}
            out.append({"name": name, "type": "list", "items": fields_from_sample(inner) if inner else []})
        elif isinstance(value, bool):
            out.append({"name": name, "type": "bool"})
        elif isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"-?[\d,]+(\.\d+)?", value)):
            out.append({"name": name, "type": "number"})
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value):
            out.append({"name": name, "type": "date"})
        else:
            out.append({"name": name, "type": "text"})
    return out
