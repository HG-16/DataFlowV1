"""GeM Bid Checker - verify an IT vendor's tender claims before the government pays.

Checks (each produces Findings with source + passage + date):
  1. Identity      - domain resolves to the claimed firm; lookalike-domain detection
  2. Capacity      - headcount / founding year vs claims; shell-company signals
  3. GST           - GSTIN format + checksum, PAN/state/entity consistency,
                     public status lookup (active / cancelled / suspended), filing regularity
  4. Firm legal    - pending cases, insolvency (NCLT/IBC), debarment, adverse media
  5. People        - each key person currently works at the vendor (not namesake / ex-employee)
  6. People legal  - cases / fraud / disqualification against each key person

Scores are combined with a weighted formula (see WEIGHTS and score()).
Data: Crustdata API (company, person, job, web search) + bidder-declared fields.

Run:  pip install httpx rapidfuzz
      export CRUSTDATA_API_KEY=...
      python gem_checker.py bids/bid_lookalike.json            # prompts for missing fields
      python gem_checker.py bids/bid_honest.json --no-prompt --yes
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field

import providers as pv

BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
MAX_CREDITS = 40
MAX_SECONDS = 180

# --------------------------------------------------------------------------- weights
# Weighted formula. Each category gets a sub-score in [0, 1] (or None = no evidence).
#   score = 100 * sum(w_i * s_i) / sum(w_i)        with s_i = 0.5 when unknown
#   coverage = sum(w_i for categories with evidence) / sum(w_i)
# Knockout rules (cancelled GST, debarment, lookalike domain, criminal/fraud case
# against firm or key person) force REJECT regardless of score.
WEIGHTS = {
    "identity": 15,      # is this really the firm it claims to be? (domain, CIN, Udyam)
    "capacity": 15,      # can it deliver? (headcount, age, hiring, experience, shell signals)
    "gst": 20,           # GSTIN genuine, active, in the vendor's name, filing regularly (GST in Cloud)
    "financials": 15,    # real revenue / profit / net worth vs claims and tender turnover rule (EODHD)
    "firm_legal": 15,    # pending cases / insolvency / debarment against the firm
    "people": 10,        # named key personnel really work there
    "people_legal": 10,  # cases against key personnel
}
UNKNOWN_PRIOR = 0.5
APPROVE_AT, HOLD_AT, MIN_COVERAGE = 75, 50, 0.6

# --------------------------------------------------------------------------- findings
CONFIRMED, CONTRADICTED, CANT_VERIFY, RED_FLAG = "confirmed", "contradicted", "cant_verify", "red_flag"


@dataclass
class Finding:
    category: str
    check: str
    status: str
    score: float | None          # contribution in [0,1]; None = no evidence
    detail: str
    source: str = ""
    passage: str = ""
    date: str = ""
    knockout: bool = False


@dataclass
class Report:
    vendor: str
    findings: list[Finding] = field(default_factory=list)
    credits_spent: float = 0.0

    def add(self, *a, **k):
        f = Finding(*a, **k)
        self.findings.append(f)
        return f


# --------------------------------------------------------------------------- client
class BudgetExceeded(BaseException):
    """BaseException so generic `except Exception` in checks never swallows a budget stop."""


class Crust:
    """Budgeted, cached Crustdata client. Re-runs hit the SQLite cache and cost nothing."""

    def __init__(self, key: str, max_credits=MAX_CREDITS, max_seconds=MAX_SECONDS, db="cache.db",
                 replay: str | None = None):
        self.offline = replay is not None
        if not self.offline:
            import httpx  # local import so replay/tests run without network deps

            self.http = httpx.Client(timeout=60)
        self.hdrs = {"authorization": f"Bearer {key}", "x-api-version": API_VERSION,
                     "content-type": "application/json"}
        self.max_credits, self.deadline, self.spent = max_credits, time.time() + max_seconds, 0.0
        self.db = sqlite3.connect(db)
        self.db.execute("create table if not exists cache(k text primary key, v text)")
        self.db.execute("create table if not exists ledger(ts real, path text, credits real)")
        self.ext = pv.Http(self.db, offline=self.offline)   # GST in Cloud / EODHD, same cache
        if replay:  # preload recorded real responses under the exact cache keys the checks will ask for
            fx = json.load(open(replay))
            for call in fx.get("calls", []):
                self.db.execute("insert or replace into cache values(?,?)",
                                (self._key(call["path"], call["body"]), json.dumps(call["response"])))
            self.db.commit()
            self.ext.preload(fx.get("http_calls", []))

    @staticmethod
    def _key(path: str, body: dict) -> str:
        return hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()

    def post(self, path: str, body: dict, est_cost: float = 1.0):
        k = self._key(path, body)
        row = self.db.execute("select v from cache where k=?", (k,)).fetchone()
        if row:
            return json.loads(row[0])
        if self.offline:
            raise RuntimeError(f"{path}: not in replay file (offline mode)")
        if time.time() > self.deadline or self.spent + est_cost > self.max_credits:
            raise BudgetExceeded(f"spent {self.spent:.1f} credits")
        for attempt in range(4):
            r = self.http.post(BASE + path, headers=self.hdrs, json=body)
            if r.status_code not in (429, 502, 503):
                break
            time.sleep(2 ** attempt)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        cost = _num(dig(data, "spent_this_call")) or est_cost   # API reports credits.spent_this_call
        self.spent += cost
        self.db.execute("insert into ledger values(?,?,?)", (time.time(), path, cost))
        self.db.execute("insert or replace into cache values(?,?)", (k, json.dumps(data)))
        self.db.commit()
        return data


# --------------------------------------------------------------------------- helpers
def dig(obj, *keys):
    """Depth-first search for the first non-empty value under any of `keys`.
    Response shapes vary by endpoint/version, so we search instead of hard-coding paths."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, "", [], {}):
                return obj[k]
        for v in obj.values():
            r = dig(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = dig(v, *keys)
            if r is not None:
                return r
    return None


def records(obj, *keys):
    """Return the first list of dicts found under any of `keys` (or obj itself if a list)."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    v = dig(obj, *keys)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|technologies|technology|solutions|services|"
               r"infotech|systems|india|co|company|corp|corporation)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s)).strip()


def name_sim(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b) if min(len(a), len(b)) >= 5 else 0)


def domain_root(d: str) -> str:
    d = re.sub(r"^https?://", "", (d or "").lower()).split("/")[0]
    d = re.sub(r"^www\.", "", d)
    return d


def snippet_of(r: dict) -> str:
    return str(r.get("snippet") or r.get("description") or r.get("content") or r.get("text") or "")[:400]


def mentions(subject: str, text: str) -> bool:
    """Strict check that `text` names `subject`: every distinctive word of the subject appears as a
    whole word (fuzzy matching would let 'Ksolves' match 'resolves')."""
    toks = norm_name(subject).split() or re.sub(r"[^a-z0-9 ]", " ", subject.lower()).split()
    words = set(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())
    return bool(toks) and all(t in words for t in toks)


def web_search(c: Crust, query: str, rep: Report, **extra):
    """/web/search/live -> {results: [{title, url, snippet, position, source}], ...}"""
    body = {"query": query, "sources": ["web"], "location": "IN", **extra}
    data = c.post("/web/search/live", body, est_cost=1)
    return [r for r in (data.get("results") or []) if isinstance(r, dict)] if isinstance(data, dict) else []


def company_matches(data) -> list[tuple[float, dict]]:
    """identify/enrich -> [{matched_on, match_type, matches: [{confidence_score, company_data}]}]"""
    out = []
    for block in data if isinstance(data, list) else [data]:
        for m in (block or {}).get("matches") or []:
            if m.get("company_data"):
                out.append((float(m.get("confidence_score") or 0), m["company_data"]))
    return out


# --------------------------------------------------------------------------- 1. identity
def check_identity(c: Crust, bid: dict, rep: Report) -> dict | None:
    """Returns the resolved company_data (crustdata_company_id + basic_info) or None."""
    dom, name = domain_root(bid.get("domain", "")), bid["vendor_name"]
    src = "crustdata:/company/identify"
    company = None
    try:
        ms = company_matches(c.post("/company/identify", {"domains": [dom]}, est_cost=0.2))
        company = ms[0][1] if ms else None
    except Exception as e:  # noqa: BLE001
        rep.add("identity", "domain_lookup", CANT_VERIFY, None, f"identify failed: {e}", src)

    if company:
        bi = company.get("basic_info") or {}
        real_name = bi.get("name") or ""
        owned = {domain_root(d) for d in [bi.get("primary_domain")] + (bi.get("all_domains") or []) if d}
        # Live finding: domain identify is fuzzy - 'ksolves.co' resolves to Ksolves (ksolves.com) at
        # confidence 1.0. So the bid domain must literally be one of the matched firm's domains.
        if owned and dom not in owned:
            from rapidfuzz import fuzz

            real = domain_root(bi.get("primary_domain") or sorted(owned)[0])
            dsim = fuzz.ratio(real.split(".")[0], dom.split(".")[0])
            rep.add("identity", "lookalike_domain", RED_FLAG, 0.0,
                    f"Bid domain {dom} is NOT owned by '{real_name}' (real domain: {real}, similarity {dsim:.0f}%)",
                    bi.get("professional_network_url") or src, passage=", ".join(sorted(owned)),
                    knockout=name_sim(name, real_name) >= 85 or dsim >= 70)
            return company
        sim = name_sim(name, real_name)
        status = CONFIRMED if sim >= 85 else CONTRADICTED
        rep.add("identity", "domain_owner", status, 1.0 if sim >= 85 else 0.0,
                f"{dom} belongs to '{real_name}'" + ("" if sim >= 85 else f", not '{name}'") + f" (match {sim:.0f}%)",
                bi.get("professional_network_url") or src, passage=real_name)
    else:
        rep.add("identity", "domain_owner", CANT_VERIFY, None, f"No company record for {dom}", src)

    # Lookalike check: resolve the *name*. If a confident match with the same name owns a different
    # but similar-looking domain, the bid's domain is likely an impersonation.
    try:
        from rapidfuzz import fuzz

        for conf, m in company_matches(c.post("/company/identify", {"names": [name]}, est_cost=0.2)):
            bi = m.get("basic_info") or {}
            doms = {domain_root(d) for d in ([bi.get("primary_domain")] + (bi.get("all_domains") or [])) if d}
            if conf < 0.75 or not doms or dom in doms or name_sim(name, bi.get("name") or "") < 85:
                continue
            mdom = domain_root(bi.get("primary_domain") or sorted(doms)[0])
            dsim = fuzz.ratio(mdom.split(".")[0], dom.split(".")[0])
            if dsim >= 70:
                rep.add("identity", "lookalike_domain", RED_FLAG, 0.0,
                        f"'{bi.get('name')}' is registered on {mdom}; bid uses lookalike {dom} (similarity {dsim:.0f}%)",
                        src, passage=mdom, knockout=True)
                return company or m      # use the real firm's record for the remaining checks
    except Exception:  # noqa: BLE001
        pass
    return company


# --------------------------------------------------------------------------- 2. capacity
def check_capacity(c: Crust, bid: dict, rep: Report, company: dict | None) -> dict:
    """Returns the enriched company_data (incl. people.cxos/founders/decision_makers) for later checks."""
    src = "crustdata:/company/enrich"
    body = {"fields": ["basic_info", "headcount", "hiring", "people"]}
    if company and company.get("crustdata_company_id"):
        body["crustdata_company_ids"] = [company["crustdata_company_id"]]
    else:
        body["domains"] = [domain_root(bid.get("domain", ""))]
    try:
        ms = company_matches(c.post("/company/enrich", body, est_cost=4))
    except Exception as e:  # noqa: BLE001
        rep.add("capacity", "enrich", CANT_VERIFY, None, f"enrich failed: {e}", src)
        return {}
    if not ms:
        rep.add("capacity", "headcount", CANT_VERIFY, None, "No company profile found", src)
        return {}
    cd = ms[0][1]
    bi, hcd = cd.get("basic_info") or {}, cd.get("headcount") or {}
    hc = _num(hcd.get("total"))
    founded = _num(bi.get("year_founded"))
    asof = str(dt.date.today())

    claimed_hc = _num(bid.get("claimed_headcount"))
    if hc is not None and claimed_hc:
        ratio = hc / claimed_hc
        ok = ratio >= 0.5
        rep.add("capacity", "headcount", CONFIRMED if ok else CONTRADICTED, min(1.0, ratio),
                f"Claimed {claimed_hc:.0f}, observed {hc:.0f}" + ("" if ok else f" ({ratio:.0%}) - inflated"),
                src, passage=f"headcount.total={hc:.0f}", date=asof)
    elif hc is not None:
        rep.add("capacity", "headcount", CONFIRMED, 1.0 if hc >= 50 else 0.6, f"Observed headcount {hc:.0f}", src,
                passage=f"headcount.total={hc:.0f}", date=asof)
    else:
        rep.add("capacity", "headcount", CANT_VERIFY, None, "Headcount not available", src)

    claimed_yr = _num(bid.get("claimed_year_founded"))
    if founded and claimed_yr:
        ok = abs(founded - claimed_yr) <= 1
        rep.add("capacity", "founded", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.0,
                f"Founded {founded:.0f}" + (f" (claimed {claimed_yr:.0f})" if ok else f", bid claims {claimed_yr:.0f}"),
                src, passage=f"basic_info.year_founded={founded:.0f}")

    # Shell-company signal: tiny team vs big tender / big claimed turnover.
    tv = _num((bid.get("tender") or {}).get("value_inr"))
    turnover = _num(bid.get("claimed_turnover_inr"))
    if hc is not None:
        if tv and tv / max(hc, 1) > 2.5e7:  # > ₹2.5 cr of contract per observed employee
            rep.add("capacity", "shell_signal_tender", RED_FLAG, 0.1,
                    f"Tender ₹{tv/1e7:.1f} cr vs {hc:.0f} staff = ₹{tv/max(hc,1)/1e7:.1f} cr per head", src,
                    passage=f"headcount.total={hc:.0f}")
        if turnover and turnover / max(hc, 1) > 1.5e7:  # IT services ~₹10-40 lakh revenue / employee
            rep.add("capacity", "shell_signal_turnover", RED_FLAG, 0.1,
                    f"Claimed turnover ₹{turnover/1e7:.1f} cr with {hc:.0f} staff "
                    f"(₹{turnover/max(hc,1)/1e5:.0f} lakh/head; IT norm is ₹10–40 lakh)", src)

    # Hiring signal (count-only job search is free).
    cid = cd.get("crustdata_company_id") or (company or {}).get("crustdata_company_id")
    if cid:
        try:
            jobs = c.post("/job/search", {"filters": {"field": "company.basic_info.crustdata_company_id",
                                                      "type": "=", "value": cid},
                                          "aggregations": [{"type": "count"}], "limit": 0}, est_cost=0)
            total = _num(jobs.get("total_count")) if isinstance(jobs, dict) else None
            if total:
                rep.add("capacity", "hiring", CONFIRMED, 1.0, f"{total:.0f} job listings on record", "crustdata:/job/search")
            elif total == 0 and tv and tv >= 5e7:
                rep.add("capacity", "hiring", CONTRADICTED, 0.3,
                        f"No job listings despite bidding a ₹{tv/1e7:.1f} cr build", "crustdata:/job/search")
        except Exception:  # noqa: BLE001
            pass
    return cd


# --------------------------------------------------------------------------- 3. GST
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}
PAN_ENTITY = {"C": "company", "F": "firm/llp", "P": "individual/proprietor", "H": "huf", "A": "aop",
              "T": "trust", "B": "boi", "L": "local authority", "J": "artificial juridical", "G": "government"}
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def gstin_checksum_ok(g: str) -> bool:
    total = 0
    for i, ch in enumerate(g[:14]):
        p = _GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return _GST_CHARS[(36 - total % 36) % 36] == g[14]


def check_gst(c: Crust | None, bid: dict, rep: Report):
    g = (bid.get("gstin") or "").strip().upper()
    if not g:
        rep.add("gst", "gstin_present", CANT_VERIFY, None, "No GSTIN supplied - mandatory for GeM sellers")
        return
    if not GSTIN_RE.match(g) or not gstin_checksum_ok(g):
        rep.add("gst", "gstin_format", RED_FLAG, 0.0, f"{g} is not a valid GSTIN (format/checksum fails)",
                "GSTIN checksum algorithm", knockout=True)
        return
    rep.add("gst", "gstin_format", CONFIRMED, 1.0, f"{g} passes format + checksum", "GSTIN checksum algorithm")

    pan, state_code, entity = g[2:12], g[:2], PAN_ENTITY.get(g[5], "unknown")
    if bid.get("pan"):
        ok = bid["pan"].strip().upper() == pan
        rep.add("gst", "pan_match", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.0,
                f"PAN in GSTIN {pan} {'matches' if ok else 'does NOT match'} declared PAN {bid['pan']}",
                "GSTIN structure", knockout=not ok)
    if bid.get("state"):
        st = GST_STATES.get(state_code, "?")
        ok = st.lower() == bid["state"].strip().lower()
        rep.add("gst", "state_match", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.4,
                f"GSTIN registered in {st} (code {state_code}); bid says {bid['state']}", "GSTIN structure")
    declared = (bid.get("entity_type") or "").lower()
    if declared:
        expect = {"private limited": "C", "public limited": "C", "company": "C", "llp": "F",
                  "partnership": "F", "proprietorship": "P"}.get(declared)
        if expect:
            ok = g[5] == expect
            rep.add("gst", "entity_type", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.2,
                    f"PAN type '{entity}' vs declared '{declared}'", "PAN 4th character")

    # ---- Authoritative record from GST in Cloud (status, legal name, constitution, returns)
    rec = None
    if c is not None and (pv.gst_configured() or getattr(c, "offline", False)):
        try:
            rec = pv.gst_lookup(c.ext, g)
        except Exception as e:  # noqa: BLE001
            rep.add("gst", "gst_api", CANT_VERIFY, None, f"GST in Cloud lookup failed: {e}", "GST in Cloud")
    elif c is not None:
        rep.add("gst", "gst_api", CANT_VERIFY, None,
                "GST in Cloud not configured (set GSTINCLOUD_API_KEY + GSTINCLOUD_URL) - using fallbacks", "GST in Cloud")

    src_api = "GST in Cloud (GSTN taxpayer record)"
    status, src, passage = "", "declared / services.gst.gov.in", ""
    if rec and rec.get("status"):
        status, src = rec["status"].lower(), src_api
        passage = f"sts={rec['status']}; lgnm={rec['legal_name']}; tradeNam={rec['trade_name']}; rgdt={rec['registration_date']}"
        # Legal / trade name must be the vendor - otherwise someone else's GSTIN is being used.
        best = max(name_sim(bid["vendor_name"], rec["legal_name"]), name_sim(bid["vendor_name"], rec["trade_name"]))
        if rec["legal_name"] or rec["trade_name"]:
            ok = best >= 85
            rep.add("gst", "gst_legal_name", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.0,
                    f"GSTIN registered to '{rec['legal_name'] or rec['trade_name']}'"
                    + ("" if ok else f" - NOT '{bid['vendor_name']}'") + f" (match {best:.0f}%)",
                    src_api, passage, knockout=best < 60)
        if rec["constitution"] and bid.get("entity_type"):
            ok = any(w in rec["constitution"].lower() for w in bid["entity_type"].lower().split())
            rep.add("gst", "gst_constitution", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.3,
                    f"Constitution on GST record: '{rec['constitution']}' (declared '{bid['entity_type']}')", src_api)
    else:
        status = (bid.get("gst_status") or "").lower()
        if not status and c is not None:     # last resort: public web pages that mirror the GST record
            try:
                for r in web_search(c, f'"{g}" GST status', rep)[:6]:
                    text = (str(r.get("title", "")) + " " + snippet_of(r))
                    if g not in text.upper():
                        continue
                    m = re.search(r"\b(active|cancelled|canceled|suspended|inactive)\b", text.lower())
                    if m:
                        status, src, passage = m.group(1), r.get("url", "web"), snippet_of(r)
                        if bid["vendor_name"] and not mentions(bid["vendor_name"], text):
                            rep.add("gst", "gst_legal_name", CONTRADICTED, 0.2,
                                    "Public GSTIN record does not mention the vendor's name", src, passage)
                        break
            except Exception as e:  # noqa: BLE001
                rep.add("gst", "gst_status", CANT_VERIFY, None, f"status lookup failed: {e}")

    if status in ("cancelled", "canceled", "suspended", "inactive", "cancelled suo-moto"):
        extra = f" since {rec['cancellation_date']}" if rec and rec.get("cancellation_date") else ""
        rep.add("gst", "gst_status", RED_FLAG, 0.0, f"GSTIN status: {status.upper()}{extra}", src, passage, knockout=True)
    elif status == "active":
        rep.add("gst", "gst_status", CONFIRMED, 1.0, "GSTIN status: ACTIVE", src, passage)
    elif status:
        rep.add("gst", "gst_status", CONTRADICTED, 0.4, f"GSTIN status: {status.upper()}", src, passage)
    elif not any(f.check == "gst_status" for f in rep.findings):
        rep.add("gst", "gst_status", CANT_VERIFY, None,
                f"Status unknown - check https://services.gst.gov.in/services/searchtp for {g}")

    # ---- Filing regularity: GSTR-3B months filed out of the last 12 (API first, else declared)
    filed, fsrc = None, "declared / GST portal"
    if rec and rec.get("filings"):
        filed, fsrc = pv.gstr3b_filed_last_12(rec["filings"]), src_api
    if filed is None:
        filed = _num(bid.get("gstr3b_filed_last_12"))
    if filed is not None:
        s = max(0.0, min(1.0, filed / 12))
        rep.add("gst", "filing_regularity", CONFIRMED if s >= 0.9 else CONTRADICTED, s,
                f"GSTR-3B filed {filed:.0f}/12 months" + ("" if s >= 0.9 else " - irregular filer"), fsrc)

    # ---- Registration age (API first, else declared)
    reg_d = pv.parse_date(rec.get("registration_date")) if rec else None
    reg_src = src_api if reg_d else "declared / GST portal"
    reg_d = reg_d or pv.parse_date(bid.get("gst_registration_date"))
    if reg_d:
        yrs = (dt.date.today() - reg_d).days / 365.25
        s = 1.0 if yrs >= 3 else 0.6 if yrs >= 1 else 0.2
        rep.add("gst", "gst_age", CONFIRMED if yrs >= 1 else CONTRADICTED, s,
                f"GST registered {yrs:.1f} years ago ({reg_d.isoformat()})", reg_src, date=reg_d.isoformat())


# --------------------------------------------------------------------------- 3b. financials
USD_INR = float(os.environ.get("USD_INR", "84"))   # only for Crustdata's USD revenue *estimates*


def _cr(x):
    return f"₹{x/1e7:,.1f} cr"


def check_financials(c: Crust, bid: dict, rep: Report, company: dict | None):
    """Claimed turnover / net worth vs real financials.
    1) EODHD audited financials for listed firms (strong evidence)
    2) Crustdata revenue estimate band (weak evidence: wide, model-based - never a knockout)
    3) Tender eligibility: average turnover of last 3 FYs >= minimum the bid document requires."""
    claimed = [_num(x) for x in (bid.get("turnover_last_3fy_inr") or []) if _num(x)]
    claimed_latest = claimed[0] if claimed else _num(bid.get("claimed_turnover_inr"))
    claimed_avg = sum(claimed) / len(claimed) if claimed else claimed_latest
    tender = bid.get("tender") or {}
    min_avg = _num(tender.get("min_avg_turnover_inr"))
    cid = (company or {}).get("crustdata_company_id")

    # ---- Crustdata: revenue band + stock symbol (to find the listed entity)
    band, symbols = None, []
    if cid:
        try:
            ms = company_matches(c.post("/company/enrich", {"crustdata_company_ids": [cid], "fields": ["revenue"]},
                                        est_cost=2))
            rv = (ms[0][1].get("revenue") or {}) if ms else {}
            est = rv.get("estimated") or {}
            if est.get("lower_bound_usd") or est.get("upper_bound_usd"):
                band = (_num(est.get("lower_bound_usd")) or 0, _num(est.get("upper_bound_usd")) or 0)
            symbols = ((rv.get("public_markets") or {}).get("stock_symbols")) or []
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001
            pass

    # ---- EODHD: audited yearly financials for listed companies
    fin, src = None, "EODHD fundamentals"
    ticker = (bid.get("stock_ticker") or "").strip().upper()
    if pv.eodhd_configured() or getattr(c, "offline", False):
        try:
            if not ticker and symbols:
                ticker = f"{symbols[0]}.NSE"
            if not ticker:
                hits = [h for h in pv.eodhd_find_ticker(c.ext, bid["vendor_name"])
                        if h.get("Exchange") in ("NSE", "BSE") and name_sim(bid["vendor_name"], h.get("Name", "")) >= 85]
                ticker = f"{hits[0]['Code']}.{hits[0]['Exchange']}" if hits else ""
            if ticker:
                fin = pv.eodhd_fundamentals(c.ext, ticker)
                src = f"EODHD fundamentals {ticker}"
                if fin["name"] and name_sim(bid["vendor_name"], fin["name"]) < 85:
                    rep.add("financials", "listed_entity", CANT_VERIFY, None,
                            f"{ticker} is '{fin['name']}', not the vendor - financials ignored", src)
                    fin = None
        except Exception as e:  # noqa: BLE001
            rep.add("financials", "eodhd", CANT_VERIFY, None, f"EODHD lookup failed: {e}", "EODHD")
    else:
        rep.add("financials", "eodhd", CANT_VERIFY, None, "EODHD not configured (set EODHD_API_KEY)", "EODHD")

    years = [y for y in (fin or {}).get("years", []) if y.get("revenue")]
    if years:
        actual = years[0]["revenue"]
        avg3 = sum(y["revenue"] for y in years[:3]) / len(years[:3])
        passage = "; ".join(f"FY{y['date'][:4]} revenue {_cr(y['revenue'])}, net income {_cr(y['net_income'] or 0)}"
                            for y in years[:3])
        if claimed_latest:
            r = claimed_latest / actual
            if r <= 1.25:
                rep.add("financials", "turnover_claim", CONFIRMED, 1.0,
                        f"Claimed turnover {_cr(claimed_latest)} vs audited {_cr(actual)} (FY{years[0]['date'][:4]})",
                        src, passage, years[0]["date"])
            else:
                rep.add("financials", "turnover_claim", RED_FLAG if r > 2 else CONTRADICTED, 0.1 if r > 2 else 0.4,
                        f"Turnover INFLATED: claimed {_cr(claimed_latest)} vs audited {_cr(actual)} ({r:.1f}x)",
                        src, passage, years[0]["date"])
        else:
            rep.add("financials", "turnover", CONFIRMED, 1.0, f"Audited revenue {_cr(actual)} (FY{years[0]['date'][:4]})",
                    src, passage, years[0]["date"])
        ni = [y.get("net_income") for y in years[:2] if y.get("net_income") is not None]
        if ni:
            if all(x < 0 for x in ni) and len(ni) == 2:
                rep.add("financials", "profitability", RED_FLAG, 0.3, "Loss-making in each of the last 2 years", src, passage)
            elif ni[0] < 0:
                rep.add("financials", "profitability", CONTRADICTED, 0.5, f"Net loss {_cr(ni[0])} in latest year", src)
            else:
                rep.add("financials", "profitability", CONFIRMED, 1.0, f"Profitable: net income {_cr(ni[0])}", src)
        eq = years[0].get("equity")
        if eq is not None:
            rep.add("financials", "net_worth", CONFIRMED if eq > 0 else RED_FLAG, 1.0 if eq > 0 else 0.0,
                    f"Net worth {_cr(eq)}" + ("" if eq > 0 else " - NEGATIVE (fails GeM financial standing)"),
                    src, knockout=eq <= 0)
        if min_avg:
            ok = avg3 >= min_avg
            rep.add("financials", "tender_turnover_rule", CONFIRMED if ok else RED_FLAG, 1.0 if ok else 0.0,
                    f"Avg turnover (last {len(years[:3])} FY) {_cr(avg3)} vs tender minimum {_cr(min_avg)}"
                    + ("" if ok else " - NOT ELIGIBLE"), src, passage, knockout=not ok)
        tv = _num(tender.get("value_inr"))
        if tv and tv > 0.5 * actual:
            rep.add("financials", "tender_vs_turnover", CONTRADICTED, 0.4,
                    f"Tender {_cr(tv)} is {tv/actual:.0%} of annual revenue - delivery risk", src)
        return

    # ---- Unlisted (or no EODHD): weak checks only
    if band and claimed_latest:
        lo, hi = band[0] * USD_INR, band[1] * USD_INR
        if claimed_latest > 3 * hi:
            rep.add("financials", "turnover_estimate", CONTRADICTED, 0.3,
                    f"Claimed {_cr(claimed_latest)} is far above Crustdata's estimate ({_cr(lo)}–{_cr(hi)})",
                    "crustdata:/company/enrich revenue (estimate)")
        else:
            rep.add("financials", "turnover_estimate", CONFIRMED, 0.7,
                    f"Claimed {_cr(claimed_latest)} not inconsistent with Crustdata estimate ({_cr(lo)}–{_cr(hi)}; wide model band)",
                    "crustdata:/company/enrich revenue (estimate)")
    if min_avg and claimed_avg:
        ok = claimed_avg >= min_avg
        rep.add("financials", "tender_turnover_rule", CANT_VERIFY if ok else CONTRADICTED, None if ok else 0.2,
                f"Declared avg turnover {_cr(claimed_avg)} vs tender minimum {_cr(min_avg)}"
                + (" - unverified (no audited source; ask for CA certificate)" if ok else " - below minimum"),
                "bid declaration")
    nw = _num(bid.get("net_worth_inr"))
    if nw is not None and nw <= 0:
        rep.add("financials", "net_worth", RED_FLAG, 0.0, f"Declared net worth {_cr(nw)} - negative", "bid declaration",
                knockout=True)
    if not any(f.category == "financials" and f.score is not None for f in rep.findings):
        rep.add("financials", "audited_financials", CANT_VERIFY, None,
                "No audited financials found (unlisted or not configured) - request CA-certified turnover statement")


# --------------------------------------------------------------------------- registrations (identity)
CIN_RE = re.compile(r"^([LU])(\d{5})([A-Z]{2})(\d{4})(PLC|PTC|OPC|GOI|SGC|FLC|FTC|NPL|ULL|ULT|GAP|GAT)(\d{6})$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-\d{4}$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")
CIN_STATE = {"UP": "Uttar Pradesh", "MH": "Maharashtra", "KA": "Karnataka", "DL": "Delhi", "TN": "Tamil Nadu",
             "TG": "Telangana", "TS": "Telangana", "GJ": "Gujarat", "HR": "Haryana", "WB": "West Bengal",
             "RJ": "Rajasthan", "MP": "Madhya Pradesh", "KL": "Kerala", "AP": "Andhra Pradesh", "PB": "Punjab",
             "OR": "Odisha", "BR": "Bihar", "CH": "Chandigarh", "GA": "Goa", "JH": "Jharkhand", "CT": "Chhattisgarh",
             "UR": "Uttarakhand", "HP": "Himachal Pradesh", "AS": "Assam", "JK": "Jammu and Kashmir"}


def check_registrations(bid: dict, rep: Report, enriched: dict | None):
    cin = (bid.get("cin") or "").strip().upper()
    if cin:
        if LLPIN_RE.match(cin):
            rep.add("identity", "cin_format", CONFIRMED, 1.0, f"LLPIN {cin} format valid", "MCA LLPIN format")
        else:
            m = CIN_RE.match(cin)
            if not m:
                rep.add("identity", "cin_format", RED_FLAG, 0.0, f"{cin} is not a valid CIN", "MCA CIN format", knockout=True)
            else:
                listed, _, st, yr, kind, _ = m.groups()
                rep.add("identity", "cin_format", CONFIRMED, 1.0,
                        f"CIN valid: {'listed' if listed == 'L' else 'unlisted'} {kind} company, incorporated {yr} in {CIN_STATE.get(st, st)}",
                        "MCA CIN format")
                cy = _num(bid.get("claimed_year_founded")) or _num(((enriched or {}).get("basic_info") or {}).get("year_founded"))
                if cy and abs(int(yr) - cy) > 1:
                    rep.add("identity", "cin_year", CONTRADICTED, 0.3,
                            f"CIN says incorporated {yr}; founding year given/observed is {cy:.0f}", "MCA CIN format")
                mkts = ((enriched or {}).get("basic_info") or {}).get("markets") or []
                if listed == "U" and any(x in ("NSE", "BSE") for x in mkts):
                    rep.add("identity", "cin_listing", CONTRADICTED, 0.3, "CIN says unlisted but the firm trades on NSE/BSE",
                            "MCA CIN format + crustdata basic_info.markets")
                g = (bid.get("gstin") or "").upper()
                if len(g) == 15 and CIN_STATE.get(st) and GST_STATES.get(g[:2]) and CIN_STATE[st] != GST_STATES[g[:2]]:
                    rep.add("identity", "cin_gst_state", CANT_VERIFY, 0.8,
                            f"Registered office state ({CIN_STATE[st]}) differs from GSTIN state ({GST_STATES[g[:2]]}) - "
                            "normal if this GSTIN is a branch registration", "CIN vs GSTIN")
    udyam = (bid.get("udyam") or "").strip().upper()
    if udyam:
        ok = bool(UDYAM_RE.match(udyam))
        rep.add("identity", "udyam_format", CONFIRMED if ok else RED_FLAG, 1.0 if ok else 0.1,
                f"Udyam number {udyam} {'format valid' if ok else 'is malformed (MSE preference claim doubtful)'}",
                "Udyam registration format")


def check_experience(bid: dict, rep: Report, enriched: dict | None):
    """Tender's minimum years-in-business rule, using the observed founding year (else declared)."""
    need = _num((bid.get("tender") or {}).get("min_experience_years"))
    yr = _num(((enriched or {}).get("basic_info") or {}).get("year_founded"))
    src = "crustdata:/company/enrich basic_info.year_founded"
    if not yr:
        yr, src = _num(bid.get("claimed_year_founded")), "bid declaration (unverified)"
    if need and yr:
        have = dt.date.today().year - yr
        ok = have >= need
        rep.add("capacity", "tender_experience_rule", CONFIRMED if ok else RED_FLAG, 1.0 if ok else 0.0,
                f"{have:.0f} years in business vs tender minimum {need:.0f}" + ("" if ok else " - NOT ELIGIBLE"),
                src, knockout=not ok and "crustdata" in src)


