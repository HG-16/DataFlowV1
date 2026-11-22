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
| GST Registry | Every GSTIN known, with where the record came from (upload, GSP API, web search) |

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
POST /api/upload?kind=vendors|registry   (JSON {filename, rows|content} or a raw CSV body)
POST /api/vendors   POST /api/check/{id}   POST /api/check-all[?unchecked=1]
POST /api/checks/{id}/review {officer, verdict, note}
GET  /api/export.csv   GET /api/template/{vendors|registry}.csv
```

e.g. `curl -X POST "http://127.0.0.1:8765/api/upload?kind=registry" -H "content-type: text/csv" --data-binary @gst.csv`

Data is stored in `dashboard/dashboard.db` (SQLite). The sample files in `dashboard/samples/` are fictional firms.
`rapidfuzz` is used when installed; otherwise a built-in difflib fallback is used.
