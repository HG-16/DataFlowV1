# GeM Bid Checker

**ContextCon 05 · Theme: "Services, replaced" · Problem statement 5.1 (Vendor Check)**

> Checks an IT vendor's GeM bid **before the government pays**, and catches the inflated, lookalike and
> shell-company bids that CAG audits later report as "unfruitful expenditure".

---

## The service

Government departments buy IT services on GeM (Government e-Marketplace). CAG audits keep finding money lost on
IT projects that should never have been awarded, for example NIC's ₹14.25 cr digital mapping and ₹12.10 cr of
obsolete hardware and software. Much of it starts with the wrong vendor.

Today the vendor diligence is either **skipped** (no time) or **outsourced** to consultants (slow and costly).
This project replaces that service. An agent takes the bid, checks every claim against independent data, and
returns a verdict a procurement officer can act on: **APPROVE / HOLD / REJECT**. Every finding cites its source.

The government-facing side is a **dashboard keyed on GSTIN**, the one identifier every GeM seller must have.

## What it does now

**Checks seven categories and combines them into one score:**

| Category | Weight | What is checked | Data source |
|---|---|---|---|
| Identity | 15 | Domain really belongs to the named firm; **lookalike domains**; CIN/LLPIN and Udyam format; listed-company match | Crustdata `/company/identify`, EODHD search, MCA formats |
| Capacity | 15 | Claimed vs observed headcount, founding year, open hiring, **shell signals** (₹ per employee), tender's minimum-experience rule | Crustdata `/company/enrich`, `/job/search` |
| GST | 20 | GSTIN format and **check digit**; PAN, state and entity type inside the GSTIN; **status** (active / cancelled / suspended); **registered to someone else?**; GSTR-3B filing regularity; registration age | GSTIN structure, GST API (GSTN taxpayer record), uploaded GST registry |
| Financials | 15 | Claimed turnover vs audited revenue, losses, **negative net worth**, tender's minimum-turnover rule | EODHD fundamentals (listed firms), Crustdata revenue band (estimate) |
| Firm legal | 15 | Pending cases, **insolvency (NCLT/IBC)**, debarment, fraud/FIR/ED/CBI, cases the officer already found | Indian Kanoon + news via Crustdata web search |
| Key people | 10 | Each named person **currently** works at the vendor (not left, not a namesake) | Crustdata `/person/search` |
| People legal | 10 | Cases, fraud or director disqualification naming the person **and** the firm | Crustdata web search |

**Hard knockouts force REJECT whatever the score:** a lookalike domain; an invalid GSTIN; the GSTIN's PAN differs
from the declared PAN; GST cancelled or suspended; the GSTIN is registered to a different firm; an invalid CIN; negative net
worth; failing the tender's turnover or experience rule; debarment; a criminal/fraud record against the firm or a
key person; a disqualified director. On the dashboard, also **cover bidding**: one PAN behind differently named
bidders on the same tender.

**Results on real data** (Crustdata API responses, 22 Nov 2026, saved in `fixtures/ksolves_live.json` and
replayed offline):

| Bid | What the agent found | Verdict |
|---|---|---|
| `bids/ksolves.json`: genuine claims | ksolves.com owned by Ksolves India Ltd; listed on NSE; 866 staff observed vs 600 claimed; founded 2012 ✓; 13 open jobs; CEO and CTO confirmed in post; no cases found | **APPROVE 78.8** (coverage 65%: no GSTIN supplied, no audited financials on the free EODHD plan) |
| `bids/ksolves_lookalike.json`: fake claims | **ksolves.co is not a Ksolves domain** (knockout); 2,500 staff claimed vs 866; founded 2005 claimed vs 2012; "Arjun Malhotra" matches 4 people, none at Ksolves | **REJECT** |

**Two ways to use it:**

- **Officer dashboard** (`server.py`): for a department checking many bidders.
  - **GSTIN lookup:** validates and decodes the GSTIN, searches automatically, and shows every bid under that GSTIN and every other GSTIN on the same PAN.
  - **Vendor list** with verdicts, batch checks and CSV export.
  - **Officer sign-off:** a REJECT needs a named officer, and overriding the system verdict needs a written note.
  - **Upload Data:** CSV / Excel / JSON.
  - **Connected databases:** a SQLite file or a CSV/JSON link, re-synced on demand.
  - **Verification before every import:** bad rows are blocked, suspicious rows are flagged.
  - **Backup and restore**, and an optional password login.
- **Single-bid form** (`app.py`, Streamlit), or the **command line** (`gem_checker.py`): one bid in, a full report and `report.json` out.

## What comes next

- **Live GST status for every GSTIN.** Plug in a paid GSP (GST Suvidha Provider) account. The code already accepts the standard GSTN taxpayer JSON. Today the free route is the uploaded registry.
- **Audited financials for unlisted firms.** Use MCA21 filings (AOC-4) by CIN; today only listed firms get audited numbers.
- **Court records beyond Indian Kanoon.** Add eCourts / NJDG district-court data, once access exists.
- **Read the bid straight from GeM.** Take the seller profile and bid document from the GeM bid number instead of typing claims in.
- **One configuration.** The dashboard uses `GSP_API_URL` and the checker uses `GSTINCLOUD_URL` for the same job; merge them.
- **Department deployment.** Officer accounts instead of one shared password, an audit log of every sign-off, and Postgres instead of SQLite.

## How it runs