# --------------------------------------------------------------------------- declared cases
CASE_SEVERITY = [
    (re.compile(r"criminal|fraud|cheating|420|fir|cbi|enforcement|pmla|forgery", re.I), "criminal/fraud", 0.0, True),
    (re.compile(r"debar|blacklist|banned", re.I), "debarment", 0.0, True),
    (re.compile(r"insolven|nclt|ibc|cirp|liquidat|winding", re.I), "insolvency", 0.2, False),
    (re.compile(r"tax|gst|income tax|show cause|penalty", re.I), "regulatory", 0.5, False),
    (re.compile(r".", re.I), "civil/other", 0.7, False),
]


def score_declared_cases(bid: dict, rep: Report, vendor: str):
    """Cases the reviewer already found (form). Active serious matters weigh like agent findings."""
    for case in bid.get("known_cases") or []:
        party = (case.get("party") or vendor).strip()
        cat = "firm_legal" if (not case.get("party") or name_sim(vendor, party) >= 85) else "people_legal"
        text = " ".join(str(case.get(k) or "") for k in ("nature", "court", "case_no"))
        active = (case.get("status") or "pending").lower() not in ("disposed", "closed", "dismissed", "acquitted", "withdrawn")
        for rx, sev, s, ko in CASE_SEVERITY:
            if rx.search(text):
                break
        rep.add(cat, f"declared_case:{party}", RED_FLAG if active and s < 0.7 else CONTRADICTED if active else CONFIRMED,
                s if active else 0.9,
                f"{'ACTIVE' if active else 'Closed'} {sev} case vs {party}: {case.get('case_no') or ''} {case.get('court') or ''}".strip(),
                "entered by reviewer", case.get("nature") or "", knockout=ko and active)


