"""Government-side dashboard for the GeM Bid Checker. Standard library only.

Run:   python dashboard/server.py            -> http://127.0.0.1:8765
Env:   CRUSTDATA_API_KEY   full live checks + web GSTIN search
       GSP_API_URL         GST API (e.g. https://gsp.example/taxpayer/{gstin}); GSP_API_KEY, GSP_AUTH_HEADER
       DASHBOARD_PORT      default 8765
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine  # noqa: E402
import store  # noqa: E402
import verify  # noqa: E402

MAX_BODY = 80 * 1024 * 1024

DB = store.connect(os.environ.get("DASHBOARD_DB") or store.DB_PATH)
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
USER = os.environ.get("DASHBOARD_USER", "")
LOCK = threading.Lock()   # one writer at a time; sqlite connection is shared

TEMPLATES = {
    "vendors": ["vendor_name", "gstin", "domain", "pan", "state", "entity_type", "claimed_headcount",
                "claimed_year_founded", "claimed_turnover_inr", "declared_pending_cases", "key_personnel",
                "tender_id", "tender_title", "tender_value_inr", "department"],
    "registry": ["gstin", "legal_name", "trade_name", "status", "registration_date", "gstr3b_filed_last_12",
                 "constitution", "address"],
}


# --------------------------------------------------------------------------- api functions
def list_vendors(q: dict) -> list[dict]:
    rows = DB.execute("""
        select v.*, c.id as check_id, c.verdict, c.score, c.coverage, c.mode, c.knockouts, c.created as checked_at,
               c.reviewed_by, r.status as gst_status_reg, r.legal_name as reg_name
        from vendors v
        left join checks c on c.id = (select max(id) from checks where vendor_id = v.id)
        left join gst_registry r on r.gstin = v.gstin
        order by v.id desc""").fetchall()
    term = (q.get("q") or [""])[0].strip().lower()
    verdict = (q.get("verdict") or [""])[0].upper()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        item = {"id": r["id"], "vendor_name": r["vendor_name"], "gstin": r["gstin"], "domain": r["domain"],
                "state": r["state"] or (engine.decode(r["gstin"]).get("state") if r["gstin"] else None),
                "tender": d.get("tender") or {}, "department": d.get("department"), "source": r["source"],
                "gst_status": r["gst_status_reg"] or d.get("gst_status"), "registry_name": r["reg_name"],
                "verdict": r["verdict"] or "UNCHECKED", "score": r["score"], "coverage": r["coverage"],
                "mode": r["mode"], "knockouts": r["knockouts"], "checked_at": r["checked_at"],
                "check_id": r["check_id"], "reviewed_by": r["reviewed_by"]}
        hay = " ".join(str(x or "") for x in (item["vendor_name"], item["gstin"], item["domain"], item["state"],
                                              item["tender"].get("id"), item["tender"].get("title"))).lower()
        if term and term not in hay:
            continue
        if verdict and item["verdict"] != verdict:
            continue
        out.append(item)
    return out


def stats() -> dict:
    vendors = list_vendors({})
    counts = {"APPROVE": 0, "HOLD": 0, "REJECT": 0, "UNCHECKED": 0}
    at_risk = cleared = 0.0
    by_state: dict[str, dict] = {}
    for v in vendors:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        val = v["tender"].get("value_inr") or 0
        if v["verdict"] == "REJECT":
            at_risk += val
        elif v["verdict"] == "APPROVE":
            cleared += val
        st = v["state"] or "Unknown"
        s = by_state.setdefault(st, {"state": st, "total": 0, "REJECT": 0, "HOLD": 0, "APPROVE": 0, "UNCHECKED": 0})
        s["total"] += 1
        s[v["verdict"]] += 1
    gst = {}
    for r in DB.execute("select coalesce(status,'unknown') s, count(*) n from gst_registry group by 1"):
        gst[r["s"]] = r["n"]
    recent = [dict(r) for r in DB.execute(
        "select id, vendor_id, vendor_name, gstin, verdict, score, coverage, mode, knockouts, created, reviewed_by"
        " from checks order by id desc limit 8")]
    alerts = []
    for c in DB.execute("select c.* from checks c where c.id in (select max(id) from checks group by vendor_id)"
                        " and c.knockouts > 0 order by c.id desc limit 12"):
        for k in json.loads(c["result"]).get("knockouts", [])[:2]:
            alerts.append({"vendor_id": c["vendor_id"], "vendor_name": c["vendor_name"], "gstin": c["gstin"], "text": k})
    return {"vendors": len(vendors), "registry": DB.execute("select count(*) from gst_registry").fetchone()[0],
            "checks": DB.execute("select count(*) from checks").fetchone()[0], "verdicts": counts,
            "value_at_risk_inr": at_risk, "value_cleared_inr": cleared,
            "by_state": sorted(by_state.values(), key=lambda s: -s["total"]), "gst_status": gst,
            "recent": recent, "alerts": alerts, "sources": engine.sources_status(),
            "uploads": DB.execute("select count(*) from uploads").fetchone()[0]}


def vendor_detail(vid: int) -> dict:
    r = DB.execute("select * from vendors where id=?", (vid,)).fetchone()
    if not r:
        raise KeyError("vendor not found")
    checks = [store.check_row(c) for c in DB.execute("select * from checks where vendor_id=? order by id desc", (vid,))]
    v = store.vendor_row(r)
    return {"vendor": v, "checks": checks,
            "gst": engine.lookup(DB, v["gstin"]) if v.get("gstin") else None}


def add_vendor(body: dict) -> dict:
    bid = store.normalise_vendor(body)
    if not bid:
        raise ValueError("vendor_name or gstin is required")
    if bid.get("gstin"):
        look = engine.lookup(DB, bid["gstin"], refresh=False)
        reg, dec = look["registry"] or {}, look["decode"]
        if not bid.get("vendor_name"):
            bid["vendor_name"] = reg.get("legal_name") or reg.get("trade_name") or bid["gstin"]
        if not dec["valid"]:
            bid.setdefault("_warning", "GSTIN failed validation")
    res = store.upsert_vendor(DB, bid, "manual")
    DB.commit()
    row = store.find_vendor(DB, bid)
    return {"result": res, "id": row["id"] if row else None}


def body_rows(body, content_type: str) -> tuple[str, list]:
    if isinstance(body, dict):
        filename = body.get("filename") or "upload"
        rows = body.get("rows")
        if rows is None:
            rows = store.parse_text(body.get("content") or "", filename)
        return filename, rows
    # raw CSV / JSON text body (curl --data-binary @file.csv)
    return "upload", store.parse_text(body, "x.json" if "json" in content_type else "x.csv")


def public(report: dict) -> dict:
    return {k: v for k, v in report.items() if not k.startswith("_")}


def import_rows(kind: str, rows: list, filename: str, source: str | None = None, dry_run: bool = False) -> dict:
    """Verify every row, then (unless dry_run) import all rows without errors."""
    if kind not in ("vendors", "registry"):
        raise ValueError("kind must be 'vendors' or 'registry'")
    report = verify.verify_rows(DB, kind, rows)
    if dry_run:
        return public(report)
    ins = upd = skip = 0
    errors = [f"row {d['row']}: " + "; ".join(i["text"] for i in d["issues"] if i["level"] == "error")
              for d in report["details"] if d["status"] == "error"]
    for rec, status in report["_records"]:
        if status == "error" or rec is None:
            skip += 1
            continue
        if kind == "registry":
            info = engine.decode(rec["gstin"])
            rec.setdefault("pan", info.get("pan"))
            rec.setdefault("state", info.get("state"))
            r = store.upsert_registry(DB, rec, source or "upload")
        else:
            r = store.upsert_vendor(DB, rec, source or f"upload:{filename}")
        ins += r == "inserted"
        upd += r == "updated"
    DB.execute("insert into uploads(kind,filename,rows_in,inserted,updated,skipped,errors,created) values(?,?,?,?,?,?,?,?)",
               (kind, filename, len(rows), ins, upd, skip, json.dumps(errors[:50]), store.now()))
    DB.commit()
    return {"kind": kind, "filename": filename, "rows": len(rows), "inserted": ins, "updated": upd,
            "skipped": skip, "errors": errors[:20], "counts": report["counts"]}


def do_upload(kind: str, body, content_type: str, dry_run: bool = False) -> dict:
    filename, rows = body_rows(body, content_type)
    return import_rows(kind, rows, filename, dry_run=dry_run)


def add_registry(body: dict, dry_run: bool) -> dict:
    """Manual entry of one GST registry record (verified like any upload)."""
    res = import_rows("registry", [body], "manual entry", source="manual", dry_run=dry_run)
    if not dry_run and res["skipped"]:
        raise ValueError("; ".join(res["errors"]) or "record failed verification")
    return res


# --------------------------------------------------------------------------- connected databases
SRC_DIR = HERE / "sources"
BACKUP_DIR = HERE / "backups"
MAX_FETCH = 25 * 1024 * 1024


def _source(sid: int) -> dict:
    r = DB.execute("select * from sources where id=?", (sid,)).fetchone()
    if not r:
        raise KeyError("database not found")
    d = dict(r)
    d["last_result"] = json.loads(d["last_result"] or "null")
    return d


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_sqlite(path: str) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = []
        for (name,) in con.execute("select name from sqlite_master where type in ('table','view')"
                                   " and name not like 'sqlite_%' order by name"):
            cols = [c[1] for c in con.execute(f"pragma table_info({_qident(name)})")]
            n = con.execute(f"select count(*) from {_qident(name)}").fetchone()[0]
            keys = {store._key(c) for c in cols}
            score = {k: sum(1 for names in a.values() if keys & set(names))
                     for k, a in (("vendors", store.VENDOR_ALIASES), ("registry", store.REGISTRY_ALIASES))}
            # registry tables have status / registration columns; vendor tables have claims / tender columns
            guess = "registry" if score["registry"] >= 3 and not keys & {"tender_id", "claimed_headcount", "domain",
                                                                       "website", "bid_value", "tender_value_inr"} \
                else "vendors"
            out.append({"table": name, "columns": cols, "rows": n, "guess": guess})
        return out
    finally:
        con.close()


def read_sqlite(path: str, table: str) -> list[dict]:
    if table not in {t["table"] for t in inspect_sqlite(path)}:
        raise ValueError(f"table '{table}' not found")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"select * from {_qident(table)} limit 200000")]
    finally:
        con.close()


def fetch_url(url: str) -> list[dict]:
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("link must start with http:// or https://")
    req = urllib.request.Request(url, headers={"accept": "application/json, text/csv, */*",
                                               "user-agent": "gem-vendor-dashboard"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_FETCH + 1)
        ctype = r.headers.get("content-type") or ""
    if len(data) > MAX_FETCH:
        raise ValueError("file at link is larger than 25 MB")
    text = data.decode("utf-8-sig", errors="replace")
    name = "x.json" if "json" in ctype or urlparse(url).path.lower().endswith(".json") else "x.csv"
    return store.parse_text(text, name)


def list_sources() -> list[dict]:
    out = []
    for r in DB.execute("select * from sources order by id desc"):
        d = dict(r)
        d["last_result"] = json.loads(d["last_result"] or "null")
        if d["type"] == "sqlite":
            try:
                d["tables"] = inspect_sqlite(d["file_path"])
            except Exception as e:  # noqa: BLE001
                d["tables"], d["error"] = [], f"cannot open file: {e}"
        d.pop("file_path", None)
        out.append(d)
    return out


def create_source(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    typ = body.get("type")
    kind = body.get("kind") or "vendors"
    if not name:
        raise ValueError("give the database a name")
    if kind not in ("vendors", "registry"):
        raise ValueError("kind must be vendors or registry")
    path = url = table = None
    if typ == "sqlite":
        raw = base64.b64decode(body.get("file_b64") or "")
        if not raw.startswith(b"SQLite format 3\x00"):
            raise ValueError("that file is not a SQLite database (.db / .sqlite)")
        SRC_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", body.get("filename") or "database.db")[-80:]
        path = str(SRC_DIR / f"{int(time.time() * 1000)}_{safe}")
        Path(path).write_bytes(raw)
        try:
            tables = inspect_sqlite(path)
        except sqlite3.DatabaseError as e:
            Path(path).unlink(missing_ok=True)
            raise ValueError(f"database could not be read: {e}") from e
        if not tables:
            Path(path).unlink(missing_ok=True)
            raise ValueError("database has no tables")
        if len(tables) == 1:
            table, kind = tables[0]["table"], tables[0]["guess"]
    elif typ == "url":
        url = (body.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("link must start with http:// or https://")
    else:
        raise ValueError("type must be 'sqlite' or 'url'")
    cur = DB.execute("insert into sources(name,type,kind,url,file_path,table_name,created) values(?,?,?,?,?,?,?)",
                     (name, typ, kind, url, path, table, store.now()))
    DB.commit()
    return {"id": cur.lastrowid}


def update_source(sid: int, body: dict) -> dict:
    s = _source(sid)
    kind = body.get("kind") or s["kind"]
    if kind not in ("vendors", "registry"):
        raise ValueError("kind must be vendors or registry")
    table = body.get("table", s["table_name"])
    if s["type"] == "sqlite" and table and table not in {t["table"] for t in inspect_sqlite(s["file_path"])}:
        raise ValueError(f"table '{table}' not found")
    DB.execute("update sources set kind=?, table_name=?, name=? where id=?",
               (kind, table, (body.get("name") or s["name"]).strip(), sid))
    DB.commit()
    return {"id": sid}


def source_rows(s: dict) -> list[dict]:
    if s["type"] == "sqlite":
        if not s["table_name"]:
            raise ValueError("choose which table to read first")
        return read_sqlite(s["file_path"], s["table_name"])
    return fetch_url(s["url"])


def sync_source(sid: int, dry_run: bool) -> dict:
    s = _source(sid)
    rows = source_rows(s)
    res = import_rows(s["kind"], rows, f"db:{s['name']}" + (f" / {s['table_name']}" if s["table_name"] else ""),
                      source=f"db:{s['name']}" if s["kind"] == "registry" else None, dry_run=dry_run)
    if not dry_run:
        DB.execute("update sources set last_sync=?, last_result=? where id=?",
                   (store.now(), json.dumps({k: res[k] for k in ("rows", "inserted", "updated", "skipped")}), sid))
        DB.commit()
    return res


def delete_source(sid: int) -> dict:
    s = _source(sid)
    if s["file_path"]:
        Path(s["file_path"]).unlink(missing_ok=True)
    DB.execute("delete from sources where id=?", (sid,))
    DB.commit()
    return {"deleted": sid}


# --------------------------------------------------------------------------- backup / restore
def backup_bytes() -> bytes:
    tmp = Path(tempfile.mkdtemp()) / "backup.db"
    dst = sqlite3.connect(str(tmp))
    DB.backup(dst)
    dst.close()
    data = tmp.read_bytes()
    tmp.unlink()
    return data


def restore(body: dict) -> dict:
    raw = base64.b64decode(body.get("file_b64") or "")
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("that file is not a dashboard backup (.db)")
    tmp = Path(tempfile.mkdtemp()) / "restore.db"
    tmp.write_bytes(raw)
    src = sqlite3.connect(str(tmp))
    try:
        if src.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup file is damaged (integrity check failed)")
        names = {r[0] for r in src.execute("select name from sqlite_master where type='table'")}
        missing = {"vendors", "gst_registry", "checks"} - names
        if missing:
            raise ValueError(f"not a dashboard backup: missing tables {', '.join(sorted(missing))}")
        counts = {t: src.execute(f"select count(*) from {t}").fetchone()[0] for t in ("vendors", "gst_registry", "checks")}
        BACKUP_DIR.mkdir(exist_ok=True)
        safety = BACKUP_DIR / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
        safety.write_bytes(backup_bytes())
        src.backup(DB)
    finally:
        src.close()
        tmp.unlink(missing_ok=True)
    DB.executescript(store.SCHEMA)
    DB.commit()
    return {"restored": counts, "previous_data_saved_to": f"dashboard/backups/{safety.name}"}


def seed() -> dict:
    """Load bids/*.json and dashboard/samples/*.csv (demo data)."""
    out = []
    for f in sorted((engine.ROOT / "bids").glob("*.json")):
        out.append(do_upload("vendors", {"filename": f"bids/{f.name}",
                                         "content": f.read_text(encoding="utf-8")}, "application/json"))
    for kind in ("registry", "vendors"):
        f = HERE / "samples" / f"sample_{kind}.csv"
        if f.exists():
            out.append(do_upload(kind, {"filename": f"samples/{f.name}", "content": f.read_text(encoding="utf-8")},
                                 "application/json"))
    return {"loaded": out}


def check_all(only_unchecked: bool) -> dict:
    ids = [r["id"] for r in list_vendors({}) if not only_unchecked or r["verdict"] == "UNCHECKED"]
    results, errors = [], []
    for vid in ids:
        try:
            c = engine.run_check(DB, vid)
            results.append({"vendor_id": vid, "verdict": c["verdict"], "score": c["score"]})
        except Exception as e:  # noqa: BLE001
            errors.append({"vendor_id": vid, "error": str(e)[:200]})
    return {"checked": len(results), "results": results, "errors": errors}


def review(cid: int, body: dict) -> dict:
    c = DB.execute("select * from checks where id=?", (cid,)).fetchone()
    if not c:
        raise KeyError("check not found")
    officer = (body.get("officer") or "").strip()
    if not officer:
        raise ValueError("officer name is required for sign-off")
    verdict = (body.get("verdict") or c["verdict"]).upper()
    if verdict not in ("APPROVE", "HOLD", "REJECT"):
        raise ValueError("verdict must be APPROVE, HOLD or REJECT")
    result = json.loads(c["result"])
    if verdict != c["verdict"]:
        result.setdefault("overrides", []).append({"from": c["verdict"], "to": verdict, "by": officer,
                                                   "note": body.get("note", ""), "at": store.now()})
    result["needs_signoff"] = False
    result["human_confirmed"] = True
    DB.execute("update checks set verdict=?, reviewed_by=?, review_note=?, result=? where id=?",
               (verdict, officer, body.get("note", ""), json.dumps(result), cid))
    DB.commit()
    return store.check_row(DB.execute("select * from checks where id=?", (cid,)).fetchone())


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vendor_name", "gstin", "state", "domain", "tender_id", "tender_value_inr", "gst_status",
                "verdict", "score", "coverage", "knockouts", "mode", "checked_at", "reviewed_by"])
    for v in list_vendors({}):
        w.writerow([v["vendor_name"], v["gstin"], v["state"], v["domain"], v["tender"].get("id"),
                    v["tender"].get("value_inr"), v["gst_status"], v["verdict"], v["score"], v["coverage"],
                    v["knockouts"], v["mode"], v["checked_at"], v["reviewed_by"]])
    return buf.getvalue()


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "GeMDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send(self, code: int, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
        self.send_response(code)
        self.send_header("content-type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("upload is larger than 80 MB")
        raw = self.rfile.read(n).decode("utf-8-sig") if n else ""
        if "json" in (self.headers.get("content-type") or ""):
            return json.loads(raw or "{}")
        return raw

    def _route(self, method: str):
        u = urlparse(self.path)
        p, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        if method == "GET" and p in ("/", "/index.html"):
            return self._send(200, (HERE / "static" / "index.html").read_bytes(), "text/html")
        if not p.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        with LOCK:
            if method == "GET":
                if p == "/api/stats":
                    return self._send(200, stats())
                if p == "/api/vendors":
                    return self._send(200, list_vendors(q))
                if m := re.fullmatch(r"/api/vendors/(\d+)", p):
                    return self._send(200, vendor_detail(int(m[1])))
                if m := re.fullmatch(r"/api/gstin/([0-9A-Za-z ]+)", p):
                    return self._send(200, engine.lookup(DB, m[1], refresh=bool(q.get("refresh"))))
                if p == "/api/registry":
                    t = f"%{(q.get('q') or [''])[0]}%"
                    return self._send(200, [dict(r) | {"raw": None} for r in DB.execute(
                        "select * from gst_registry where gstin like ? or legal_name like ? or trade_name like ?"
                        " order by updated desc limit 500", (t, t, t))])
                if p == "/api/uploads":
                    return self._send(200, [dict(r) | {"errors": json.loads(r["errors"] or "[]")} for r in
                                            DB.execute("select * from uploads order by id desc limit 50")])
                if p == "/api/export.csv":
                    return self._send(200, export_csv(), "text/csv",
                                      {"content-disposition": "attachment; filename=gem_vendor_checks.csv"})
                if p == "/api/sources":
                    return self._send(200, list_sources())
                if p == "/api/db/backup":
                    return self._send(200, backup_bytes(), "application/octet-stream",
                                      {"content-disposition": f"attachment; filename=gem-dashboard-backup-"
                                                              f"{time.strftime('%Y%m%d-%H%M')}.db"})
                if m := re.fullmatch(r"/api/template/(vendors|registry)\.csv", p):
                    return self._send(200, ",".join(TEMPLATES[m[1]]) + "\n", "text/csv",
                                      {"content-disposition": f"attachment; filename={m[1]}_template.csv"})
            if method == "POST":
                body = self._body()
                if p == "/api/vendors":
                    return self._send(200, add_vendor(body))
                dry = bool(q.get("dry_run"))
                if p == "/api/upload":
                    return self._send(200, do_upload((q.get("kind") or [""])[0], body,
                                                     self.headers.get("content-type") or "", dry_run=dry))
                if p == "/api/registry":
                    return self._send(200, add_registry(body, dry))
                if p == "/api/sources":
                    return self._send(200, create_source(body))
                if m := re.fullmatch(r"/api/sources/(\d+)", p):
                    return self._send(200, update_source(int(m[1]), body))
                if m := re.fullmatch(r"/api/sources/(\d+)/(verify|sync)", p):
                    return self._send(200, sync_source(int(m[1]), dry_run=m[2] == "verify"))
                if p == "/api/db/restore":
                    return self._send(200, restore(body))
                if p == "/api/seed":
                    return self._send(200, seed())
                if p == "/api/check-all":
                    return self._send(200, check_all(bool(q.get("unchecked"))))
                if m := re.fullmatch(r"/api/check/(\d+)", p):
                    return self._send(200, engine.run_check(DB, int(m[1]), refresh_lookup=bool(q.get("refresh"))))
                if m := re.fullmatch(r"/api/checks/(\d+)/review", p):
                    return self._send(200, review(int(m[1]), body))
            if method == "DELETE":
                if m := re.fullmatch(r"/api/sources/(\d+)", p):
                    return self._send(200, delete_source(int(m[1])))
                if m := re.fullmatch(r"/api/vendors/(\d+)", p):
                    DB.execute("delete from checks where vendor_id=?", (int(m[1]),))
                    DB.execute("delete from vendors where id=?", (int(m[1]),))
                    DB.commit()
                    return self._send(200, {"deleted": int(m[1])})
        return self._send(404, {"error": f"no route {method} {p}"})

    def _authorised(self) -> bool:
        if not PASSWORD:
            return True
        h = self.headers.get("authorization") or ""
        if h.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                return hmac.compare_digest(pw, PASSWORD) and (not USER or hmac.compare_digest(user, USER))
            except (ValueError, UnicodeDecodeError):
                return False
        return False

    def _safe(self, method):
        if not self._authorised():
            self.send_response(401)
            self.send_header("www-authenticate", 'Basic realm="GeM vendor dashboard", charset="UTF-8"')
            self.send_header("content-length", "0")
            self.end_headers()
            return
        try:
            self._route(method)
        except KeyError as e:
            self._send(404, {"error": str(e).strip("'")})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def do_DELETE(self):
        self._safe("DELETE")


def main():
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "8765")
    host = os.environ.get("DASHBOARD_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    if host != "127.0.0.1" and not PASSWORD:
        print("WARNING: listening publicly with no DASHBOARD_PASSWORD set - anyone with the URL has full access")
    srv = ThreadingHTTPServer((host, port), Handler)
    src = engine.sources_status()
    print(f"GeM vendor dashboard on http://{host}:{port}  (login {'on' if PASSWORD else 'off'})")
    print(f"  GSP API: {'on' if src['gsp_api'] else 'off (set GSP_API_URL)'}   "
          f"Crustdata: {'on' if src['crustdata'] else 'off (set CRUSTDATA_API_KEY)'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
