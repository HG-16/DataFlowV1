"""External data providers beyond Crustdata: GST in Cloud (GSTIN status / returns) and EODHD (financials).

Both are optional - with no key configured, the matching checks report "can't verify" and the
weighted score treats that category as unknown (neutral prior), exactly like missing Crustdata data.

Configuration (environment variables):
  GSTINCLOUD_API_KEY      key for the GST in Cloud API
  GSTINCLOUD_URL          URL template with {gstin}, e.g. https://<host>/<path>/{gstin}
  GSTINCLOUD_AUTH         how the key is sent: "header:<Header-Name>" (default "header:Authorization",
                          sent as "Bearer <key>") or "query:<param>" (e.g. query:api_key)
  EODHD_API_KEY           key for https://eodhd.com (fundamentals + search)

GST responses are normalised from the field names the GSTN taxpayer-search API uses
(sts, lgnm, tradeNam, rgdt, cxdt, ctb, stj, pradr, filing list with rtntype/ret_prd/dof/status), which
most GST API resellers pass through unchanged, plus common camelCase/snake_case aliases.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time


class Http:
    """Cached GET client with replay support (same idea as the Crustdata client).
    Secrets are stripped from cache keys so recorded fixtures never contain keys."""

    SECRET_PARAMS = {"api_token", "api_key", "apikey", "key", "token", "access_token"}

    def __init__(self, db: sqlite3.Connection, offline: bool = False):
        self.db, self.offline = db, offline
        self.db.execute("create table if not exists http_cache(k text primary key, v text)")

    @classmethod
    def key(cls, url: str, params: dict | None) -> str:
        clean = {k: v for k, v in (params or {}).items() if k not in cls.SECRET_PARAMS}
        return hashlib.sha256((url + json.dumps(clean, sort_keys=True)).encode()).hexdigest()

    def preload(self, calls: list[dict]):
        for c in calls:   # a recorded failure replays as the same error
            val = {"__error__": c["response_error"]} if "response_error" in c else c["response"]
            self.db.execute("insert or replace into http_cache values(?,?)",
                            (self.key(c["url"], c.get("params")), json.dumps(val)))
        self.db.commit()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        k = self.key(url, params)
        row = self.db.execute("select v from http_cache where k=?", (k,)).fetchone()
        if row:
            val = json.loads(row[0])
            if isinstance(val, dict) and "__error__" in val:
                raise RuntimeError(val["__error__"])
            return val
        if self.offline:
            raise RuntimeError(f"{url}: not in replay file (offline mode)")
        import httpx

        for attempt in range(3):
            r = httpx.get(url, params=params, headers=headers or {}, timeout=45)
            if r.status_code not in (429, 502, 503):
                break
            time.sleep(2 ** attempt)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        self.db.execute("insert or replace into http_cache values(?,?)", (k, json.dumps(data)))
        self.db.commit()
        return data


def _dig(obj, *keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, "", [], {}):
                return obj[k]
        for v in obj.values():
            r = _dig(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig(v, *keys)
            if r is not None:
                return r
    return None


def parse_date(s) -> dt.date | None:
    if not s:
        return None
    s = str(s).strip()[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- GST in Cloud
def gst_configured() -> bool:
    return bool(os.environ.get("GSTINCLOUD_API_KEY") and os.environ.get("GSTINCLOUD_URL"))


def gst_lookup(http: Http, gstin: str) -> dict:
    """Fetch and normalise a GSTIN record. Raises if not configured or the call fails."""
    key, tmpl = os.environ.get("GSTINCLOUD_API_KEY", ""), os.environ.get("GSTINCLOUD_URL", "")
    if not tmpl and not http.offline:
        raise RuntimeError("GSTINCLOUD_URL not set")
    tmpl = tmpl or "https://gstincloud/{gstin}"          # replay mode: fixed placeholder URL
    auth = os.environ.get("GSTINCLOUD_AUTH", "header:Authorization")
    mode, _, name = auth.partition(":")
    params, headers = {}, {}
    if mode == "query":
        params[name or "api_key"] = key
    else:
        headers[name or "Authorization"] = f"Bearer {key}" if (name or "Authorization") == "Authorization" else key
    raw = http.get(tmpl.format(gstin=gstin), params=params or None, headers=headers)
    return normalise_gst(raw)


def normalise_gst(raw) -> dict:
    status = _dig(raw, "sts", "status", "gstin_status", "gstinStatus", "taxpayer_status", "Status")
    addr = _dig(raw, "pradr", "principal_address", "principalAddress")
    addr_txt = ""
    if isinstance(addr, dict):
        a = addr.get("addr", addr)
        addr_txt = ", ".join(str(a.get(k)) for k in ("bno", "bnm", "flno", "st", "loc", "dst", "stcd", "pncd")
                             if isinstance(a, dict) and a.get(k)) or str(_dig(addr, "adr", "address") or "")
    filings = []
    for f in _dig(raw, "filing", "filings", "returns", "EFiledlist", "filingStatus", "filing_status") or []:
        if isinstance(f, list):          # some APIs nest one list per return type
            filings += [x for x in f if isinstance(x, dict)]
        elif isinstance(f, dict):
            filings.append(f)
    norm_filings = [{
        "type": str(_dig(f, "rtntype", "return_type", "returnType", "rtnType") or "").upper(),
        "period": str(_dig(f, "ret_prd", "tax_period", "taxPeriod", "period", "fy") or ""),
        "filed_on": str(_dig(f, "dof", "date_of_filing", "dateOfFiling", "filing_date") or ""),
        "status": str(_dig(f, "status", "sts") or ""),
    } for f in filings]
    return {
        "status": str(status or "").strip(),
        "legal_name": str(_dig(raw, "lgnm", "legal_name", "legalName", "LegalName") or ""),
        "trade_name": str(_dig(raw, "tradeNam", "trade_name", "tradeName", "TradeName") or ""),
        "registration_date": str(_dig(raw, "rgdt", "registration_date", "registrationDate", "dateOfRegistration") or ""),
        "cancellation_date": str(_dig(raw, "cxdt", "cancellation_date", "cancellationDate") or ""),
        "constitution": str(_dig(raw, "ctb", "constitution_of_business", "constitutionOfBusiness") or ""),
        "taxpayer_type": str(_dig(raw, "dty", "taxpayer_type", "taxpayerType") or ""),
        "state_jurisdiction": str(_dig(raw, "stj", "state_jurisdiction", "stateJurisdiction") or ""),
        "address": addr_txt,
        "einvoice": str(_dig(raw, "einvoiceStatus", "einvoice_status") or ""),
        "filings": norm_filings,
    }


def gstr3b_filed_last_12(filings: list[dict], today: dt.date | None = None) -> int | None:
    """Number of distinct monthly GSTR-3B periods filed in the 12 months before `today`.
    Periods look like 'MMYYYY' (GSTN) or 'YYYY-MM'. Returns None if no 3B data present."""
    today = today or dt.date.today()
    periods = set()
    seen_3b = False
    for f in filings:
        if "3B" not in f["type"]:
            continue
        seen_3b = True
        if f["status"] and f["status"].lower() not in ("filed", "y", "yes", "submitted"):
            continue
        p = re.sub(r"[^0-9]", "", f["period"])
        if len(p) == 6:
            mm, yy = (int(p[:2]), int(p[2:])) if int(p[:2]) <= 12 else (int(p[4:]), int(p[:4]))
            months_ago = (today.year - yy) * 12 + today.month - mm
            if 1 <= months_ago <= 12:
                periods.add((yy, mm))
    return len(periods) if seen_3b else None


# --------------------------------------------------------------------------- EODHD
EODHD = "https://eodhd.com/api"


def eodhd_configured() -> bool:
    return bool(os.environ.get("EODHD_API_KEY"))


def eodhd_find_ticker(http: Http, name: str) -> list[dict]:
    """Search listed securities by name; returns [{Code, Exchange, Name, ISIN, Type, Country}]."""
    q = re.sub(r"\b(private|pvt|limited|ltd)\b\.?", " ", name, flags=re.I).strip()
    data = http.get(f"{EODHD}/search/{q}", params={"api_token": os.environ.get("EODHD_API_KEY", ""), "fmt": "json"})
    return [d for d in (data if isinstance(data, list) else []) if isinstance(d, dict)]


def eodhd_fundamentals(http: Http, ticker: str) -> dict:
    """ticker like 'KSOLVES.NSE'. Returns a compact summary of yearly financials (reporting currency)."""
    raw = http.get(f"{EODHD}/fundamentals/{ticker}",
                   params={"api_token": os.environ.get("EODHD_API_KEY", ""), "fmt": "json"})
    gen = raw.get("General") or {}
    fin = raw.get("Financials") or {}

    def yearly(stmt):
        y = ((fin.get(stmt) or {}).get("yearly")) or {}
        rows = sorted(((k, v) for k, v in y.items() if isinstance(v, dict)), key=lambda kv: kv[0], reverse=True)
        return rows

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    inc = yearly("Income_Statement")
    bal = {k: v for k, v in yearly("Balance_Sheet")}
    years = []
    for date, row in inc[:4]:
        b = bal.get(date, {})
        years.append({"date": date, "revenue": num(row.get("totalRevenue")), "net_income": num(row.get("netIncome")),
                      "equity": num(b.get("totalStockholderEquity")), "total_liab": num(b.get("totalLiab"))})
    return {"name": gen.get("Name") or "", "isin": gen.get("ISIN") or "", "exchange": gen.get("Exchange") or "",
            "currency": gen.get("CurrencyCode") or "", "updated": gen.get("UpdatedAt") or "",
            "revenue_ttm": num((raw.get("Highlights") or {}).get("RevenueTTM")),
            "market_cap": num((raw.get("Highlights") or {}).get("MarketCapitalization")), "years": years}
