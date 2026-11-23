"""核銷附件缺漏檢核：應附單據清單由機關維護。"""

from xharness.check.rulekit import dig, rule


@rule("AC-01", title="應附單據缺漏",
      basis="機關核定各類核銷應附單據",
      message="「{claim_type}」應附「{label}」，本次未附")
def missing_attachment(record, tables):
    attached = {str(item.get("kind")) for item in dig(record, "attachments", []) if isinstance(item, dict)}
    out = []
    for row in tables.get("required_attachments"):
        if row.get("claim_type") == record.get("claim_type") and str(row.get("kind")) not in attached:
            out.append({"claim_type": record.get("claim_type"), "label": row.get("label") or row.get("kind"),
                        "fields": ["claim_type", "attachments"]})
    return out


@rule("AC-02", title="核銷類別未設定應附單據",
      basis="機關核定各類核銷應附單據",
      message="核銷類別「{claim_type}」沒有設定應附單據，無法比對，轉人工")
def unknown_type(record, tables):
    if not tables.find("required_attachments", claim_type=record.get("claim_type")):
        return {"claim_type": record.get("claim_type"), "level": "manual", "fields": ["claim_type"]}
    return None
