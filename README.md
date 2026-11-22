# GeM Bid Checker

**ContextCon 05 · Theme: "Services, replaced" · Problem statement 5.1 (Vendor Check)**

Government departments buy IT services on GeM, and CAG audits keep reporting "unfruitful
expenditure" on IT projects (e.g. NIC: ₹14.25 cr digital mapping, ₹12.10 cr obsolete hardware/software).
Much of it starts with picking the wrong vendor. Today that diligence is either skipped or
outsourced to consultants. This agent checks an IT vendor's bid claims **before the money is paid**.

> It catches inflated, lookalike and body-shop bids, the kind CAG later reports as unfruitful expenditure.

## What it checks

| # | Category (weight) | Checks | Data |
|---|---|---|---|
| 1 | Identity (15) | Domain belongs to the named firm; **lookalike domain** detection | Crustdata `/company/identify` |
| 2 | Capacity (20) | Claimed vs observed headcount (50% slack), founding year, **shell signals** (₹ tender or turnover per employee), open hiring | `/company/enrich`, `/job/search` |
| 3 | GST (20) | GSTIN format + **checksum**, embedded PAN = declared PAN, state code = declared state, PAN entity type = declared entity, **status** (active / cancelled / suspended), GSTR-3B filing regularity, registration age | GSTIN structure, web lookup, GST portal values |
| 4 | Firm legal (20) | Pending cases, **insolvency (NCLT/IBC)**, debarment/blacklisting, fraud/FIR/ED/CBI, CAG and regulatory hits, declared cases | `/web/search/live` on indiankanoon.org + news |
| 5 | Key people (15) | Each named person **currently** works at the vendor (vs left / namesake / not found) | `/person/search` |
| 6 | Key people legal (10) | Cases, fraud, director disqualification naming each person *together with* the firm | `/web/search/live` |

Every finding records `category, check, status (confirmed | contradicted | cant_verify | red_flag),
score, detail, source, passage, date, knockout`.

## Weighted formula

```
s_c       = mean(finding scores in category c), capped at its worst red flag   (s_c ∈ [0,1])
s_c       = 0.5 if category c has no evidence                                   (neutral prior)
Score     = 100 × Σ w_c·s_c / Σ w_c                                             (Σ w_c = 100)
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

- **GST status and filings:** there's no free official API. The GST portal search has a CAPTCHA. The tool uses public web pages and values the reviewer enters from `services.gst.gov.in`. A GSP API (Masters India, ClearTax, Karza, etc.) can be plugged into `check_gst`.
- **Court cases:** eCourts/NJDG has no open API. The tool searches Indian Kanoon and news through Crustdata web search, so unindexed district-court matters can be missed. The "no cases" result is labelled as such.
- **Company data:** MCA financials and director DINs aren't in Crustdata. Director disqualification is detected only through public mentions.
- **Response formats:** checked against the live API (identify, enrich, person search, job search, web search). The script calls the REST API directly with the same request bodies the MCP connector uses.
- **Test bids:** the files in `bids/` are synthetic. The firm and people are fictional.

## AI assistance

This code was written with an AI coding assistant (Claude) during the hackathon.
