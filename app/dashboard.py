"""Streamlit dashboard.

Designed to be legible on video at small size: large type, few colours, one
idea per section. The learning curves come first because they are the claim
the whole project is making.

    streamlit run app/dashboard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.db as db                                       # noqa: E402
from src.db import get_conn                               # noqa: E402
from src.contract import (compare_to_contract, leakage_report)  # noqa: E402
from src.metrics import learning_curve, rule_activity, score_batch  # noqa: E402
from src.money import format_paise                        # noqa: E402
from src.rule_engine import plain_english                 # noqa: E402

st.set_page_config(page_title="Reconciliation Agent", layout="wide")

st.markdown("""
<style>
  .block-container {padding-top: 2rem;}
  h1 {font-size: 2.4rem !important;}
  h2 {font-size: 1.7rem !important; margin-top: 1.4rem;}
  [data-testid="stMetricValue"] {font-size: 2.1rem;}
  [data-testid="stMetricLabel"] {font-size: 1rem;}
</style>
""", unsafe_allow_html=True)


def conn():
    """A fresh connection per rerun, deliberately NOT cached.

    Streamlit reruns the script on a different thread each time, and a sqlite3
    connection may only be used from the thread that made it -- caching one
    raises ProgrammingError on the second interaction. Opening a local sqlite
    file is microseconds; caching it is a bug.
    """
    return get_conn(db.DB_PATH)


def batches(c):
    return [r["batch_id"] for r in c.execute(
        "SELECT DISTINCT batch_id FROM run_metrics ORDER BY batch_id")]


if not Path(db.DB_PATH).exists():
    st.error("No database yet. Run:  python -m src.pipeline --batch 1")
    st.stop()

c = conn()
if not batches(c):
    st.error("No batches have been run yet. Run:  python -m src.pipeline --batch 1")
    st.stop()

st.title("Multi-Source Reconciliation Agent")
st.caption("Algorithms decide matches. The LLM only proposes rules and writes "
           "explanations. It never decides on its own that two records match.")

numbered = [b for b in batches(c) if b.isdigit()]
curve = pd.DataFrame(learning_curve(c, numbered)) if numbered else pd.DataFrame()

# ------------------------------------------------- 0. were you charged right?
st.header("1 · Were you charged what you agreed to?")
st.caption("The contracted rates are an input — they are in the merchant's "
           "signed agreement. The settlement data is what is under audit. "
           "Learning the rates from the aggregator's own output instead would "
           "quietly accept whatever they charged.")

audit_batch = st.selectbox("Audit batch", batches(c),
                           index=len(batches(c)) - 1, key="audit")
rows = compare_to_contract(c, audit_batch)
lk = leakage_report(c, audit_batch)

la, lb, lcol = st.columns(3)
la.metric("Fee leakage", lk["total_leaked"],
          delta=None if lk["total_leaked_paise"] == 0 else "over contract",
          delta_color="inverse")
lb.metric("Transactions overcharged", lk["transactions_overcharged"],
          delta_color="inverse")
lcol.metric("Transactions audited", lk["transactions_checked"])

st.dataframe(pd.DataFrame([{
    "instrument": r["instrument"],
    "contracted": r["contracted"],
    "observed in the data": r.get("observed", "-"),
    "transactions": r["samples"],
    "verdict": ("matches contract" if r["agrees"]
                else "no data" if r["agrees"] is None
                else "DEVIATES FROM CONTRACT")} for r in rows]),
    use_container_width=True, hide_index=True)

if lk["total_leaked_paise"] > 0:
    st.error(f"**{lk['total_leaked']} charged above the contracted rates** "
             f"across {lk['transactions_overcharged']} transactions.")
    st.dataframe(pd.DataFrame([{
        "settlement": w["settlement_txn_id"], "instrument": w["instrument"],
        "charged": format_paise(w["charged_paise"]),
        "agreed": format_paise(w["agreed_paise"]),
        "over by": format_paise(w["leaked_paise"])}
        for w in lk["worst_offenders"]]),
        use_container_width=True, hide_index=True)
else:
    st.success("Every fee deducted matches the contracted rates.")

# ------------------------------------------------------------ 1. the headline
st.header("2 · What it learned, batch over batch")

if curve.empty:
    st.info("Run at least two batches to see a learning curve.")
else:
    first, last = curve.iloc[0], curve.iloc[-1]
    a, b, d, e = st.columns(4)
    a.metric("Open exceptions", int(last.open_exceptions),
             int(last.open_exceptions - first.open_exceptions))
    b.metric("LLM calls per 100 records", f"{last.llm_calls_per_100_records:.1f}",
             f"{last.llm_calls_per_100_records - first.llm_calls_per_100_records:+.1f}")
    d.metric("Match rate", f"{last.match_rate:.1%}",
             f"{(last.match_rate - first.match_rate) * 100:+.1f} pts")
    e.metric("False positives", int(last.false_positive_count),
             int(last.false_positive_count - first.false_positive_count),
             delta_color="inverse")

    idx = curve.set_index("batch_id")
    left, right = st.columns(2)
    with left:
        st.subheader("Falling: exceptions and LLM calls")
        st.line_chart(idx[["open_exceptions", "llm_calls",
                           "llm_calls_per_100_records"]], height=280)
    with right:
        st.subheader("Rising: match rate and the rule library")
        st.line_chart(idx[["match_rate", "precision", "recall"]], height=280)
        st.bar_chart(idx[["active_rules"]], height=160)

    st.caption("Batch 1 is mostly exceptions on purpose: the rule library "
               "starts with a single exact-id rule and knows nothing about "
               "fees or settlement timing.")

# ---------------------------------------------------------- 2. batch summary
st.header("3 · Batch summary")
sel = st.selectbox("Batch", batches(c), index=len(batches(c)) - 1)
s = score_batch(c, sel)

k = st.columns(5)
k[0].metric("Precision", f"{s['precision']:.1%}")
k[1].metric("Recall", f"{s['recall']:.1%}")
k[2].metric("Match rate", f"{s['match_rate']:.1%}")
k[3].metric("False positives", s["false_positive_count"], delta_color="inverse")
k[4].metric("Money at risk", format_paise(s["money_at_risk_paise"]))

c1, c2 = st.columns([2, 3])
with c1:
    st.subheader("Who resolved it")
    if s["matches_by_resolver"]:
        st.bar_chart(pd.Series(s["matches_by_resolver"], name="matches"),
                     height=240)
    st.caption(f"Cost-weighted error **{s['cost_weighted_error']:.0f}** — a "
               f"false positive is priced at 50x an open exception, because a "
               f"wrong match corrupts the books silently while an exception "
               f"costs a controller two minutes.")
with c2:
    st.subheader("False positives")
    if s["false_positive_count"] == 0:
        st.success("None. Every match claimed in this batch is correct "
                   "against ground truth.")
    else:
        st.warning(f"{s['false_positive_count']} wrong matches — "
                   f"{format_paise(s['false_positive_money_paise'])} at risk")
        st.dataframe(pd.DataFrame(s["false_positives"])[
            ["left", "right", "resolved_by", "trap_type", "money_paise"]],
            use_container_width=True, hide_index=True)
    if s["component_sizes"]:
        st.caption(f"DSU component sizes: {s['component_sizes']}")

# ------------------------------------------------------------ 3. rule library
st.header("4 · The rule library it induced")
act = rule_activity(c)
r = st.columns(4)
r[0].metric("Active rules", act["active"])
r[1].metric("Promoted", act["promoted"])
r[2].metric("Rejected by the gate", act["rejected"])
r[3].metric("Retired", act["retired"])

rules = []
for row in c.execute("SELECT * FROM rules WHERE status='active' ORDER BY priority"):
    audit = c.execute("SELECT detail_json FROM rule_audit WHERE event='promoted'"
                      " AND rule_id=?", (row["rule_id"],)).fetchone()
    bt = json.loads(audit["detail_json"])["backtest"] if audit else {}
    rules.append({
        "rule": plain_english(json.loads(row["predicate_json"])),
        "induced?": "yes" if row["promoted_from_proposal_id"] else "seeded",
        "promoted": row["promoted_at"] or "-",
        "backtest precision": f"{bt.get('precision', 0):.0%}" if bt else "-",
        "records agreed": str(bt.get("correct_matches", "-")),
        "contradicted": str(bt.get("wrong_matches", "-")),
        "applied": row["times_applied"]})
st.dataframe(pd.DataFrame(rules), use_container_width=True, hide_index=True)
st.caption("Nothing here was configured. Every 'induced' row was proposed by "
           "the model, then had to survive an occurrence count, a confidence "
           "floor, and a replay against all previously-resolved records.")

with st.expander("Rules the gate BLOCKED — and why", expanded=True):
    rejected = c.execute(
        "SELECT * FROM rule_proposals WHERE status='rejected'"
        " ORDER BY occurrence_count DESC LIMIT 15").fetchall()
    if not rejected:
        st.info("No proposals have been rejected in this run.")
    else:
        st.dataframe(pd.DataFrame([{
            "proposed rule": plain_english(json.loads(p["predicate_json"])),
            "times proposed": p["occurrence_count"],
            "avg confidence": f"{p['llm_confidence'] or 0:.2f}",
            "blocked on": p["rejection_reason"]} for p in rejected]),
            use_container_width=True, hide_index=True)
        st.caption("A rejection is the gate working. The model proposed these "
                   "confidently; the backtest found records they contradicted.")

# --------------------------------------------------------- 4. exception queue
st.header("5 · Exception queue")
st.caption("Sorted by money at risk — this is the controller's actual work list.")
rows = c.execute(
    "SELECT * FROM exceptions WHERE status='open' AND batch_id=?"
    " ORDER BY money_at_risk_paise DESC LIMIT 50", (sel,)).fetchall()
if not rows:
    st.success("No open exceptions in this batch.")
else:
    st.dataframe(pd.DataFrame([{
        "record": f"{e['record_type']}:{e['record_id']}",
        "reason": e["reason_code"],
        "money at risk": format_paise(e["money_at_risk_paise"]),
        "what it considered": ", ".join(
            str(x) for x in json.loads(e["candidates_json"] or "[]")[:3]) or "-",
        "detail": e["reason_text"]} for e in rows]),
        use_container_width=True, hide_index=True)

# --------------------------------------------------------------- 5. Q&A chat
st.header("6 · Ask the settlement agent")
st.caption('Try: "What have you learned about this merchant?"')

if "chat" not in st.session_state:
    st.session_state.chat = []
for role, text in st.session_state.chat:
    with st.chat_message(role):
        st.markdown(text)

if q := st.chat_input("Ask about the reconciled data"):
    st.session_state.chat.append(("user", q))
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        try:
            from src.qa_agent import SettlementQA
            with st.spinner("Looking it up..."):
                out = SettlementQA(conn=c).ask(q)
            answer = out["answer"]
            if out.get("tools_used"):
                answer += ("\n\n<sub>tools called: "
                           + ", ".join(t["tool"] for t in out["tools_used"])
                           + "</sub>")
        except Exception as e:
            answer = (f"Could not reach the model ({type(e).__name__}). "
                      f"Set ANTHROPIC_API_KEY to enable chat; every other "
                      f"section of this dashboard works without it.")
        st.markdown(answer, unsafe_allow_html=True)
        st.session_state.chat.append(("assistant", answer))