```
            bid (form / JSON / CSV / connected DB)
                          │
                 verify.py: row checks ── blocked rows stop here
                          │
   ┌──────────── gem_checker.py: seven category checks ────────────┐
   │ Crustdata (company, people, jobs, web)   providers.py:        │
   │ budgeted · cached in SQLite · replayable  GST API, EODHD      │
   └───────────────────────────┬───────────────────────────────────┘
              weighted score + knockouts → APPROVE / HOLD / REJECT
                          │
          engine.py: GST registry + cross-bid checks (shared PAN, reused GSTIN)
                          │
        server.py + index.html: dashboard     app.py: Streamlit form
                          │
                 store.py: SQLite (vendors, registry, checks, sign-offs)
```

**Run it:**

```bash
pip install -r requirements.txt streamlit

# Officer dashboard: http://127.0.0.1:8765 → "Load demo data" → "Run checks on all vendors"
python server.py

# Single-bid form (offline demo on recorded Ksolves data is ticked by default)
streamlit run app.py

# Command line, offline on real recorded data: no API key, no credits
python gem_checker.py bids/ksolves.json --replay fixtures/ksolves_live.json --no-prompt --yes
python gem_checker.py bids/ksolves_lookalike.json --replay fixtures/ksolves_live.json --no-prompt --yes

# Tests: 25, offline, including the real-data regressions
python -m pytest -q tests
```

**Keys (all optional; without them that category reports "can't verify" and the score treats it as unknown):**

| Variable | Used for |
|---|---|
| `CRUSTDATA_API_KEY` | Company, people, jobs and web/court search |
| `GSTINCLOUD_API_KEY`, `GSTINCLOUD_URL`, `GSTINCLOUD_AUTH` | GST taxpayer record in the checker (`{gstin}` in the URL) |
| `GSP_API_URL`, `GSP_API_KEY` | GST taxpayer record for dashboard lookups |
| `EODHD_API_KEY` | Listed-company search and audited financials |
| `DASHBOARD_PASSWORD`, `DASHBOARD_DB`, `PORT` | Dashboard login, database location, hosting port |

**How it scores:**

```
s_c       = mean(finding scores in category c), capped at its worst red flag     s_c ∈ [0, 1]
s_c       = 0.5 if category c has no evidence                                    neutral prior
Score     = 100 × Σ w_c·s_c / Σ w_c                                              Σ w_c = 100
Coverage  = Σ w_c (categories with evidence) / Σ w_c

REJECT   any knockout, or Score < 50          (needs human sign-off)
APPROVE  Score ≥ 75 and Coverage ≥ 60%
HOLD     everything else
```

**Safeguards:** at most 40 Crustdata credits and 180 s per check; a SQLite response cache, so re-runs are free; a
credit ledger; retries with backoff on rate limits; API keys stripped from cache keys and fixtures; REJECT needs a
human to confirm it.

## What you tried

- **Fuzzy domain matching gave false identities.** Crustdata's `/company/identify` resolves `ksolves.co` to
  Ksolves at confidence 1.0. Now the bid domain must literally appear in the matched firm's domain list, so the
  lookalike is caught (covered by a regression test).
- **Court searches returned noise.** Searches mostly return generic NCLT cause lists that don't name the vendor.
  A hit now counts only if its snippet names the subject in whole words, and for a person names the firm too
  (no namesakes). A legal knockout needs a court record or two independent hits.
- **The GST portal search has a CAPTCHA**, so it can't be automated. We tried public web pages that mirror GST
  records (weak and patchy), then moved to a GST API provider plus a **department-uploaded GST registry** as the
  source of truth.
- **Financials:** EODHD's free plan confirms the listed entity but not audited numbers (paid plan). Crustdata's
  revenue is a wide model estimate, so it can lower a score but **never** knock a bid out.
- **eCourts / NJDG has no open API.** We use Indian Kanoon and news through Crustdata web search, and label
  "no cases found" as such instead of treating it as clean (it scores 0.85, not 1.0).
- **A free text box wasn't enough for officers.** We added structured intake (claims, tender rules, cases already
  found), then a dashboard for checking many bidders at once with manual upload, since most departments have
  spreadsheets, not APIs.

## Why this way

- **GSTIN as the key.** Every GeM seller must have one. It contains the PAN and the state and has a check digit,
  so a lot can be verified before any API call. A shared PAN across "different" bidders is how cover bidding shows up.
- **Weighted score plus hard knockouts, not one or the other.** Some facts end the discussion (cancelled GST,
  lookalike domain, debarment) and no amount of good evidence should outweigh them. Everything else is a matter
  of degree.
- **Unknown is not the same as clean.** A missing category scores a neutral 0.5 and lowers **coverage**. A
  vendor can't reach APPROVE on thin evidence (coverage under 60% → HOLD).
- **Every finding cites its source**, with the passage and date, so an officer or an auditor can check it.
  An unexplained AI verdict would not hold up in a procurement file.
- **A human signs every REJECT.** The agent prepares the decision; the officer makes it, with their name and note on record.
- **Works offline and on standard government machines.** The dashboard needs only the Python standard library.
  Recorded real responses let the whole system be demoed and tested with no keys and no credits. Department
  data can be uploaded by hand when no API access exists.
- **Verification before import.** Bad data is blocked at the door, but an invalid GSTIN on a *bid* is imported
  and flagged on purpose, so the fraud shows up as a REJECT instead of silently disappearing.

---

*Test bids in `bids/` other than Ksolves, and the files `sample_vendors.csv` / `sample_registry.csv`, are
synthetic: the firms and people are fictional. Code written with an AI coding assistant (Claude) during the hackathon.*
