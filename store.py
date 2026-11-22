"""SQLite store for the government dashboard: vendors (bids), GST registry, checks, uploads.

Everything is keyed on GSTIN where one exists. Rows can come from manual uploads (CSV / JSON / XLSX
converted in the browser), from the bids/ folder, or from automatic GSTIN lookups.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "dashboard.db"

SCHEMA = """
create table if not exists vendors(
  id integer primary key autoincrement,
  gstin text, vendor_name text not null, domain text, state text, pan text,
  data text not null, source text, created text, updated text);
create index if not exists vendors_gstin on vendors(gstin);
create index if not exists vendors_pan on vendors(pan);
create table if not exists gst_registry(
  gstin text primary key, legal_name text, trade_name text, status text, registration_date text,
  gstr3b_filed_last_12 real, state text, pan text, constitution text, address text,
  source text, updated text, raw text);
create table if not exists checks(
  id integer primary key autoincrement, vendor_id integer, gstin text, vendor_name text,
  verdict text, score real, coverage real, mode text, knockouts integer,
  result text, findings text, created text, reviewed_by text, review_note text);
create index if not exists checks_vendor on checks(vendor_id);
create table if not exists uploads(
  id integer primary key autoincrement, kind text, filename text, rows_in integer,
  inserted integer, updated integer, skipped integer, errors text, created text);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# --------------------------------------------------------------------------- normalisation
def _key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


VENDOR_ALIASES = {
    "vendor_name": ["vendor_name", "vendor", "name", "company", "company_name", "firm", "firm_name",
                    "seller", "seller_name", "bidder", "bidder_name", "legal_name"],
    "domain": ["domain", "website", "web", "url", "site"],
    "gstin": ["gstin", "gst", "gst_no", "gst_number", "gstn", "gstin_no", "gstin_number", "gst_in"],
    "pan": ["pan", "pan_no", "pan_number"],
    "state": ["state", "state_of_registration", "registered_state"],
    "entity_type": ["entity_type", "entity", "constitution", "type_of_entity", "company_type"],
    "claimed_headcount": ["claimed_headcount", "headcount", "employees", "staff", "employee_count"],
    "claimed_year_founded": ["claimed_year_founded", "year_founded", "founded", "incorporation_year"],
    "claimed_turnover_inr": ["claimed_turnover_inr", "turnover", "turnover_inr", "annual_turnover"],
    "declared_pending_cases": ["declared_pending_cases", "pending_cases", "cases"],
    "gst_status": ["gst_status", "status"],
    "gstr3b_filed_last_12": ["gstr3b_filed_last_12", "gstr3b_filed", "returns_filed", "gstr_3b"],
    "gst_registration_date": ["gst_registration_date", "registration_date", "gst_reg_date", "rgdt"],
    "key_personnel": ["key_personnel", "personnel", "key_people", "people"],
    "tender_id": ["tender_id", "bid_id", "bid_no", "bid_number", "tender_no"],
    "tender_title": ["tender_title", "bid_title", "work", "tender_name"],
    "tender_value_inr": ["tender_value_inr", "tender_value", "bid_value", "contract_value", "value_inr"],
    "department": ["department", "buyer", "ministry", "buyer_department"],
}

REGISTRY_ALIASES = {
    "gstin": VENDOR_ALIASES["gstin"],
    "legal_name": ["legal_name", "lgnm", "legal_name_of_business", "name", "taxpayer_name"],
    "trade_name": ["trade_name", "tradenam", "trade_nam", "trade_name_of_business"],
    "status": ["status", "sts", "gstin_status", "gst_status"],
    "registration_date": ["registration_date", "rgdt", "date_of_registration", "gst_registration_date"],
    "gstr3b_filed_last_12": VENDOR_ALIASES["gstr3b_filed_last_12"],
    "constitution": ["constitution", "ctb", "constitution_of_business", "entity_type"],
    "address": ["address", "pradr", "principal_place", "principal_address"],
}

NUMERIC = {"claimed_headcount", "claimed_year_founded", "claimed_turnover_inr", "declared_pending_cases",
           "gstr3b_filed_last_12", "tender_value_inr"}