# --------------------------------------------------------------------------- 4/6. legal
# Severity of a hit (snippet must also name the subject to count).
LEGAL_TERMS = [
    (re.compile(r"\b(debarred|blacklist(ed)?|banned from (bidding|tender)|suspended from gem)\b", re.I), "debarment", 0.0, True),
    (re.compile(r"\b(fraud|cheating|section 420|forgery|money laundering|pmla|enforcement directorate|cbi (case|probe|raid)|fir (lodged|registered)|chargesheet|arrested)\b", re.I), "criminal/fraud", 0.0, True),
    (re.compile(r"\b(disqualified director|director disqualified|din (deactivated|disqualified))\b", re.I), "director disqualification", 0.1, True),
    (re.compile(r"\b(insolvency|nclt|cirp|ibc|liquidation|winding[- ]up)\b", re.I), "insolvency", 0.2, False),
    (re.compile(r"\b(cag|unfruitful expenditure|show cause|penalty imposed|gst evasion|tax evasion|fake invoice)\b", re.I), "regulatory", 0.4, False),
    (re.compile(r"\b(arbitration|recovery suit|civil suit|writ petition|petitioner|respondent|high court|district court|pending case)\b", re.I), "civil case", 0.6, False),
]
LEGAL_QUERY = ('(fraud OR cheating OR blacklisted OR debarred OR insolvency OR NCLT OR "winding up" '
               'OR arbitration OR "high court" OR FIR OR CBI OR "enforcement directorate")')


