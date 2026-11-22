# Government dashboard

A web dashboard for procurement officers, built on `gem_checker.py`. Everything is keyed on **GSTIN**.

```bash
python dashboard/server.py          # http://127.0.0.1:8765   (standard library only)
```

Click **Load demo data** and then **Run checks on all vendors** to see it working.

## Pages

| Page | What it does |
|---|---|
| Overview | KPIs (rejected / hold / approved, tender value stopped), verdict and state charts, knockout alerts, recent checks |
| GSTIN Lookup | Type a GSTIN and it is validated (format + check digit, with the correct digit shown when wrong), decoded (state, PAN, entity type), searched automatically in the registry and APIs, and linked to every bid under it and every other GSTIN on the same PAN |
| Vendors | All bidders with their latest verdict. Search, filter, check one or all, add a vendor by GSTIN only (name auto-filled), export CSV |
| Vendor detail | Score, category bars, every finding with its source, knockouts, check history, and **officer sign-off**: a REJECT needs a named officer, and overriding the system verdict needs a note |
| Upload Data | Manual upload of the **vendor/bid database** and the **GST registry** as CSV, Excel or JSON, with a preview before import. Column names are matched loosely (`GST No`, `GSTN`, `gstin`…). GSTN API JSON (`lgnm`, `sts`, `rgdt`, `ctb`, `pradr`) is accepted as-is. Templates are downloadable |
| Databases | **Connect a database**: a SQLite file (pick the table; the page guesses whether it holds vendors or GST registry data) or an online CSV/JSON link. **Verify & import** whenever it changes. **Backup & restore** the whole dashboard database |
| GST Registry | Every GSTIN known, with where it came from (upload, connected database, manual entry, GSP API, web search). **Add record manually** to type in or correct one taxpayer record |

## Verification

Every import is verified row by row first: file uploads, connected databases, manual registry entries and the Add vendor form. You see a report before anything is saved.

| Level | Meaning | Examples |
|---|---|---|
| Blocked (error) | Never imported | Registry GSTIN with a wrong check digit; text in a number column; impossible or future dates; GSTR-3B over 12; a row with no name and no GSTIN |
| Warning | Imported and flagged | Bid GSTIN invalid (the vendor check will reject it); declared PAN or state doesn't match the GSTIN; bidder name differs from the GST registry; registration cancelled; no GSTIN; duplicate row in the file |
| Info | Imported | Status change from the previous record; missing website |

Invalid GSTINs on *bids* are imported as warnings on purpose, so that the fraud shows up as a REJECT instead of silently vanishing.

Restoring a backup first saves the current data to `dashboard/backups/before-restore-*.db`. A backup must contain the
dashboard's tables and pass SQLite's integrity check, or it is refused. MySQL, PostgreSQL and Oracle aren't connected
directly: export the table to CSV and upload it, or publish it as a CSV/JSON link.

## Automatic GSTIN search

Each lookup or check tries these sources in order and saves what it finds into the registry:

1. **Uploaded GST registry**. Always on.
2. **GSP / GST API**: set `GSP_API_URL` (e.g. `https://your-gsp/taxpayer/{gstin}`), `GSP_API_KEY` and optionally `GSP_AUTH_HEADER` (default `Authorization: Bearer …`).
3. **Public web search** through Crustdata: set `CRUSTDATA_API_KEY`.

API results are reused for 24 h; **Re-query APIs** forces a fresh call.

## Check modes

| Mode | When | Coverage |
|---|---|---|
| `live` | `CRUSTDATA_API_KEY` set | all six categories |
| `replay` | no key, but `fixtures/` has recorded responses for the vendor's domain (Ksolves) | all six |
| `gst_only` | no key | GST + declared data (~40%), so clean vendors land on HOLD for officer sign-off |

On top of `gem_checker.py`'s checks, the dashboard adds cross-checks from its own database:

- **GST legal name vs bidder name** (from the registry)
- **GSTIN reused** by differently named bidders
- **Shared PAN**: the same PAN behind differently named bidders. If they bid on the same tender, it's a knockout (cover bidding)

## Deploy

Settings (environment variables):

| Variable | Purpose |
|---|---|
| `PORT` | Set by most hosts. When present the server listens on `0.0.0.0` |
| `DASHBOARD_PASSWORD` | **Required when public.** Turns on a browser login (any username unless `DASHBOARD_USER` is set) |
| `DASHBOARD_USER` | Optional fixed username |
| `DASHBOARD_DB` | Path of the SQLite file. Point it at a persistent disk, or data is lost on restart |
| `CRUSTDATA_API_KEY`, `GSP_API_URL`, `GSP_API_KEY` | Optional data sources (see above) |

**Render.com** (or Railway, similar): New → Web Service → connect the GitHub repo.
Build command `pip install -r requirements.txt`, start command `python dashboard/server.py`.
Add `DASHBOARD_PASSWORD`. Attach a persistent disk at `/data` and set `DASHBOARD_DB=/data/dashboard.db`;
without a disk, uploads and checks reset whenever the service restarts.

**Own server / government VM**: `git clone` the repo, `pip install -r requirements.txt`, then run
`DASHBOARD_PASSWORD=... DASHBOARD_HOST=0.0.0.0 python dashboard/server.py` under a process manager
(systemd, or NSSM on Windows) behind nginx/IIS with HTTPS. Basic-auth passwords travel in every request,
so serve it over HTTPS only.

## API (for scripts)

```
GET  /api/stats                     GET  /api/vendors?q=&verdict=
GET  /api/gstin/{GSTIN}[?refresh=1] GET  /api/vendors/{id}
POST /api/upload?kind=vendors|registry[&dry_run=1]   (JSON {filename, rows|content} or a raw CSV body)
POST /api/registry[?dry_run=1]      manual registry record
GET/POST /api/sources               connected databases; POST /api/sources/{id} {table, kind}
POST /api/sources/{id}/verify|sync  DELETE /api/sources/{id}
GET  /api/db/backup                 POST /api/db/restore {file_b64}
POST /api/vendors   POST /api/check/{id}   POST /api/check-all[?unchecked=1]
POST /api/checks/{id}/review {officer, verdict, note}
GET  /api/export.csv   GET /api/template/{vendors|registry}.csv
```

e.g. `curl -X POST "http://127.0.0.1:8765/api/upload?kind=registry" -H "content-type: text/csv" --data-binary @gst.csv`

Data is stored in `dashboard/dashboard.db` (SQLite). The sample files in `dashboard/samples/` are fictional firms.
`rapidfuzz` is used when installed; otherwise a built-in difflib fallback is used.
