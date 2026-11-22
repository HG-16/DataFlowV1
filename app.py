"""GeM Bid Checker - web form.

    pip install streamlit httpx rapidfuzz
    streamlit run app.py

API keys can be set as environment variables (CRUSTDATA_API_KEY, GSTINCLOUD_API_KEY, GSTINCLOUD_URL,
EODHD_API_KEY) or typed into the sidebar (kept only in this browser session).
"""
import json
import os

import streamlit as st

import gem_checker as g

st.set_page_config(page_title="GeM Bid Checker", page_icon="🔎", layout="wide")
st.title("GeM Bid Checker")
st.caption("Verify an IT vendor's GeM bid before the money is paid: identity, capacity, GST, financials, "
           "pending cases and key people. Every finding cites its source.")

# ---------------------------------------------------------------- sidebar: keys / mode
# Server-side keys (env vars / Streamlit secrets) are NEVER sent to the browser: the inputs start empty
# and a typed value only overrides the server key for this session.
def server_secret(name: str) -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # noqa: BLE001 - no secrets file
        pass
    return os.environ.get(name, "")


def key_input(label: str, name: str, key: str) -> str:
    have = bool(server_secret(name))
    typed = st.text_input(label, type="password", key=key,
                          placeholder="using server key" if have else "not set",
                          help="Leave empty to use the key configured on the server." if have else None)
    return typed.strip() or server_secret(name)


with st.sidebar:
    st.header("Data sources")
    replay = st.checkbox("Offline demo (recorded real data, Ksolves)", value=not server_secret("CRUSTDATA_API_KEY"))
    crust_key = "" if replay else key_input("Crustdata API key", "CRUSTDATA_API_KEY", "ck")
    st.subheader("GST in Cloud")
    gst_key = key_input("API key", "GSTINCLOUD_API_KEY", "gk")
    gst_url = st.text_input("URL template (use {gstin})", placeholder=server_secret("GSTINCLOUD_URL") or "https://.../{gstin}") \
        or server_secret("GSTINCLOUD_URL")
    gst_auth = st.text_input("Auth (header:Name or query:param)", placeholder=server_secret("GSTINCLOUD_AUTH") or "header:Authorization") \
        or server_secret("GSTINCLOUD_AUTH") or "header:Authorization"
    st.subheader("EODHD")
    eod_key = key_input("API key ", "EODHD_API_KEY", "ek")
    st.divider()
    st.markdown("**Weights**  \n" + "  \n".join(f"{k}: {v}" for k, v in g.WEIGHTS.items()))

# ---------------------------------------------------------------- the form
KSOLVES = json.load(open("bids/ksolves.json")) if os.path.exists("bids/ksolves.json") else {}
pre = KSOLVES if replay else {}

with st.form("bid"):
    st.subheader("1. Seller (from GeM seller profile)")
    c1, c2, c3 = st.columns(3)
    vendor = c1.text_input("Legal name of firm *", pre.get("vendor_name", ""))
    domain = c2.text_input("Website domain *", pre.get("domain", ""))
    entity = c3.selectbox("Entity type", ["", "private limited", "public limited", "llp", "partnership", "proprietorship"],
                          index=["", "private limited", "public limited", "llp", "partnership", "proprietorship"]
                          .index(pre.get("entity_type", "")))
    c1, c2, c3, c4 = st.columns(4)
    gstin = c1.text_input("GSTIN", pre.get("gstin", ""), max_chars=15)
    pan = c2.text_input("PAN", pre.get("pan", ""), max_chars=10)
    cin = c3.text_input("CIN / LLPIN", pre.get("cin", ""))
    udyam = c4.text_input("Udyam no. (if MSE)", pre.get("udyam", ""))
    c1, c2, c3 = st.columns(3)
    state = c1.selectbox("State of registration", [""] + sorted(set(g.GST_STATES.values())),
                         index=([""] + sorted(set(g.GST_STATES.values()))).index(pre.get("state", "")))
    founded = c2.number_input("Year founded (claimed)", 1900, 2030, int(pre.get("claimed_year_founded", 2015)))
    ticker = c3.text_input("Stock ticker if listed (e.g. KSOLVES.NSE)", pre.get("stock_ticker", ""))

    st.subheader("2. Claims in the bid")
    c1, c2, c3, c4, c5 = st.columns(5)
    headcount = c1.number_input("Employees (claimed)", 0, 10_000_000, int(pre.get("claimed_headcount", 0)))
    t1 = c2.number_input("Turnover FY latest (₹ cr)", 0.0, 1e7, 0.0)
    t2 = c3.number_input("Turnover FY-1 (₹ cr)", 0.0, 1e7, 0.0)
    t3 = c4.number_input("Turnover FY-2 (₹ cr)", 0.0, 1e7, 0.0)
    nw = c5.number_input("Net worth (₹ cr)", -1e7, 1e7, 0.0)

    st.subheader("3. Tender")
    tp = pre.get("tender", {})
    c1, c2, c3, c4 = st.columns(4)
    t_id = c1.text_input("GeM bid no.", tp.get("id", ""))
    t_val = c2.number_input("Bid value (₹ cr)", 0.0, 1e7, float(tp.get("value_inr", 0)) / 1e7)
    t_min = c3.number_input("Min avg annual turnover required (₹ cr)", 0.0, 1e7, 0.0)
    t_exp = c4.number_input("Min years of experience required", 0, 100, 0)

    st.subheader("4. Key personnel named in the bid")
    people = st.data_editor(pre.get("key_personnel") or [{"name": "", "title": ""}], num_rows="dynamic",
                            column_config={"name": "Name", "title": "Role in bid"}, key="people")

    st.subheader("5. GST details you already checked (optional - the GST in Cloud API fills these)")
    c1, c2, c3 = st.columns(3)
    gst_status = c1.selectbox("GST status", ["", "active", "cancelled", "suspended"])
    filed = c2.number_input("GSTR-3B filed in last 12 months (-1 = unknown)", -1, 12, -1)
    gst_reg = c3.text_input("GST registration date (YYYY-MM-DD)", "")

    st.subheader("6. Cases you already found")
    st.caption("The agent ALSO searches court records and news for the firm and every key person, and lists "
               "anything you didn't find as 'FOUND BY AGENT'.")
    cases = st.data_editor([{"party": "", "case_no": "", "court": "", "nature": "", "status": "pending"}],
                           num_rows="dynamic", key="cases",
                           column_config={"party": "Against (firm or person)", "case_no": "Case no.", "court": "Court",
                                          "nature": "Nature (e.g. arbitration, cheating u/s 420, NCLT)",
                                          "status": st.column_config.SelectboxColumn("Status", options=["pending", "disposed"])})
    go = st.form_submit_button("Run checks", type="primary")