def legal_scan(c: Crust, subject: str, context: str, category: str, rep: Report, label: str):
    """Search court records (Indian Kanoon) + web/news for cases naming `subject`.
    For a person, `context` is the firm's short name and must also appear (namesake guard)."""
    hits, seen = [], set()
    ctx = f' "{context}"' if context else ""
    queries = [
        (f'"{subject}"{ctx}', {"site": "indiankanoon.org"}, "court"),
        (f'"{subject}"{ctx} {LEGAL_QUERY}', {}, "news"),
    ]
    for q, extra, kind in queries:
        try:
            results = web_search(c, q, rep, **extra)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            rep.add(category, f"{label}_search", CANT_VERIFY, None, f"search failed: {e}")
            continue
        for r in results[:10]:
            url = r.get("url", "")
            if url in seen:
                continue
            seen.add(url)
            text = f"{r.get('title', '')} {snippet_of(r)}"
            # The snippet itself must name the subject (and the firm, for a person). Search engines
            # return lots of generic NCLT cause lists etc. that never mention the subject.
            if not mentions(subject, text) or (context and not mentions(context, text)):
                continue
            for rx, sev, s, ko in LEGAL_TERMS:
                if rx.search(text):
                    hits.append((sev, s, ko, url, snippet_of(r), str(r.get("date") or "")))
                    break
    if not hits:
        # Absence of hits is weaker evidence than a clean certificate: cap at 0.85.
        rep.add(category, f"{label}_cases", CONFIRMED, 0.85,
                f"No cases / adverse records found naming {subject} (web search can miss unindexed "
                f"matters - confirm on ecourts.gov.in)", "crustdata:/web/search/live (indiankanoon.org + web)")
        return
    hits.sort(key=lambda h: h[1])
    for sev, s, ko, url, snip, date in hits[:3]:
        # Knockouts need a strong signal: a court record or 2+ independent hits.
        strong = "indiankanoon" in url or "ecourts" in url or len(hits) >= 2
        rep.add(category, f"{label}_{sev.replace(' ', '_').replace('/', '_')}", RED_FLAG, s,
                f"FOUND BY AGENT: {sev.upper()} record naming {subject}", url, snip, date, knockout=ko and strong)


