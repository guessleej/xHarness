"""國內差旅費交通費檢核規則。數值（上限、等級）一律來自機關核定的判定值表。"""

from xharness.check.rulekit import dig, number, rule


def _legs(record):
    legs = dig(record, "legs", [])
    return legs if isinstance(legs, list) else []


def _route(leg):
    return f"{leg.get('from', '')}→{leg.get('to', '')}（{leg.get('mode', '')}）"


@rule("TF-01", title="交通費高於票價上限",
      basis="國內出差旅費報支要點（交通費）；機關核定票價上限表",
      message="第{n}段 {route} 報支 {amount:,.0f} 元，高於上限 {cap:,.0f} 元，請確認是否有特殊事由")
def over_cap(record, tables):
    out = []
    for index, leg in enumerate(_legs(record)):
        row = tables.find("fare_cap", **{"from": leg.get("from"), "to": leg.get("to"), "mode": leg.get("mode")})
        amount, cap = number(leg.get("amount")), number(row.get("cap")) if row else None
        if row and amount is not None and cap is not None and amount > cap:
            out.append({"n": index + 1, "route": _route(leg), "amount": amount, "cap": cap,
                        "fields": [f"legs.{index}.amount", f"legs.{index}.from", f"legs.{index}.to", f"legs.{index}.mode"],
                        "fingerprint": {"person": record.get("traveler"), "date": leg.get("date") or record.get("trip_date"),
                                        "amount": amount}})
    return out


@rule("TF-02", title="查無票價上限",
      basis="機關核定票價上限表",
      message="第{n}段 {route} 不在票價上限表內，無法比對，轉人工")
def no_cap(record, tables):
    out = []
    for index, leg in enumerate(_legs(record)):
        if not tables.find("fare_cap", **{"from": leg.get("from"), "to": leg.get("to"), "mode": leg.get("mode")}):
            out.append({"n": index + 1, "route": _route(leg), "level": "manual",
                        "fields": [f"legs.{index}.from", f"legs.{index}.to", f"legs.{index}.mode"]})
    return out


@rule("TF-03", title="車廂等級不在可報支範圍",
      basis="國內出差旅費報支要點（交通費）；機關核定可報支車廂等級",
      message="第{n}段 {route} 搭乘「{cls}」，不在可報支等級內")
def cabin_class(record, tables):
    out = []
    for index, leg in enumerate(_legs(record)):
        cls = leg.get("class") or "標準"
        if not tables.find("class_allowed", mode=leg.get("mode"), **{"class": cls}):
            out.append({"n": index + 1, "route": _route(leg), "cls": cls, "fields": [f"legs.{index}.class"]})
    return out


@rule("TF-04", title="同日重複行程",
      basis="國內出差旅費報支要點（核實報支）",
      message="第{n}段與第{first}段同日同路線同交通工具，請確認是否重複報支")
def duplicate_leg(record, tables):
    seen = {}
    out = []
    for index, leg in enumerate(_legs(record)):
        key = (leg.get("date") or record.get("trip_date"), leg.get("from"), leg.get("to"), leg.get("mode"))
        if key in seen:
            out.append({"n": index + 1, "first": seen[key] + 1, "fields": [f"legs.{index}", f"legs.{seen[key]}"]})
        else:
            seen[key] = index
    return out
