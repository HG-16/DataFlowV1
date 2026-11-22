# GeM Bid Checker

**ContextCon 05 · Theme: "Services, replaced" · Problem statement 5.1 (Vendor Check)**

Government departments buy IT services on GeM, and CAG audits keep reporting "unfruitful
expenditure" on IT projects (e.g. NIC: ₹14.25 cr digital mapping, ₹12.10 cr obsolete hardware/software).
Much of it starts with picking the wrong vendor. Today that diligence is either skipped or
outsourced to consultants. This agent checks an IT vendor's bid claims **before the money is paid**.

> It catches inflated, lookalike and body-shop bids, the kind CAG later reports as unfruitful expenditure.

## Using it

**Web form (recommended):** `pip install -r requirements.txt && streamlit run app.py`

The form asks for what a GeM seller profile and bid contain:
- **Seller:** legal name, website, entity type, GSTIN, PAN, CIN/LLPIN, Udyam number, state, founding year, stock ticker
- **Claims:** headcount, turnover for the last 3 financial years, net worth
- **Tender:** bid number, value, minimum average turnover, minimum years of experience
- **Key personnel**
- **GST details you already checked:** optional, since the GST in Cloud API fills these
- **Cases you already found**

The agent scores the cases you list **and** searches court records and news itself. Anything you missed is marked `FOUND BY AGENT`. Tick **Offline demo** in the sidebar to run on the recorded real Ksolves data with no keys.

**API keys:** set them in the sidebar or as environment variables.

| Variable | Used for |
|---|---|
| `CRUSTDATA_API_KEY` | Company, people, jobs, web/court search |
| `GSTINCLOUD_API_KEY`, `GSTINCLOUD_URL` (template with `{gstin}`), `GSTINCLOUD_AUTH` (`header:Name` or `query:param`) | Live GSTIN status, legal name, constitution, registration date, GSTR-3B filing history |
| `EODHD_API_KEY` | Audited revenue, net income and net worth for listed firms. The ticker is found automatically from Crustdata's stock symbol or EODHD search |

If a key is missing, that category is marked "can't verify" and scored neutral. Nothing crashes.

## What it checks

