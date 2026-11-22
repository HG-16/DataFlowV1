"""GSTIN lookup + vendor checks for the government dashboard. Reuses gem_checker.py for all scoring.

Automatic GSTIN lookup tries, in order:
  1. the GST registry the department uploaded (dashboard "Upload data" -> GST registry)
  2. a GSP / GST API, if GSP_API_URL is set (any provider that returns the standard GSTN
     taxpayer JSON: lgnm, tradeNam, sts, rgdt, ctb, pradr)
  3. a public web search through Crustdata, if CRUSTDATA_API_KEY is set
Results from 2 and 3 are saved into the registry so later lookups are free.

Check modes (picked per vendor):
  live      CRUSTDATA_API_KEY set: all six categories, live API
  replay    no key, but fixtures/*.json has recorded responses for this vendor's domain
  gst_only  no key: GSTIN checks, registry cross-checks and declared data only (low coverage, so HOLD unless a knockout)
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import sys
import types
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# rapidfuzz is optional here: fall back to difflib so the dashboard runs with the standard library only.
try:
    import rapidfuzz  # noqa: F401
except ImportError:
    def _ratio(a, b):
        return 100 * difflib.SequenceMatcher(None, a, b).ratio()

    def _partial(a, b):
        short, long_ = sorted((a, b), key=len)
        if not short:
            return 0
        return max(_ratio(short, long_[i:i + len(short)]) for i in range(len(long_) - len(short) + 1))

    def _token_sort(a, b):
        return _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))

    _fuzz = types.SimpleNamespace(ratio=_ratio, partial_ratio=_partial, token_sort_ratio=_token_sort)
    sys.modules["rapidfuzz"] = types.SimpleNamespace(fuzz=_fuzz)
    sys.modules["rapidfuzz.fuzz"] = _fuzz

import gem_checker as g  # noqa: E402

import store  # noqa: E402

CACHE_DB = str(Path(__file__).resolve().parent / "crust_cache.db")
FIXTURES = sorted((ROOT / "fixtures").glob("*.json"))
LOOKUP_TTL_HOURS = 24


# --------------------------------------------------------------------------- GSTIN decode
def decode(gstin: str) -> dict:
    gs = re.sub(r"\s", "", gstin or "").upper()
    out = {"gstin": gs, "length_ok": len(gs) == 15, "format_ok": bool(g.GSTIN_RE.match(gs))}
    out["checksum_ok"] = out["format_ok"] and g.gstin_checksum_ok(gs)
    out["valid"] = out["checksum_ok"]
    if len(gs) >= 12:
        out["state_code"] = gs[:2]
        out["state"] = g.GST_STATES.get(gs[:2], "Unknown state code")
        out["pan"] = gs[2:12]
        out["entity_code"] = gs[5]
        out["entity_type"] = g.PAN_ENTITY.get(gs[5], "unknown")
    if len(gs) == 15:
        out["registration_no"] = gs[12]
        out["check_digit"] = gs[14]
        if out["format_ok"] and not out["checksum_ok"]:
            out["expected_check_digit"] = _expected_digit(gs)
    problems = []
    if not out["length_ok"]:
        problems.append(f"GSTIN must be 15 characters (got {len(gs)})")
    elif not out["format_ok"]:
        problems.append("Pattern must be 2 digits + PAN (10) + entity no. + 'Z' + check digit")
    elif not out["checksum_ok"]:
        problems.append(f"Check digit is wrong (expected {out.get('expected_check_digit')}): typo or fabricated GSTIN")
    if out.get("state") == "Unknown state code":
        problems.append(f"State code {gs[:2]} does not exist")
    out["problems"] = problems
    return out


def _expected_digit(gs: str) -> str:
    total = 0
    for i, ch in enumerate(gs[:14]):
        p = g._GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return g._GST_CHARS[(36 - total % 36) % 36]


# --------------------------------------------------------------------------- data sources
def sources_status() -> dict:
    return {
        "registry_upload": True,
        "gsp_api": bool(os.environ.get("GSP_API_URL")),
        "crustdata": bool(os.environ.get("CRUSTDATA_API_KEY")),
        "replay_fixtures": [f.name for f in FIXTURES],
    }


def _fresh(rec: dict) -> bool:
    try:
        age = dt.datetime.now() - dt.datetime.fromisoformat(rec.get("updated") or "")
        return age.total_seconds() < LOOKUP_TTL_HOURS * 3600
    except ValueError:
        return False


def gsp_lookup(gstin: str) -> dict | None:
    """Generic GSP / GST API call. GSP_API_URL may contain {gstin}; otherwise the GSTIN is appended."""
    url = os.environ.get("GSP_API_URL")
    if not url:
        return None
    url = url.replace("{gstin}", gstin) if "{gstin}" in url else url.rstrip("/") + "/" + gstin
    hdrs = {"accept": "application/json"}
    key = os.environ.get("GSP_API_KEY")
    if key:
        header = os.environ.get("GSP_AUTH_HEADER", "Authorization")
        hdrs[header] = (f"Bearer {key}" if header.lower() == "authorization" else key)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    rec = {"gstin": gstin, "raw": data}
    for field, keys in {"legal_name": ("lgnm", "legal_name", "legalName"),
                        "trade_name": ("tradeNam", "trade_name", "tradeName"),
                        "status": ("sts", "status", "gstin_status"),
                        "registration_date": ("rgdt", "registration_date", "registrationDate"),
                        "constitution": ("ctb", "constitution"),
                        "address": ("pradr",)}.items():
        v = g.dig(data, *keys)
        if v not in (None, ""):
            rec[field] = v
    norm = store.normalise_registry(rec) or {"gstin": gstin}
    norm["raw"] = data
    return norm if len(norm) > 2 else None


def web_lookup(gstin: str) -> dict | None:
    key = os.environ.get("CRUSTDATA_API_KEY")
    if not key:
        return None
    c = g.Crust(key, max_credits=3, max_seconds=60, db=CACHE_DB)
    rep = g.Report(gstin)
    for r in g.web_search(c, f'"{gstin}" GST status', rep)[:8]:
        text = f"{r.get('title', '')} {g.snippet_of(r)}"
        if gstin not in text.upper():
            continue
        m = re.search(r"\b(active|cancelled|canceled|suspended|inactive)\b", text.lower())
        name = re.split(r"\s[-|–]\s", str(r.get("title", "")))[0].strip()
        rec = {"gstin": gstin, "raw": {"url": r.get("url"), "snippet": g.snippet_of(r), "title": r.get("title")}}
        if m:
            rec["status"] = "cancelled" if m.group(1) == "canceled" else m.group(1)
        if name and gstin not in name.upper():
            rec["trade_name"] = name
        return rec
    return None


def lookup(db, gstin: str, refresh: bool = False) -> dict:
    """Everything the dashboard knows about a GSTIN, fetching from APIs when needed."""
    info = decode(gstin)
    gs = info["gstin"]
    rec = store.registry(db, gs)
    attempts = []
    if info["valid"] and (refresh or not rec or (rec.get("source") != "upload" and not _fresh(rec))):
        for name, fn in (("gsp_api", gsp_lookup), ("web_search", web_lookup)):
            try:
                found = fn(gs)
            except Exception as e:  # noqa: BLE001
                attempts.append({"source": name, "ok": False, "error": str(e)[:200]})
                continue
            if found is None:
                attempts.append({"source": name, "ok": False,
                                 "error": "not configured" if not sources_status()[
                                     "gsp_api" if name == "gsp_api" else "crustdata"] else "no record found"})
                continue
            found.setdefault("pan", gs[2:12])
            found.setdefault("state", info.get("state"))
            store.upsert_registry(db, found, name)
            db.commit()
            rec = store.registry(db, gs)
            attempts.append({"source": name, "ok": True})
            break
    vendors = [store.vendor_row(r) for r in db.execute("select * from vendors where gstin=?", (gs,))]
    for v in vendors:
        v["_last_check"] = _check_summary(store.latest_check(db, v["_id"]))
    same_pan = []
    if info.get("pan"):
        for r in db.execute("select * from vendors where (pan=? or substr(gstin,3,10)=?) and coalesce(gstin,'')<>?",
                            (info["pan"], info["pan"], gs)):
            same_pan.append({"id": r["id"], "vendor_name": r["vendor_name"], "gstin": r["gstin"], "state": r["state"]})
        for r in db.execute("select gstin, legal_name, status from gst_registry where substr(gstin,3,10)=? and gstin<>?",
                            (info["pan"], gs)):
            if not any(x["gstin"] == r["gstin"] for x in same_pan):
                same_pan.append({"gstin": r["gstin"], "vendor_name": r["legal_name"], "status": r["status"],
                                 "registry_only": True})
    if rec and rec.get("raw"):
        try:
            rec["raw"] = json.loads(rec["raw"])
        except (TypeError, ValueError):
            pass
    return {"decode": info, "registry": rec, "lookup_attempts": attempts, "vendors": vendors,
            "same_pan": same_pan, "alerts": _alerts(info, rec, vendors, same_pan)}


def _alerts(info, rec, vendors, same_pan) -> list[dict]:
    a = [{"level": "critical", "text": p} for p in info["problems"]]
    if rec:
        st = (rec.get("status") or "").lower()
        if st in ("cancelled", "canceled", "suspended", "inactive"):
            a.append({"level": "critical", "text": f"GST registration is {st.upper()} - ineligible to bid"})
        f = rec.get("gstr3b_filed_last_12")
        if f is not None and f < 10:
            a.append({"level": "warning", "text": f"Only {f:.0f}/12 GSTR-3B returns filed in the last year"})
        reg = rec.get("registration_date")
        if reg:
            try:
                yrs = (dt.date.today() - dt.date.fromisoformat(reg)).days / 365.25
                if yrs < 1:
                    a.append({"level": "warning", "text": f"GST registered only {yrs * 12:.0f} months ago"})
            except ValueError:
                pass
        names = [n for n in (rec.get("legal_name"), rec.get("trade_name")) if n]
        for v in vendors:
            if names and max(g.name_sim(v["vendor_name"], n) for n in names) < 85:
                a.append({"level": "critical",
                          "text": f"Bid name '{v['vendor_name']}' does not match GST legal name '{names[0]}'"})
    elif info["valid"]:
        a.append({"level": "info", "text": "No registry record yet - upload the GST registry or configure a GSP API"})
    distinct = {g.norm_name(v["vendor_name"]) for v in vendors}
    if len(distinct) > 1:
        a.append({"level": "critical", "text": f"Same GSTIN used by {len(distinct)} differently named bidders"})
    others = [x for x in same_pan if not x.get("registry_only")]
    if others:
        a.append({"level": "warning",
                  "text": f"PAN {info.get('pan')} also appears on {len(others)} other bidder record(s): possible related parties"})
    return a


# --------------------------------------------------------------------------- checks
def _replay_fixture_for(bid: dict) -> str | None:
    dom = g.domain_root(bid.get("domain", ""))
    if not dom:
        return None
    want = g.Crust._key("/company/identify", {"domains": [dom]})
    for f in FIXTURES:
        try:
            calls = json.loads(f.read_text(encoding="utf-8")).get("calls") or []
        except (OSError, ValueError):
            continue
        if any(g.Crust._key(c["path"], c["body"]) == want for c in calls):
            return str(f)
    return None


def _check_summary(c: dict | None) -> dict | None:
    if not c:
        return None
    return {k: c[k] for k in ("id", "verdict", "score", "coverage", "mode", "knockouts", "created", "reviewed_by")}


def run_check(db, vendor_id: int, refresh_lookup: bool = False) -> dict:
    row = db.execute("select * from vendors where id=?", (vendor_id,)).fetchone()
    if not row:
        raise KeyError(f"vendor {vendor_id} not found")
    bid = {k: v for k, v in json.loads(row["data"]).items() if not k.startswith("_")}
    bid.setdefault("vendor_name", row["vendor_name"])
    bid.setdefault("domain", "")

    reg, look = None, None
    if bid.get("gstin"):
        look = lookup(db, bid["gstin"], refresh=refresh_lookup)
        reg = look["registry"]
        # Registry values are independent evidence: they fill the GST portal fields the bid left blank.
        if reg:
            if not bid.get("gst_status") and reg.get("status"):
                bid["gst_status"] = reg["status"]
            if bid.get("gstr3b_filed_last_12") in (None, "") and reg.get("gstr3b_filed_last_12") is not None:
                bid["gstr3b_filed_last_12"] = reg["gstr3b_filed_last_12"]
            if not bid.get("gst_registration_date") and reg.get("registration_date"):
                bid["gst_registration_date"] = reg["registration_date"]

    key = os.environ.get("CRUSTDATA_API_KEY")
    fixture = None if key else _replay_fixture_for(bid)
    if key and bid.get("domain"):
        mode = "live"
        rep, _ = g.run(bid, g.Crust(key, db=CACHE_DB))
    elif fixture:
        mode = "replay"
        rep, _ = g.run(bid, g.Crust("", db=":memory:", replay=fixture))
    else:
        mode = "gst_only"
        rep = g.Report(bid["vendor_name"])
        g.check_gst(None, bid, rep)
        n = g._num(bid.get("declared_pending_cases"))
        if n is not None:
            rep.add("firm_legal", "declared_cases", g.CONFIRMED if n == 0 else g.CONTRADICTED,
                    1.0 if n == 0 else max(0.2, 1 - 0.2 * n), f"Vendor declared {n:.0f} pending case(s)",
                    "bid declaration")

    _cross_checks(db, bid, reg, rep, vendor_id)
    s = g.score(rep)
    s["needs_signoff"] = s["verdict"] == "REJECT"
    s["mode"] = mode
    findings = [asdict(f) for f in rep.findings]
    cur = db.execute(
        "insert into checks(vendor_id,gstin,vendor_name,verdict,score,coverage,mode,knockouts,result,findings,created)"
        " values(?,?,?,?,?,?,?,?,?,?,?)",
        (vendor_id, bid.get("gstin"), bid["vendor_name"], s["verdict"], s["score"], s["coverage"], mode,
         len(s["knockouts"]), json.dumps(s), json.dumps(findings), store.now()))
    db.commit()
    out = store.check_row(db.execute("select * from checks where id=?", (cur.lastrowid,)).fetchone())
    out["credits_spent"] = rep.credits_spent
    return out


def _cross_checks(db, bid: dict, reg: dict | None, rep, vendor_id: int):
    gs = bid.get("gstin") or ""
    src = f"GST registry ({reg.get('source')})" if reg else ""
    if reg:
        names = [n for n in (reg.get("legal_name"), reg.get("trade_name")) if n]
        if names:
            best = max(names, key=lambda n: g.name_sim(bid["vendor_name"], n))
            sim = g.name_sim(bid["vendor_name"], best)
            ok = sim >= 85
            rep.add("gst", "gst_legal_name", g.CONFIRMED if ok else g.CONTRADICTED, 1.0 if ok else 0.1,
                    f"GST registry name '{best}' {'matches' if ok else 'does NOT match'} bidder "
                    f"'{bid['vendor_name']}' ({sim:.0f}%)", src, passage=best)
    if gs:
        others = db.execute("select id, vendor_name from vendors where gstin=? and id<>?", (gs, vendor_id)).fetchall()
        diff = [o["vendor_name"] for o in others if g.name_sim(o["vendor_name"], bid["vendor_name"]) < 85]
        if diff:
            rep.add("identity", "gstin_reused", g.RED_FLAG, 0.1,
                    f"GSTIN {gs} is also used by: {', '.join(diff[:3])}", "dashboard vendor database")
    pan = gs[2:12] if len(gs) == 15 else (bid.get("pan") or "")
    if pan:
        rel = db.execute("select vendor_name, gstin, data from vendors where (pan=? or substr(gstin,3,10)=?)"
                         " and id<>? and coalesce(gstin,'')<>?", (pan, pan, vendor_id, gs)).fetchall()
        rel = [r for r in rel if g.name_sim(r["vendor_name"], bid["vendor_name"]) < 85]
        if rel:
            tid = (bid.get("tender") or {}).get("id")
            same_tender = [r for r in rel if tid and (json.loads(r["data"]).get("tender") or {}).get("id") == tid]
            rep.add("identity", "shared_pan", g.RED_FLAG, 0.0 if same_tender else 0.4,
                    f"PAN {pan} is shared with {len(rel)} differently named bidder(s): "
                    f"{', '.join(r['vendor_name'] for r in rel[:3])}"
                    + (f" - bidding on the SAME tender {tid} (cover bidding)" if same_tender else ""),
                    "dashboard vendor database", knockout=bool(same_tender))