def check_firm_legal(c: Crust, bid: dict, rep: Report):
    legal_scan(c, bid["vendor_name"], "", "firm_legal", rep, "firm")
    n = _num(bid.get("declared_pending_cases"))
    if n is not None:
        rep.add("firm_legal", "declared_cases", CONFIRMED if n == 0 else CONTRADICTED,
                1.0 if n == 0 else max(0.2, 1 - 0.2 * n), f"Vendor declared {n:.0f} pending case(s)", "bid declaration")


# --------------------------------------------------------------------------- 5. people
def _jobs(profile: dict, when: str) -> list[dict]:
    j = ((profile.get("experience") or {}).get("employment_details") or {}).get(when) or []
    return j if isinstance(j, list) else [j]


def _employer(job: dict) -> str:
    return str(job.get("name") or job.get("employer_name") or job.get("company_name") or "")


def _url(profile: dict) -> str:
    return str((((profile.get("social_handles") or {}).get("professional_network_identifier") or {})
                .get("profile_url")) or "")


def _pname(profile: dict) -> str:
    return str((profile.get("basic_profile") or {}).get("name") or "").strip()


def check_people(c: Crust, bid: dict, rep: Report, company: dict | None, enriched: dict | None = None):
    enriched = enriched or {}
    vendor = ((enriched.get("basic_info") or (company or {}).get("basic_info") or {}).get("name")) or bid["vendor_name"]
    short = norm_name(vendor) or vendor
    leaders = []
    for grp in ("founders", "cxos", "decision_makers"):
        leaders += [p for p in ((enriched.get("people") or {}).get(grp) or []) if isinstance(p, dict)]

    people = list(bid.get("key_personnel") or [])
    if not people:
        # Nobody named in the bid: check the firm's own leadership from Crustdata instead.
        auto = [p for p in leaders if any(name_sim(vendor, _employer(j)) >= 85 for j in _jobs(p, "current"))]
        people = [{"name": _pname(p), "title": "leadership (auto)"} for p in auto[:3] if _pname(p)]
        if people:
            rep.add("people", "key_personnel", CANT_VERIFY, None,
                    f"Bid names no key personnel; checking firm leadership instead: "
                    f"{', '.join(p['name'] for p in people)}", "crustdata:/company/enrich people")
        else:
            rep.add("people", "key_personnel", CANT_VERIFY, None, "No key personnel listed or found")
            return

    for p in people[:5]:
        nm, src = p["name"], "crustdata:/person/search"
        # 1) free cross-check against the enriched leadership list
        cands = [x for x in leaders if name_sim(nm, _pname(x)) >= 90]
        # 2) otherwise a person search on the name
        if not any(any(name_sim(vendor, _employer(j)) >= 85 for j in _jobs(x, "current")) for x in cands):
            try:
                data = c.post("/person/search", {
                    "filters": {"op": "and", "conditions": [
                        {"field": "basic_profile.name", "type": "(.)", "value": nm}]},
                    "fields": ["basic_profile.name", "basic_profile.headline",
                               "social_handles.professional_network_identifier.profile_url",
                               "experience.employment_details.current.name",
                               "experience.employment_details.current.title",
                               "experience.employment_details.past.name",
                               "experience.employment_details.past.end_date"],
                    "limit": 10}, est_cost=0.7)
                cands += [x for x in (data.get("profiles") or []) if name_sim(nm, _pname(x)) >= 90]
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                rep.add("people", f"person:{nm}", CANT_VERIFY, None, f"search failed: {e}", src)
                continue
        else:
            src = "crustdata:/company/enrich people"

        current = next((x for x in cands if any(name_sim(vendor, _employer(j)) >= 85 for j in _jobs(x, "current"))), None)
        past = next((x for x in cands if any(name_sim(vendor, _employer(j)) >= 85 for j in _jobs(x, "past"))), None)
        if current:
            job = next(j for j in _jobs(current, "current") if name_sim(vendor, _employer(j)) >= 85)
            title = job.get("title") or job.get("employee_title") or ""
            claimed = p.get("title") or ""
            rep.add("people", f"person:{nm}", CONFIRMED, 1.0,
                    f"{nm} currently at {vendor} as '{title}'" + (f" (bid says '{claimed}')" if claimed else ""),
                    src, passage=_url(current), date=str(current.get("updated_at") or ""))
        elif past:
            now = ", ".join(_employer(j) for j in _jobs(past, "current") if _employer(j)) or "unknown"
            rep.add("people", f"person:{nm}", CONTRADICTED, 0.0,
                    f"{nm} has LEFT {vendor}; now at {now} - name used without being on staff", src, passage=_url(past))
        elif cands:
            rep.add("people", f"person:{nm}", CANT_VERIFY, 0.4,
                    f"{len(cands)} people named {nm} found, none linked to {vendor} (namesake risk)", src)
        else:
            rep.add("people", f"person:{nm}", CANT_VERIFY, None, f"No public profile for {nm}", src)

        legal_scan(c, nm, short, "people_legal", rep, f"person:{nm}")