def norm_date(v) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    for cand in (s, s[:10]):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
            try:
                return dt.datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


def norm_num(v):
    if v in (None, ""):
        return None
    try:
        f = float(str(v).replace(",", "").replace("₹", "").strip())
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def pick(row: dict, aliases: dict) -> dict:
    flat = {_key(k): v for k, v in row.items()}
    out = {}
    for field, names in aliases.items():
        for n in names:
            if n in flat and flat[n] not in (None, ""):
                out[field] = flat[n]
                break
    return out


def parse_people(v) -> list[dict]:
    """'Asha Raman|Project Director; Vikram Sethi|Architect'  or a JSON list."""
    if isinstance(v, list):
        return [p if isinstance(p, dict) else {"name": str(p), "title": ""} for p in v]
    s = str(v or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return parse_people(json.loads(s))
        except ValueError:
            pass
    people = []
    for part in re.split(r"[;\n]", s):
        if part.strip():
            bits = re.split(r"\s*[|:]\s*", part.strip(), maxsplit=1)
            people.append({"name": bits[0].strip(), "title": bits[1].strip() if len(bits) > 1 else ""})
    return people


def normalise_vendor(row: dict) -> dict | None:
    """Accepts a flat CSV row or the nested bid JSON used by gem_checker.py; returns a bid dict."""
    nested_tender = row.get("tender") if isinstance(row.get("tender"), dict) else {}
    r = pick(row, VENDOR_ALIASES)
    bid: dict = {}
    for k, v in r.items():
        if k in ("tender_id", "tender_title", "tender_value_inr"):
            continue
        if k in NUMERIC:
            v = norm_num(v)
            if v is None:
                continue
        elif k == "key_personnel":
            v = parse_people(v)
        elif k == "gst_registration_date":
            v = norm_date(v)
        elif isinstance(v, str):
            v = v.strip()
        bid[k] = v
    tender = dict(nested_tender)
    if r.get("tender_id"):
        tender["id"] = str(r["tender_id"]).strip()
    if r.get("tender_title"):
        tender["title"] = str(r["tender_title"]).strip()
    if r.get("tender_value_inr") not in (None, ""):
        tender["value_inr"] = norm_num(r["tender_value_inr"])
    if tender:
        bid["tender"] = tender
    if bid.get("gstin"):
        bid["gstin"] = re.sub(r"\s", "", str(bid["gstin"])).upper()
    if bid.get("pan"):
        bid["pan"] = str(bid["pan"]).strip().upper()
    if bid.get("domain"):
        bid["domain"] = re.sub(r"^https?://(www\.)?", "", str(bid["domain"]).strip().lower()).split("/")[0]
    if not bid.get("vendor_name") and not bid.get("gstin"):
        return None
    return bid


def normalise_registry(row: dict) -> dict | None:
    r = pick(row, REGISTRY_ALIASES)
    g = re.sub(r"\s", "", str(r.get("gstin") or "")).upper()
    if not g:
        return None
    out = {"gstin": g}
    for k in ("legal_name", "trade_name", "constitution"):
        if r.get(k):
            out[k] = str(r[k]).strip()
    if r.get("status"):
        out["status"] = str(r["status"]).strip().lower()
    if r.get("registration_date"):
        out["registration_date"] = norm_date(r["registration_date"])
    if r.get("gstr3b_filed_last_12") not in (None, ""):
        out["gstr3b_filed_last_12"] = norm_num(r["gstr3b_filed_last_12"])
    addr = r.get("address")
    if isinstance(addr, dict):  # GSTN API shape: pradr.addr.{bno,st,loc,dst,stcd,pncd}
        a = addr.get("addr") or addr
        addr = ", ".join(str(a[k]) for k in ("bno", "flno", "bnm", "st", "loc", "dst", "stcd", "pncd") if a.get(k))
    if addr:
        out["address"] = str(addr).strip()
    return out


def parse_text(content: str, filename: str) -> list[dict]:
    """CSV or JSON text -> list of row dicts."""
    s = content.lstrip("﻿").strip()
    if filename.lower().endswith(".json") or s[:1] in "[{":
        data = json.loads(s)
        if isinstance(data, dict):
            for k in ("vendors", "bids", "rows", "data", "records", "taxpayers"):
                if isinstance(data.get(k), list):
                    return data[k]
            return [data]
        return [x for x in data if isinstance(x, dict)]
    return list(csv.DictReader(io.StringIO(s)))


# --------------------------------------------------------------------------- vendors
def vendor_row(r: sqlite3.Row) -> dict:
    d = json.loads(r["data"])
    d["_id"], d["_source"], d["_updated"] = r["id"], r["source"], r["updated"]
    return d


def find_vendor(db, bid: dict):
    if bid.get("gstin"):
        r = db.execute("select * from vendors where gstin=? and lower(vendor_name)=lower(?)",
                       (bid["gstin"], bid.get("vendor_name") or "")).fetchone()
        if r:
            return r
        r = db.execute("select * from vendors where gstin=?", (bid["gstin"],)).fetchall()
        if len(r) == 1 and not bid.get("vendor_name"):
            return r[0]
    if bid.get("vendor_name"):
        return db.execute("select * from vendors where lower(vendor_name)=lower(?) and coalesce(domain,'')=?"
                          " and coalesce(gstin,'')=?",
                          (bid["vendor_name"], bid.get("domain") or "", bid.get("gstin") or "")).fetchone()
    return None


def upsert_vendor(db, bid: dict, source: str) -> str:
    """Returns 'inserted' or 'updated'. Existing fields are kept unless the new row supplies them."""
    existing = find_vendor(db, bid)
    if existing:
        merged = {**json.loads(existing["data"]), **bid}
        db.execute("update vendors set gstin=?, vendor_name=?, domain=?, state=?, pan=?, data=?, source=?, updated=?"
                   " where id=?", (merged.get("gstin"), merged.get("vendor_name") or merged.get("gstin"),
                                    merged.get("domain"), merged.get("state"), merged.get("pan"),
                                    json.dumps(merged), source, now(), existing["id"]))
        return "updated"
    if not bid.get("vendor_name"):
        bid["vendor_name"] = bid["gstin"]
    db.execute("insert into vendors(gstin,vendor_name,domain,state,pan,data,source,created,updated)"
               " values(?,?,?,?,?,?,?,?,?)",
               (bid.get("gstin"), bid["vendor_name"], bid.get("domain"), bid.get("state"), bid.get("pan"),
                json.dumps(bid), source, now(), now()))
    return "inserted"


def upsert_registry(db, rec: dict, source: str) -> str:
    old = db.execute("select * from gst_registry where gstin=?", (rec["gstin"],)).fetchone()
    cols = ["legal_name", "trade_name", "status", "registration_date", "gstr3b_filed_last_12", "state", "pan",
            "constitution", "address"]
    merged = {c: (old[c] if old else None) for c in cols}
    merged.update({k: v for k, v in rec.items() if k in cols and v not in (None, "")})
    raw = json.dumps(rec.get("raw") or rec)
    if old:
        db.execute(f"update gst_registry set {', '.join(c + '=?' for c in cols)}, source=?, updated=?, raw=? where gstin=?",
                   [merged[c] for c in cols] + [source, now(), raw, rec["gstin"]])
        return "updated"
    db.execute(f"insert into gst_registry(gstin, {', '.join(cols)}, source, updated, raw) values(?{',?' * len(cols)},?,?,?)",
               [rec["gstin"]] + [merged[c] for c in cols] + [source, now(), raw])
    return "inserted"


def registry(db, gstin: str) -> dict | None:
    r = db.execute("select * from gst_registry where gstin=?", (gstin,)).fetchone()
    return dict(r) if r else None


def latest_check(db, vendor_id: int) -> dict | None:
    r = db.execute("select * from checks where vendor_id=? order by id desc limit 1", (vendor_id,)).fetchone()
    return check_row(r) if r else None


def check_row(r) -> dict:
    d = dict(r)
    d["result"] = json.loads(d["result"] or "{}")
    d["findings"] = json.loads(d["findings"] or "[]")
    return d
