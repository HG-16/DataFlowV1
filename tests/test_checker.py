"""Offline tests: a fake Crustdata client returns canned responses, so the scoring logic,
GST checks and knockout rules are exercised without network or credits.
The canned shapes are guesses at the live API; the parser (`dig`/`records`) is shape-tolerant."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import gem_checker as g  # noqa: E402

BIDS = pathlib.Path(__file__).resolve().parents[1] / "bids"
# Shapes below mirror real /2025-11-01 responses captured from the Crustdata API (Ksolves, 22 Nov 2026).
BI = {"name": "Nimbus Cloudworks Private Limited", "crustdata_company_id": 1, "primary_domain": "nimbuscloudworks.in",
      "all_domains": ["nimbuscloudworks.in"], "year_founded": 2016,
      "professional_network_url": "https://www.linkedin.com/company/nimbus"}


def person(name, cur, past=None, as_enrich=False):
    key = "employer_name" if as_enrich else "name"      # enrich.people uses employer_name; search uses name
    return {"basic_profile": {"name": name},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://linkedin.com/in/{name.split()[0].lower()}"}},
            "experience": {"employment_details": {"current": [{key: cur, "title": "Lead"}],
                                                  "past": [{key: p, "end_date": "2024-01-01T00:00:00"} for p in (past or [])]}}}


def ident(conf=1.0):
    return [{"matched_on": "x", "match_type": "domain",
             "matches": [{"confidence_score": conf, "company_data": {"crustdata_company_id": 1, "basic_info": BI}}]}]


class Fake:
    def __init__(self, web=None, gst_status_text=None, jobs=14):
        self.spent, self.web, self.gst, self.jobs = 0.0, web or {}, gst_status_text, jobs

    def post(self, path, body, est_cost=1.0):
        self.spent += est_cost
        if path == "/company/identify":
            if "domains" in body:
                return ident() if body["domains"] == ["nimbuscloudworks.in"] else [{"matched_on": body["domains"][0], "matches": []}]
            return ident(0.8)
        if path == "/company/enrich":
            if body.get("crustdata_company_ids") == [1] or body.get("domains") == ["nimbuscloudworks.in"]:
                return [{"matches": [{"confidence_score": 1.0, "company_data": {
                    "crustdata_company_id": 1, "basic_info": BI, "headcount": {"total": 210},
                    "hiring": {"openings_count": None},
                    "people": {"founders": [], "cxos": [person("Asha Raman", "Nimbus Cloudworks Private Limited", as_enrich=True)],
                               "decision_makers": []}}}]}]
            return [{"matches": []}]
        if path == "/job/search":
            return {"job_listings": [], "count": 0, "total_count": self.jobs}
        if path == "/person/search":
            nm = body["filters"]["conditions"][0]["value"]
            db = {"Asha Raman": [person("Asha Raman", "Nimbus Cloudworks Pvt Ltd")],
                  "Vikram Sethi": [person("Vikram Sethi", "Nimbus Cloudworks")],
                  "Rohit Khanna": [person("Rohit Khanna", "Infosys", ["Nimbus Cloudworks"])]}
            return {"profiles": db.get(nm, []), "total_count": len(db.get(nm, []))}
        if path == "/web/search/live":
            q = body["query"]
            if "GST status" in q and self.gst:
                gstin = q.split('"')[1]
                return {"success": True, "results": [{"url": "https://example-gst-lookup.in", "title": gstin,
                        "snippet": f"{gstin} Nimbus Cloudworks Private Limited status {self.gst}"}]}
            for k, v in self.web.items():
                if k in q and (body.get("site") or "") == ("indiankanoon.org" if "kanoon" in v[0]["url"] else ""):
                    return {"success": True, "results": v}
            return {"success": True, "results": []}
        raise AssertionError(path)


def load(n):
    return json.loads((BIDS / n).read_text())


def test_gstin_checksum():
    assert g.gstin_checksum_ok("27AAPFU0939F1ZV")
    assert not g.gstin_checksum_ok("27AAPFU0939F1ZA")


def test_honest_bid_approved():
    rep, s = g.run(load("bid_honest.json"), Fake(gst_status_text="Active"))
    assert s["verdict"] == "APPROVE", (s, [f.detail for f in rep.findings])
    assert s["score"] >= 75 and not s["knockouts"]


def test_lookalike_bid_rejected_with_reasons():
    rep, s = g.run(load("bid_lookalike.json"), Fake())
    assert s["verdict"] == "REJECT"
    checks = {f.check: f for f in rep.findings}
    assert checks["lookalike_domain"].knockout
    assert checks["pan_match"].status == g.CONTRADICTED      # GSTIN PAN != declared PAN
    assert checks["state_match"].status == g.CONTRADICTED    # 07 = Delhi, bid says Karnataka
    assert checks["entity_type"].status == g.CONTRADICTED    # PAN type F vs 'private limited'
    assert checks["person:Rohit Khanna"].status == g.CONTRADICTED  # left the firm


def test_cancelled_gst_is_knockout():
    bid = load("bid_honest.json")
    rep, s = g.run(bid, Fake(gst_status_text="Cancelled"))
    assert s["verdict"] == "REJECT" and any("gst_status" in k for k in s["knockouts"])


def test_invalid_gstin_is_knockout():
    bid = load("bid_honest.json") | {"gstin": "29AABCN1234F1ZA"}
    _, s = g.run(bid, Fake(gst_status_text="Active"))
    assert any("gstin_format" in k for k in s["knockouts"])


def test_court_record_against_key_person_is_knockout():
    web = {'"Vikram Sethi"': [{"url": "https://indiankanoon.org/doc/1/",
                               "title": "State vs Vikram Sethi",
                               "snippet": "Vikram Sethi, director of Nimbus Cloudworks, accused of cheating under section 420"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert s["verdict"] == "REJECT"
    assert any(f.category == "people_legal" and f.knockout for f in rep.findings)


def test_namesake_hit_ignored():
    # Snippet names a different person -> must not count against our key person.
    web = {'"Asha Raman"': [{"url": "https://news.example/x", "title": "Fraud case",
                             "snippet": "Asha Ramachandran-Iyer arrested for fraud in Pune"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert s["verdict"] == "APPROVE", [f.detail for f in rep.findings if f.status == g.RED_FLAG]


def test_insolvency_lowers_score_without_knockout():
    web = {'"Nimbus Cloudworks Private Limited"': [{
        "url": "https://indiankanoon.org/doc/2/", "title": "Nimbus Cloudworks Private Limited - NCLT",
        "snippet": "Application under section 9 IBC against Nimbus Cloudworks Private Limited before NCLT Bengaluru"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert not s["knockouts"]
    assert s["categories"]["firm_legal"]["score"] <= 0.2
    assert s["verdict"] in ("HOLD", "APPROVE") and s["score"] < 100


def test_missing_data_lowers_coverage_to_hold():
    bid = {"vendor_name": "Unknown Vendor", "domain": "unknown-vendor.in", "key_personnel": []}
    _, s = g.run(bid, Fake())
    assert s["coverage"] < g.MIN_COVERAGE and s["verdict"] in ("HOLD", "REJECT")


def test_budget_stop_is_reported():
    class Broke(Fake):
        def post(self, path, body, est_cost=1.0):
            if path == "/company/enrich":
                raise g.BudgetExceeded("spent 40")
            return super().post(path, body, est_cost)
    rep, s = g.run(load("bid_honest.json"), Broke())
    assert any(f.check == "budget" for f in rep.findings)


def test_generic_nclt_noise_ignored():
    # Real-world pattern: searches return NCLT cause lists that never name the vendor.
    web = {'"Nimbus Cloudworks Private Limited"': [{"url": "https://nclt.gov.in/cause-list.pdf",
            "title": "Cause List 2023 PB", "snippet": "Following matters are listed before Principal Bench - insolvency"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert s["categories"]["firm_legal"]["score"] >= 0.85


def test_mentions_is_strict():
    assert g.mentions("Ksolves India Limited", "Order against Ksolves India Ltd")
    assert not g.mentions("Ksolves India Limited", "the tribunal resolves the matter")


def test_auto_checks_leadership_when_bid_names_nobody():
    bid = load("bid_honest.json") | {"key_personnel": []}
    rep, _ = g.run(bid, Fake(gst_status_text="Active"))
    assert any(f.check == "person:Asha Raman" and f.status == g.CONFIRMED for f in rep.findings)


# ---- Real-data regression: responses recorded from the live Crustdata API (fixtures/ksolves_live.json)
FIX = str(pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ksolves_live.json")


def test_real_ksolves_bid_approved():
    rep, s = g.run(load("ksolves.json"), g.Crust("", db=":memory:", replay=FIX))
    assert s["verdict"] == "APPROVE", [f.detail for f in rep.findings]
    assert any(f.check == "person:Ratan Srivastava" and f.status == g.CONFIRMED for f in rep.findings)


def test_real_lookalike_domain_caught_despite_fuzzy_identify():
    # Live API resolves 'ksolves.co' to Ksolves (ksolves.com) at confidence 1.0 - must still be flagged.
    rep, s = g.run(load("ksolves_lookalike.json"), g.Crust("", db=":memory:", replay=FIX))
    assert s["verdict"] == "REJECT"
    checks = {f.check: f for f in rep.findings}
    assert checks["lookalike_domain"].knockout
    assert checks["headcount"].status == g.CONTRADICTED and checks["founded"].status == g.CONTRADICTED
    assert checks["person:Arjun Malhotra"].status == g.CANT_VERIFY      # 4 namesakes, none at Ksolves


# ---- GST in Cloud / EODHD / registrations / declared cases
import sqlite3  # noqa: E402

import providers as pv  # noqa: E402

GSTIN = "29AABCN1234F1ZS"


def gstn_record(status="Active", lgnm="NIMBUS CLOUDWORKS PRIVATE LIMITED", months=12, trade="NIMBUS CLOUDWORKS"):
    """Shape of the GSTN public taxpayer-search response that GST API resellers pass through."""
    import datetime as _dt
    today = _dt.date.today()
    filing = []
    for i in range(1, months + 1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year + ((today.month - i - 1) // 12)
        filing.append({"rtntype": "GSTR3B", "ret_prd": f"{m:02d}{y}", "dof": "20/01/2026", "status": "Filed"})
    return {"data": {"gstin": GSTIN, "sts": status, "lgnm": lgnm, "tradeNam": trade,
                     "rgdt": "01/07/2017", "ctb": "Private Limited Company", "stj": "State - Karnataka",
                     "pradr": {"addr": {"bno": "12", "st": "MG Road", "loc": "Bengaluru", "stcd": "Karnataka", "pncd": "560001"}},
                     **({"cxdt": "15/03/2026"} if status != "Active" else {})},
            "filing": filing}


def eod(rev=(450e7, 400e7, 350e7), ni=(40e7, 35e7), equity=200e7):
    years = ["2026-03-31", "2025-03-31", "2024-03-31"]
    inc = {d: {"totalRevenue": r, "netIncome": (ni[i] if i < len(ni) else 10e7)} for i, (d, r) in enumerate(zip(years, rev))}
    bal = {d: {"totalStockholderEquity": equity} for d in years}
    return {"General": {"Name": "Nimbus Cloudworks Ltd", "ISIN": "INE000X01010", "Exchange": "NSE", "CurrencyCode": "INR"},
            "Highlights": {"RevenueTTM": rev[0]},
            "Financials": {"Income_Statement": {"yearly": inc}, "Balance_Sheet": {"yearly": bal}}}


class FakeExt(Fake):
    """Fake Crustdata + real providers.Http in offline mode preloaded with GST / EODHD responses."""
    offline = True

    def __init__(self, gst=None, fundamentals=None, **kw):
        super().__init__(**kw)
        self.ext = pv.Http(sqlite3.connect(":memory:"), offline=True)
        calls = []
        if gst is not None:
            calls.append({"url": f"https://gstincloud/{GSTIN}", "params": None, "response": gst})
        if fundamentals is not None:
            calls.append({"url": "https://eodhd.com/api/search/Nimbus Cloudworks", "params": {"fmt": "json"},
                          "response": [{"Code": "NIMBUS", "Exchange": "NSE", "Name": "Nimbus Cloudworks Ltd"}]})
            calls.append({"url": "https://eodhd.com/api/fundamentals/NIMBUS.NSE", "params": {"fmt": "json"},
                          "response": fundamentals})
        self.ext.preload(calls)


def find(rep, check):
    return next(f for f in rep.findings if f.check == check)


def test_gst_api_active_and_filing_counted():
    rep, s = g.run(load("bid_honest.json") | {"gstr3b_filed_last_12": None}, FakeExt(gst=gstn_record()))
    assert find(rep, "gst_status").status == g.CONFIRMED and find(rep, "gst_status").source.startswith("GST in Cloud")
    assert find(rep, "gst_legal_name").status == g.CONFIRMED
    assert "12/12" in find(rep, "filing_regularity").detail


def test_gst_api_cancelled_is_knockout_with_date():
    rep, s = g.run(load("bid_honest.json"), FakeExt(gst=gstn_record(status="Cancelled")))
    f = find(rep, "gst_status")
    assert f.knockout and "15/03/2026" in f.detail and s["verdict"] == "REJECT"


def test_gstin_registered_to_someone_else_is_knockout():
    rep, s = g.run(load("bid_honest.json"), FakeExt(gst=gstn_record(lgnm="SHREE BALAJI TRADERS", trade="BALAJI ENTERPRISES")))
    assert find(rep, "gst_legal_name").knockout and s["verdict"] == "REJECT"


def test_irregular_filer_scored_down():
    rep, _ = g.run(load("bid_honest.json") | {"gstr3b_filed_last_12": None}, FakeExt(gst=gstn_record(months=5)))
    f = find(rep, "filing_regularity")
    assert f.status == g.CONTRADICTED and abs(f.score - 5 / 12) < 1e-6


def test_eodhd_turnover_confirmed_and_tender_rule_passes():
    bid = load("bid_honest.json") | {"claimed_turnover_inr": 45e8}
    bid["tender"] = bid["tender"] | {"min_avg_turnover_inr": 30e8}
    rep, s = g.run(bid, FakeExt(gst=gstn_record(), fundamentals=eod()))
    assert find(rep, "turnover_claim").status == g.CONFIRMED
    assert find(rep, "tender_turnover_rule").status == g.CONFIRMED
    assert s["verdict"] == "APPROVE"


def test_eodhd_inflated_turnover_and_failed_eligibility():
    bid = load("bid_honest.json") | {"claimed_turnover_inr": 200e8}            # claims 4.4x the audited figure
    bid["tender"] = bid["tender"] | {"min_avg_turnover_inr": 100e8}
    rep, s = g.run(bid, FakeExt(gst=gstn_record(), fundamentals=eod()))
    assert find(rep, "turnover_claim").status == g.RED_FLAG
    assert find(rep, "tender_turnover_rule").knockout and s["verdict"] == "REJECT"


def test_negative_net_worth_is_knockout():
    rep, s = g.run(load("bid_honest.json"), FakeExt(gst=gstn_record(), fundamentals=eod(ni=(-5e7, -8e7), equity=-3e7)))
    assert find(rep, "net_worth").knockout and find(rep, "profitability").status == g.RED_FLAG


def test_cin_checks():
    bid = load("bid_honest.json") | {"cin": "U72200KA2016PTC123456"}
    rep, _ = g.run(bid, FakeExt(gst=gstn_record()))
    assert find(rep, "cin_format").status == g.CONFIRMED
    rep, _ = g.run(load("bid_honest.json") | {"cin": "U72200KA2009PTC123456"}, FakeExt(gst=gstn_record()))
    assert find(rep, "cin_year").status == g.CONTRADICTED       # CIN year 2009 vs founded 2016
    rep, _ = g.run(load("bid_honest.json") | {"cin": "X123"}, FakeExt(gst=gstn_record()))
    assert find(rep, "cin_format").knockout


def test_declared_cases_scored_and_agent_still_searches():
    bid = load("bid_honest.json") | {"known_cases": [
        {"party": "Nimbus Cloudworks Private Limited", "case_no": "ARB/12/2025", "court": "Delhi HC",
         "nature": "arbitration over payment", "status": "pending"},
        {"party": "Vikram Sethi", "case_no": "CC 55/2024", "court": "CMM Delhi", "nature": "cheating u/s 420", "status": "pending"}]}
    web = {'"Asha Raman"': [{"url": "https://indiankanoon.org/doc/9/", "title": "State vs Asha Raman",
                             "snippet": "Asha Raman of Nimbus Cloudworks accused of forgery and cheating"}]}
    rep, s = g.run(bid, FakeExt(gst=gstn_record(), web=web))
    assert find(rep, "declared_case:Nimbus Cloudworks Private Limited").category == "firm_legal"
    assert find(rep, "declared_case:Vikram Sethi").knockout                     # active criminal case vs key person
    agent = [f for f in rep.findings if f.detail.startswith("FOUND BY AGENT") and "Asha Raman" in f.detail]
    assert agent and agent[0].category == "people_legal"                      # case the reviewer missed
    assert s["verdict"] == "REJECT"


def test_providers_not_configured_reported_not_crashing():
    rep, _ = g.run(load("bid_honest.json"), Fake(gst_status_text="Active"))
    assert find(rep, "gst_api").status == g.CANT_VERIFY and find(rep, "eodhd").status == g.CANT_VERIFY