# --------------------------------------------------------------------------- scoring
def score(rep: Report) -> dict:
    cats = {}
    for cat, w in WEIGHTS.items():
        vals = [f.score for f in rep.findings if f.category == cat and f.score is not None]
        # Red flags dominate: category score is min(mean, worst red flag)
        reds = [f.score for f in rep.findings if f.category == cat and f.status == RED_FLAG and f.score is not None]
        s = None
        if vals:
            s = sum(vals) / len(vals)
            if reds:
                s = min(s, min(reds))
        cats[cat] = {"weight": w, "score": None if s is None else round(s, 3)}
    total_w = sum(WEIGHTS.values())
    raw = sum(v["weight"] * (UNKNOWN_PRIOR if v["score"] is None else v["score"]) for v in cats.values())
    coverage = sum(v["weight"] for v in cats.values() if v["score"] is not None) / total_w
    final = round(100 * raw / total_w, 1)
    knockouts = [f for f in rep.findings if f.knockout]
    if knockouts or final < HOLD_AT:
        verdict = "REJECT"
    elif final >= APPROVE_AT and coverage >= MIN_COVERAGE:
        verdict = "APPROVE"
    else:
        verdict = "HOLD"
    return {"score": final, "coverage": round(coverage, 2), "verdict": verdict, "categories": cats,
            "knockouts": [f"{f.check}: {f.detail}" for f in knockouts]}