# ---------------------------------------------------------------- run
if go:
    if not vendor or not domain:
        st.error("Firm name and website domain are required.")
        st.stop()
    for k, v in {"GSTINCLOUD_API_KEY": gst_key, "GSTINCLOUD_URL": gst_url, "GSTINCLOUD_AUTH": gst_auth,
                 "EODHD_API_KEY": eod_key}.items():
        if v:
            os.environ[k] = v
    cr = lambda x: x * 1e7 if x else None  # noqa: E731
    bid = {
        "vendor_name": vendor.strip(), "domain": domain.strip(), "entity_type": entity, "gstin": gstin.strip().upper(),
        "pan": pan.strip().upper(), "cin": cin.strip().upper(), "udyam": udyam.strip().upper(), "state": state,
        "claimed_year_founded": founded, "stock_ticker": ticker.strip().upper(),
        "claimed_headcount": headcount or None,
        "turnover_last_3fy_inr": [x for x in (cr(t1), cr(t2), cr(t3)) if x],
        "net_worth_inr": cr(nw) if nw else None,
        "gst_status": gst_status, "gstr3b_filed_last_12": filed if filed >= 0 else None,
        "gst_registration_date": gst_reg.strip() or None,
        "key_personnel": [p for p in people if (p.get("name") or "").strip()],
        "known_cases": [c for c in cases if (c.get("case_no") or c.get("nature") or "").strip()],
        "tender": {"id": t_id, "value_inr": cr(t_val), "min_avg_turnover_inr": cr(t_min),
                   "min_experience_years": t_exp or None},
    }
    if replay:
        client = g.Crust("", db=":memory:", replay="fixtures/ksolves_live.json")
    elif crust_key:
        client = g.Crust(crust_key)
    else:
        st.error("Add a Crustdata API key in the sidebar, or tick the offline demo.")
        st.stop()

    with st.spinner("Checking identity, capacity, GST, financials, cases and key people..."):
        rep, s = g.run(bid, client)

    color = {"APPROVE": "green", "HOLD": "orange", "REJECT": "red"}[s["verdict"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"### :{color}[{s['verdict']}]")
    c2.metric("Score", f"{s['score']}/100")
    c3.metric("Evidence coverage", f"{s['coverage']:.0%}")
    c4.metric("Crustdata credits", f"{rep.credits_spent:.1f}")
    for k in s["knockouts"]:
        st.error(f"Knockout: {k}")
    if s["verdict"] == "REJECT":
        st.warning("REJECT needs a human sign-off before it is acted on.")

    st.dataframe([{"category": c, "weight": v["weight"],
                   "sub-score": "n/a (no evidence)" if v["score"] is None else str(round(v["score"] * 100))}
                  for c, v in s["categories"].items()], hide_index=True, width="stretch")

    icon = {g.CONFIRMED: "✅", g.CONTRADICTED: "❌", g.CANT_VERIFY: "❔", g.RED_FLAG: "🚩"}
    for cat in g.WEIGHTS:
        fs = [f for f in rep.findings if f.category == cat]
        if not fs:
            continue
        with st.expander(f"{cat} ({len(fs)} findings)", expanded=any(f.status in (g.RED_FLAG, g.CONTRADICTED) for f in fs)):
            for f in fs:
                st.markdown(f"{icon[f.status]} **{f.check}**: {f.detail}" + ("  **[KNOCKOUT]**" if f.knockout else ""))
                meta = " · ".join(x for x in (f.source, f.date) if x)
                if meta:
                    st.caption(meta)
                if f.passage:
                    st.code(f.passage[:400], language=None)

    out = {"bid": bid, "result": s, "findings": [g.asdict(f) for f in rep.findings], "credits_spent": rep.credits_spent}
    st.download_button("Download report.json", json.dumps(out, indent=2, default=str), "report.json", "application/json")