| # | Category (weight) | Checks | Data |
|---|---|---|---|
| 1 | Identity (15) | Domain belongs to the named firm; **lookalike domain** detection; CIN format, incorporation year and listed status vs claims; Udyam format | Crustdata `/company/identify`, MCA CIN structure |
| 2 | Capacity (15) | Claimed vs observed headcount (50% slack), founding year, **shell signals** (₹ tender or turnover per employee), open hiring | `/company/enrich`, `/job/search` |
| 3 | GST (20) | GSTIN format + **checksum**, embedded PAN = declared PAN, state code = declared state, PAN entity type = declared entity; **live status** (active / cancelled / suspended, with cancellation date), **legal name on the GSTIN = vendor** (else someone else's GSTIN → knockout), constitution, **GSTR-3B filed in last 12 months**, registration age | GSTIN structure, **GST in Cloud API**, declared values as fallback |
| 3b | Financials (15) | Claimed turnover vs **audited revenue** (>2× = red flag), profit/loss trend, **net worth** (negative = knockout), **tender rule: avg turnover of last 3 FY ≥ required minimum** (fail = knockout), tender value vs revenue; unlisted firms: Crustdata revenue estimate (weak) + declared figures | **EODHD fundamentals**, Crustdata `revenue` |
| 4 | Firm legal (15) | **Cases you entered** (active criminal/fraud/debarment = knockout) plus the agent's own search for pending cases, **insolvency (NCLT/IBC)**, debarment/blacklisting, fraud/FIR/ED/CBI, CAG and regulatory hits, reported as `FOUND BY AGENT` | `/web/search/live` on indiankanoon.org + news |
| 5 | Key people (10) | Each named person **currently** works at the vendor (vs left / namesake / not found) | `/person/search` |
| 6 | Key people legal (10) | Cases, fraud, director disqualification naming each person *together with* the firm | `/web/search/live` |

Every finding records `category, check, status (confirmed | contradicted | cant_verify | red_flag),
score, detail, source, passage, date, knockout`.

## Weighted formula

```
s_c       = mean(finding scores in category c), capped at its worst red flag   (s_c ∈ [0,1])
s_c       = 0.5 if category c has no evidence                                   (neutral prior)
Score     = 100 × Σ w_c·s_c / Σ w_c                                             (Σ w_c = 100: identity 15, capacity 15, gst 20, financials 15, firm_legal 15, people 10, people_legal 10)
Coverage  = Σ w_c (categories with evidence) / Σ w_c
```

Knockouts force **REJECT** whatever the score:
- lookalike domain
- invalid GSTIN
- GSTIN's PAN differs from the declared PAN
- GST cancelled or suspended
- debarment or blacklisting
- a criminal or fraud record against the firm or a key person
- a disqualified director

A legal knockout needs a court record (Indian Kanoon or eCourts) or two independent hits. A snippet that doesn't name the person is ignored, which protects against namesakes.

| Verdict | Rule |
|---|---|
| REJECT | any knockout, or Score < 50 (needs human sign-off; `--yes` skips this for demos) |
| APPROVE | Score ≥ 75 **and** Coverage ≥ 60% |
| HOLD | everything else |

Scoring notes:
- Finding no case records scores 0.85, not 1.0, because a web search can miss cases.
- Shell-company thresholds: over ₹2.5 cr of tender value per observed employee, or over ₹1.5 cr of claimed turnover per employee. The IT-services norm is about ₹10–40 lakh revenue per employee.

## Live results (real Crustdata data, 22 Nov 2026)

Run against **Ksolves India Limited** (NSE-listed IT services firm). Every response came from the live
Crustdata API through the Crustdata MCP connector and is saved in `fixtures/ksolves_live.json`.

| Bid | What the agent found | Verdict |
|---|---|---|
| `bids/ksolves.json`: genuine claims | ksolves.com is owned by Ksolves; 866 staff observed vs 600 claimed; founded 2012 ✓; 13 job listings; CEO Ratan Srivastava and CTO Manish Gurnani confirmed in post; no cases naming the firm or either person | **APPROVE 85.5** (GST not supplied, coverage 80%) |
| `bids/ksolves_lookalike.json`: fake claims | **ksolves.co is not a Ksolves domain** (knockout); 2500 staff claimed vs 866 observed; founded 2005 claimed vs 2012; "Arjun Malhotra" matches 4 people, none at Ksolves | **REJECT** |

Two problems the live data exposed, both now fixed and covered by tests:
1. `/company/identify` matches domains fuzzily. It resolves `ksolves.co` to Ksolves at confidence 1.0, so the bid domain must literally appear in the matched firm's `all_domains`.
2. Court and news searches mostly return generic NCLT cause lists that don't name the vendor. A hit counts only when its snippet names the subject in whole words (and also names the firm, for a person).

## Run

```bash
pip install httpx rapidfuzz
export CRUSTDATA_API_KEY=...
python gem_checker.py bids/bid_lookalike.json          # asks for missing GSTIN/PAN/people etc.
python gem_checker.py bids/bid_honest.json --no-prompt --yes
python gem_checker.py bids/ksolves.json --replay fixtures/ksolves_live.json --no-prompt --yes   # real data, offline
python -m pytest -q tests                              # offline, no credits (incl. real-data regressions)
```

Bid JSON fields:
- Required: `vendor_name`, `domain`
- Optional: `claimed_headcount`, `claimed_year_founded`, `claimed_turnover_inr`, `gstin`, `pan`, `state`, `entity_type`, `gst_status`, `gstr3b_filed_last_12`, `gst_registration_date`, `declared_pending_cases`, `key_personnel[{name,title}]`, `tender{id,title,value_inr}`

Output: a console report and `report.json`.

Safeguards:
- At most 40 credits and 180 s per run
- SQLite response cache, so re-runs are free
- Credit ledger
- Retries with backoff on rate limits
- REJECT needs a human to confirm it

## Known limits

- **GST in Cloud:** responses are normalised from GSTN's standard field names (`sts`, `lgnm`, `tradeNam`, `rgdt`, `cxdt`, `ctb`, filing list). The exact endpoint, auth and response format must be confirmed against the provider's docs when the key is added. Without a key, the tool falls back to public web pages and values the reviewer enters.
- **Financials:** EODHD only covers listed companies. For unlisted firms, only Crustdata's revenue band (very wide: Ksolves shows $100M–$500M) and declared figures are available, so the tool asks for a CA-certified turnover statement.
- **Court cases:** eCourts/NJDG has no open API. The tool searches Indian Kanoon and news through Crustdata web search, so unindexed district-court matters can be missed. The "no cases" result is labelled as such.
- **Company data:** MCA financials and director DINs aren't in Crustdata. Director disqualification is detected only through public mentions.
- **Response formats:** checked against the live API (identify, enrich, person search, job search, web search). The script calls the REST API directly with the same request bodies the MCP connector uses.
- **Test bids:** the files in `bids/` are synthetic. The firm and people are fictional.

## AI assistance

This code was written with an AI coding assistant (Claude) during the hackathon.