# --------------------------------------------------------------------------- intake
ASK = [
    ("gstin", "GSTIN (15 chars, from the seller's GeM profile)"),
    ("pan", "PAN of the firm"),
    ("cin", "CIN / LLPIN (blank if proprietorship/partnership)"),
    ("udyam", "Udyam registration no. (only if claiming MSE preference)"),
    ("state", "State of registration (e.g. Karnataka)"),
    ("entity_type", "Entity type (private limited / public limited / llp / partnership / proprietorship)"),
    ("stock_ticker", "Stock ticker if listed, e.g. KSOLVES.NSE (blank = auto-detect)"),
    ("claimed_turnover_inr", "Claimed turnover, latest FY, in INR"),
    ("net_worth_inr", "Declared net worth in INR"),
    ("gst_status", "GST status if already checked (active/cancelled/suspended) - blank = check via API"),
    ("gstr3b_filed_last_12", "GSTR-3B returns filed in last 12 months (0-12) - blank = check via API"),
    ("gst_registration_date", "GST registration date YYYY-MM-DD - blank = check via API"),
]


def intake(bid: dict, interactive: bool) -> dict:
    for req in ("vendor_name", "domain"):
        if not bid.get(req):
            if not interactive:
                sys.exit(f"bid is missing required field '{req}'")
            bid[req] = input(f"{req}: ").strip()
    if interactive:
        missing = [(k, q) for k, q in ASK if bid.get(k) in (None, "")]
        if missing:
            print("\nSome details would improve the check (press Enter to skip):")
        for k, q in missing:
            v = input(f"  {q}: ").strip()
            if v:
                bid[k] = v
        if not bid.get("turnover_last_3fy_inr"):
            v = input("  Turnover of last 3 FYs in INR, latest first, comma-separated (blank to skip): ").strip()
            if v:
                bid["turnover_last_3fy_inr"] = [x.strip() for x in v.split(",") if x.strip()]
        t = bid.setdefault("tender", {})
        for k, q in (("value_inr", "Tender / bid value in INR"),
                     ("min_avg_turnover_inr", "Tender's minimum average annual turnover (INR)"),
                     ("min_experience_years", "Tender's minimum years of experience")):
            if t.get(k) in (None, ""):
                v = input(f"  {q} (blank to skip): ").strip()
                if v:
                    t[k] = v
        if "known_cases" not in bid:
            print("  Cases you already found (party | case no | court | nature | status) - blank line to finish:")
            cases = []
            while True:
                line = input("    > ").strip()
                if not line:
                    break
                parts = [x.strip() for x in line.split("|")] + [""] * 5
                cases.append(dict(zip(("party", "case_no", "court", "nature", "status"), parts[:5])))
            bid["known_cases"] = cases
        if not bid.get("key_personnel"):
            print("  Key personnel (name, title) - blank line to finish:")
            people = []
            while True:
                line = input("    > ").strip()
                if not line:
                    break
                n, _, t = line.partition(",")
                people.append({"name": n.strip(), "title": t.strip()})
            bid["key_personnel"] = people
    return bid


