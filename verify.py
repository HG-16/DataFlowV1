"""Row-level verification, run before anything is imported (file upload, connected database, manual entry).

Each row gets issues at three levels:
  error    the row is blocked and never imported (bad data that would corrupt the store)
  warning  imported, but flagged (usually a fraud signal the vendor check will also catch)
  info     imported; just tells the officer what will happen
"""
from __future__ import annotations

import datetime as dt

import engine
import store

GST_STATUSES = {"active", "cancelled", "canceled", "suspended", "inactive", "provisional"}
LABELS = {"claimed_headcount": "Headcount", "claimed_year_founded": "Year founded",
          "claimed_turnover_inr": "Turnover", "declared_pending_cases": "Pending cases",
          "gstr3b_filed_last_12": "GSTR-3B filed", "tender_value_inr": "Tender value"}


def _date_issue(raw, value, label: str, issues: list):
    if raw in (None, ""):
        return
    try:
        d = dt.date.fromisoformat(value or "")
    except ValueError:
        issues.append(("error", f"{label} '{raw}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)"))
        return
    if d > dt.date.today():
        issues.append(("error", f"{label} {d} is in the future"))
    elif d.year < 2017:
        issues.append(("warning", f"{label} {d} is before GST began (1 Jul 2017): likely migrated or wrong"))


def _numbers(raw: dict, issues: list):
    year = dt.date.today().year
    for f, label in LABELS.items():
        if raw.get(f) in (None, ""):
            continue
        n = store.norm_num(raw[f])
        if n is None:
            issues.append(("error", f"{label} '{raw[f]}' is not a number"))
        elif n < 0:
            issues.append(("error", f"{label} cannot be negative ({n})"))
        elif f == "claimed_year_founded" and not 1800 <= n <= year:
            issues.append(("error", f"Year founded {n} is not a real year"))
        elif f == "gstr3b_filed_last_12" and n > 12:
            issues.append(("error", f"GSTR-3B filed {n} is more than 12 months"))


def verify_vendor(db, row: dict):
    raw = store.pick(row, store.VENDOR_ALIASES)
    rec = store.normalise_vendor(row)
    issues: list[tuple[str, str]] = []
    if rec is None:
        return None, [("error", "No vendor name and no GSTIN: nothing to identify the bidder by")]
    _numbers(raw, issues)
    _date_issue(raw.get("gst_registration_date"), rec.get("gst_registration_date"), "GST registration date", issues)
    gs = rec.get("gstin")
    if gs:
        d = engine.decode(gs)
        if not d["valid"]:
            issues.append(("warning", f"GSTIN {gs} is invalid ({'; '.join(d['problems'])}): this bid will be REJECTED when checked"))
        else:
            if rec.get("pan") and rec["pan"] != d["pan"]:
                issues.append(("warning", f"Declared PAN {rec['pan']} differs from the PAN inside the GSTIN ({d['pan']}): knockout at check"))
            if rec.get("state") and rec["state"].strip().lower() != str(d.get("state", "")).lower():
                issues.append(("warning", f"Declared state '{rec['state']}' but GSTIN is registered in {d.get('state')}"))
            reg = store.registry(db, gs)
            names = [n for n in ((reg or {}).get("legal_name"), (reg or {}).get("trade_name")) if n]
            if names and rec.get("vendor_name") and max(engine.g.name_sim(rec["vendor_name"], n) for n in names) < 85:
                issues.append(("warning", f"Name differs from GST registry ('{names[0]}')"))
            if reg and str(reg.get("status") or "").lower() in ("cancelled", "canceled", "suspended", "inactive"):
                issues.append(("warning", f"GST registration is {str(reg['status']).upper()} in the registry"))
    else:
        issues.append(("warning", "No GSTIN: GST checks will be skipped"))
    if not rec.get("vendor_name"):
        issues.append(("info", "No name given: it will be filled from the GST registry"))
    if not rec.get("domain"):
        issues.append(("info", "No website: identity and capacity checks need one"))
    return rec, issues


def verify_registry(db, row: dict):
    raw = store.pick(row, store.REGISTRY_ALIASES)
    rec = store.normalise_registry(row)
    issues: list[tuple[str, str]] = []
    if rec is None:
        return None, [("error", "No GSTIN in this row")]
    d = engine.decode(rec["gstin"])
    if not d["valid"]:
        issues.append(("error", f"GSTIN {rec['gstin']} is invalid: {'; '.join(d['problems'])}"))
    _numbers(raw, issues)
    _date_issue(raw.get("registration_date"), rec.get("registration_date"), "Registration date", issues)
    st = rec.get("status")
    if st and st not in GST_STATUSES:
        issues.append(("warning", f"Unknown GST status '{raw.get('status')}' (expected Active / Cancelled / Suspended)"))
    if not rec.get("legal_name") and not rec.get("trade_name"):
        issues.append(("warning", "No legal or trade name: bidder-name cross-checks can't run"))
    old = store.registry(db, rec["gstin"]) if d["valid"] else None
    if old and st and (old.get("status") or "") != st:
        issues.append(("info", f"Status changes from {(old.get('status') or 'unknown').upper()} to {st.upper()}"))
    if st in ("cancelled", "canceled", "suspended", "inactive"):
        issues.append(("info", f"Registration is {st.upper()}: bids under this GSTIN will be rejected"))
    return rec, issues


def verify_rows(db, kind: str, rows: list, detail_limit: int = 2000) -> dict:
    fn = verify_vendor if kind == "vendors" else verify_registry
    counts = {"ok": 0, "warning": 0, "error": 0}
    actions = {"insert": 0, "update": 0, "skip": 0}
    details, records, seen = [], [], {}
    for i, row in enumerate(rows, 1):
        if isinstance(row, dict):
            rec, issues = fn(db, row)
        else:
            rec, issues = None, [("error", "Row is not a record")]
        if rec is not None:
            key = (rec.get("gstin"), engine.g.norm_name(rec.get("vendor_name") or ""),
                   str((rec.get("tender") or {}).get("id") or "")) if kind == "vendors" else rec["gstin"]
            if key in seen:
                issues.append(("warning", f"Duplicate of row {seen[key]} in this file: values will be merged"))
            else:
                seen[key] = i
        levels = {lvl for lvl, _ in issues}
        status = "error" if "error" in levels else "warning" if "warning" in levels else "ok"
        if status == "error":
            action = "skip"
        elif kind == "vendors":
            exists = store.find_vendor(db, rec) is not None
            action = "update" if exists or any("Duplicate" in t for _, t in issues) else "insert"
        else:
            exists = store.registry(db, rec["gstin"]) is not None
            action = "update" if exists or any("Duplicate" in t for _, t in issues) else "insert"
        counts[status] += 1
        actions[action] += 1
        records.append((rec, status))
        if len(details) < detail_limit:
            label = (rec or {}).get("vendor_name") or (rec or {}).get("legal_name") or (rec or {}).get("trade_name") or ""
            details.append({"row": i, "status": status, "action": action, "gstin": (rec or {}).get("gstin"),
                            "name": label, "issues": [{"level": lvl, "text": t} for lvl, t in issues]})
    return {"kind": kind, "rows": len(rows), "counts": counts, "actions": actions, "details": details,
            "details_truncated": len(rows) > detail_limit, "_records": records}