# --------------------------------------------------------------------------- main
def run(bid: dict, client: Crust) -> tuple[Report, dict]:
    rep = Report(bid["vendor_name"])
    company = None
    try:
        company = check_identity(client, bid, rep)
        enriched = check_capacity(client, bid, rep, company)
        check_registrations(bid, rep, enriched)
        check_experience(bid, rep, enriched)
        check_gst(client, bid, rep)
        check_financials(client, bid, rep, company or enriched)
        score_declared_cases(bid, rep, ((company or {}).get("basic_info") or {}).get("name") or bid["vendor_name"])
        check_firm_legal(client, bid, rep)
        check_people(client, bid, rep, company, enriched)
    except BudgetExceeded as e:
        rep.add("identity", "budget", CANT_VERIFY, None, f"Stopped early: budget hit ({e}); remaining checks skipped")
    rep.credits_spent = getattr(client, "spent", 0.0)
    return rep, score(rep)


ICON = {CONFIRMED: "OK ", CONTRADICTED: "XX ", CANT_VERIFY: "?? ", RED_FLAG: "!! "}


def print_report(rep: Report, s: dict):
    print(f"\n=== GeM Bid Check: {rep.vendor} ===")
    for cat in WEIGHTS:
        cs = s["categories"][cat]
        sc = "n/a" if cs["score"] is None else f"{cs['score']*100:.0f}"
        print(f"\n[{cat}]  weight {cs['weight']}  sub-score {sc}")
        for f in [f for f in rep.findings if f.category == cat]:
            print(f"  {ICON[f.status]}{f.check}: {f.detail}" + ("  [KNOCKOUT]" if f.knockout else ""))
            if f.source:
                print(f"       source: {f.source}" + (f"  ({f.date})" if f.date else ""))
            if f.passage:
                print(f"       \"{f.passage[:160]}\"")
    print(f"\nScore {s['score']}/100   coverage {s['coverage']:.0%}   credits {rep.credits_spent:.1f}")
    for k in s["knockouts"]:
        print(f"  KNOCKOUT -> {k}")
    print(f"VERDICT: {s['verdict']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bid")
    ap.add_argument("--no-prompt", action="store_true", help="don't ask for missing details")
    ap.add_argument("--yes", action="store_true", help="skip human confirmation of REJECT (demo)")
    ap.add_argument("--out", default="report.json")
    ap.add_argument("--replay", metavar="FIXTURE", help="run offline on recorded API responses (e.g. fixtures/ksolves_live.json)")
    a = ap.parse_args(argv)
    bid = intake(json.load(open(a.bid)), interactive=not a.no_prompt and sys.stdin.isatty())
    if a.replay:
        client = Crust("", db=":memory:", replay=a.replay)
    else:
        client = Crust(os.environ.get("CRUSTDATA_API_KEY") or sys.exit("set CRUSTDATA_API_KEY (or use --replay)"))
    rep, s = run(bid, client)
    print_report(rep, s)
    if s["verdict"] == "REJECT" and not a.yes:
        ok = input("\nREJECT requires human sign-off. Confirm reject? [y/N] ").strip().lower() == "y"
        s["human_confirmed"] = ok
        if not ok:
            s["verdict"] = "HOLD"
            print("Downgraded to HOLD pending manual review.")
    json.dump({"bid": bid, "result": s, "findings": [asdict(f) for f in rep.findings],
               "credits_spent": rep.credits_spent, "generated": dt.datetime.now().isoformat()},
              open(a.out, "w"), indent=2)
    print(f"\nWrote {a.out}")


if __name__ == "__main__":
    main()
