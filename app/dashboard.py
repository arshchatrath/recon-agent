"""Streamlit dashboard.

Designed for a demo watched on video at small size: one control, one status
answer above the fold, and everything else behind tabs rather than a long
scroll. Large type, a small palette used semantically (red means money is
leaking, amber means a human is needed, green means clear), and no decoration
that does not carry information.

    streamlit run app/dashboard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.db as db                                       # noqa: E402
from src.contract import compare_to_contract, leakage_report  # noqa: E402
from src.db import get_conn                               # noqa: E402
from src.metrics import learning_curve, rule_activity, score_batch  # noqa: E402
from src.money import format_paise                        # noqa: E402
from src.rule_engine import plain_english                 # noqa: E402

st.set_page_config(page_title="Reconciliation Agent", layout="wide",
                   initial_sidebar_state="expanded")

DANGER, WARN, OK, COOL, MUTED = "#c0392b", "#c77700", "#1a7f5a", "#2563eb", "#6b7280"

st.markdown(f"""
<style>
  .block-container {{padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px;}}
  h1 {{font-size: 2.1rem !important; letter-spacing: -0.02em;}}
  h2 {{font-size: 1.45rem !important; margin-top: .4rem;}}
  h3 {{font-size: 1.05rem !important; color: {MUTED}; font-weight: 600;
       text-transform: uppercase; letter-spacing: .06em;}}
  [data-testid="stMetricValue"] {{font-size: 2rem; font-weight: 650;}}
  [data-testid="stMetricLabel"] {{font-size: .82rem; color: {MUTED};
       text-transform: uppercase; letter-spacing: .05em;}}
  .stTabs [data-baseweb="tab"] {{font-size: 1rem; font-weight: 600;
       padding: .55rem 1.1rem;}}
  .banner {{padding: 1.15rem 1.4rem; border-radius: 10px; margin: .3rem 0 1.4rem 0;
       font-size: 1.1rem; line-height: 1.5; border-left: 6px solid;}}
  .banner b {{font-size: 1.26rem;}}
  .bad  {{background: #fdf0ee; border-color: {DANGER}; color: #7d2018;}}
  .good {{background: #eefaf4; border-color: {OK};     color: #10563a;}}
  .card {{background: #fbfbfc; border: 1px solid #e6e8eb; border-radius: 10px;
       padding: 1rem 1.15rem; height: 100%;}}
  .card .k {{color: {MUTED}; font-size: .74rem; text-transform: uppercase;
       letter-spacing: .06em;}}
  .card .v {{font-size: 1.55rem; font-weight: 650; line-height: 1.35;}}
  .foot {{color: {MUTED}; font-size: .85rem;}}
</style>
""", unsafe_allow_html=True)


def conn():
    """A fresh connection per rerun, deliberately NOT cached.

    Streamlit reruns the script on a different thread each time, and a sqlite3
    connection may only be used from the thread that made it -- caching one
    raises ProgrammingError on the second interaction.
    """
    return get_conn(db.DB_PATH)


def batches(c):
    return [r["batch_id"] for r in c.execute(
        "SELECT DISTINCT batch_id FROM run_metrics ORDER BY batch_id")]


def card(col, label, value, colour=None):
    col.markdown(
        f'<div class="card"><div class="k">{label}</div>'
        f'<div class="v" style="color:{colour or "inherit"}">{value}</div></div>',
        unsafe_allow_html=True)


def line(df, x, ys, colours, title, y_title, pct=False):
    """One styled multi-series line chart. Altair rather than st.line_chart so
    the colours carry the same meaning on every chart."""
    long = df.melt(id_vars=[x], value_vars=ys, var_name="series", value_name="v")
    fmt = ".0%" if pct else "~s"
    return (alt.Chart(long, title=title).mark_line(point=True, strokeWidth=3)
            .encode(
                x=alt.X(f"{x}:N", title=None,
                        axis=alt.Axis(labelAngle=0, labelFontSize=12)),
                y=alt.Y("v:Q", title=y_title,
                        axis=alt.Axis(format=fmt, labelFontSize=12)),
                color=alt.Color("series:N", title=None,
                                scale=alt.Scale(domain=ys, range=colours),
                                legend=alt.Legend(orient="top", labelFontSize=12)),
                tooltip=[x, "series", alt.Tooltip("v:Q", format=".3f")])
            .properties(height=270).configure_view(strokeWidth=0))


# --------------------------------------------------------------------- guards
if not Path(db.DB_PATH).exists():
    st.title("Reconciliation Agent")
    st.error("No database yet. Run:  `python -m src.pipeline --batch 1`")
    st.stop()

c = conn()
all_batches = batches(c)
if not all_batches:
    st.title("Reconciliation Agent")
    st.error("No batches have been run yet. Run:  `python -m src.pipeline --batch 1`")
    st.stop()

# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### Reconciliation Agent")
    st.caption("Three sources, one truth — and proof you were charged what you "
               "agreed to.")
    batch = st.selectbox("Batch", all_batches, index=len(all_batches) - 1)
    st.divider()

    s = score_batch(c, batch)
    lk = leakage_report(c, batch)
    st.metric("Fee leakage", lk["total_leaked"],
              delta=None if lk["total_leaked_paise"] == 0 else "above contract",
              delta_color="inverse")
    st.metric("Open exceptions", s["open_exceptions"])
    st.metric("Precision", f"{s['precision']:.1%}")
    st.metric("False positives", s["false_positive_count"], delta_color="inverse")
    st.divider()
    st.caption(f"**{s['total_records']}** records · **{s['active_rules']}** active "
               f"rules · {s['wall_clock_seconds']:.1f}s")
    st.caption("Every tab works without an API key, except **Ask**.")

numbered = [b for b in all_batches if b.isdigit()]
curve = pd.DataFrame(learning_curve(c, numbered)) if numbered else pd.DataFrame()

# ---------------------------------------------------------------- the answer
st.title("Were you charged what you agreed to?")

rows = compare_to_contract(c, batch)
deviating = [r["instrument"] for r in rows if r["agrees"] is False]

if lk["total_leaked_paise"] > 0:
    st.markdown(
        f'<div class="banner bad">In batch {batch} the aggregator charged '
        f'<b>{lk["total_leaked"]}</b> more than your contract allows — across '
        f'{lk["transactions_overcharged"]} of {lk["transactions_checked"]} '
        f'transactions, on {", ".join(deviating)}.</div>',
        unsafe_allow_html=True)
elif s["open_exceptions"]:
    st.markdown(
        f'<div class="banner good">Every fee in batch {batch} matches the '
        f'contracted rates. <b>{s["open_exceptions"]}</b> records still need a '
        f'human — see <b>Exceptions</b>.</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="banner good">Fully reconciled. Every fee matches '
                'contract and nothing is outstanding.</div>',
                unsafe_allow_html=True)

k = st.columns(4)
card(k[0], "Fee leakage", lk["total_leaked"],
     DANGER if lk["total_leaked_paise"] else OK)
card(k[1], "Match rate", f"{s['match_rate']:.1%}", COOL)
card(k[2], "Precision / recall", f"{s['precision']:.0%} / {s['recall']:.0%}",
     OK if s["precision"] == 1 else WARN)
card(k[3], "False positives", s["false_positive_count"],
     OK if not s["false_positive_count"] else DANGER)

st.write("")
tab_audit, tab_learn, tab_rules, tab_exc, tab_ask = st.tabs(
    ["Contract audit", "How it learned", "Rule library", "Exceptions", "Ask"])

# ------------------------------------------------------------ contract audit
with tab_audit:
    st.markdown("### Contracted rates vs what was actually deducted")
    st.caption("The contract is an input — it is in the merchant's signed "
               "agreement. The settlement data is what is under audit. Learning "
               "the rates from the aggregator's own output would quietly accept "
               "whatever they charged.")

    st.dataframe(pd.DataFrame([{
        "instrument": r["instrument"], "contracted": r["contracted"],
        "observed in data": r.get("observed", "—"),
        "transactions": r["samples"],
        "verdict": ("matches contract" if r["agrees"]
                    else "no data" if r["agrees"] is None
                    else "DEVIATES FROM CONTRACT")} for r in rows]),
        width='stretch', hide_index=True)

    if lk["worst_offenders"]:
        st.markdown("### Worst individual transactions")
        st.dataframe(pd.DataFrame([{
            "settlement": w["settlement_txn_id"], "order": w["order_id"],
            "instrument": w["instrument"],
            "charged": format_paise(w["charged_paise"]),
            "agreed": format_paise(w["agreed_paise"]),
            "over by": format_paise(w["leaked_paise"])}
            for w in lk["worst_offenders"]]),
            width='stretch', hide_index=True)

    if not curve.empty and curve["fee_leakage_paise"].abs().sum() > 0:
        st.markdown("### When did it start?")
        bars = (alt.Chart(curve).mark_bar(size=46, cornerRadiusEnd=4).encode(
            x=alt.X("batch_id:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("fee_leakage_paise:Q", title="leakage (paise)"),
            color=alt.condition(alt.datum.fee_leakage_paise > 0,
                                alt.value(DANGER), alt.value(OK)),
            tooltip=["batch_id", "fee_leakage_paise"])
            .properties(height=240).configure_view(strokeWidth=0))
        st.altair_chart(bars, width='stretch')
        st.caption("A rate change reads as a step — which is the question a "
                   "controller actually asks.")

# --------------------------------------------------------------- the learning
with tab_learn:
    if curve.empty:
        st.info("Run at least two batches to see a learning curve.")
    else:
        first, last = curve.iloc[0], curve.iloc[-1]
        m = st.columns(4)
        m[0].metric("Open exceptions", int(last.open_exceptions),
                    int(last.open_exceptions - first.open_exceptions))
        m[1].metric("LLM calls per 100 records",
                    f"{last.llm_calls_per_100_records:.1f}",
                    f"{last.llm_calls_per_100_records - first.llm_calls_per_100_records:+.1f}")
        m[2].metric("Match rate", f"{last.match_rate:.1%}",
                    f"{(last.match_rate - first.match_rate) * 100:+.1f} pts")
        m[3].metric("False positives", int(last.false_positive_count),
                    int(last.false_positive_count - first.false_positive_count),
                    delta_color="inverse")

        a, b = st.columns(2)
        with a:
            st.altair_chart(line(curve, "batch_id",
                                 ["open_exceptions", "llm_calls"],
                                 [DANGER, "#7c3aed"],
                                 "Falling: exceptions and model calls", "count"),
                            width='stretch')
        with b:
            st.altair_chart(line(curve, "batch_id",
                                 ["match_rate", "recall", "precision"],
                                 [COOL, OK, MUTED],
                                 "Rising: coverage, precision held at 100%",
                                 "rate", pct=True), width='stretch')
        st.caption("Batch 1 is mostly exceptions on purpose: the library starts "
                   "with a single exact-id rule and knows nothing about fees or "
                   "settlement timing. Everything after that was induced.")

# --------------------------------------------------------------- rule library
with tab_rules:
    act = rule_activity(c)
    r = st.columns(4)
    card(r[0], "Active rules", act["active"], COOL)
    card(r[1], "Promoted", act["promoted"], OK)
    card(r[2], "Blocked by the gate", act["rejected"], WARN)
    card(r[3], "Retired", act["retired"], MUTED)

    st.markdown("### What it induced from the data")
    lib = []
    for row in c.execute("SELECT * FROM rules WHERE status='active'"
                         " ORDER BY priority, rule_id"):
        audit = c.execute("SELECT detail_json FROM rule_audit WHERE"
                          " event='promoted' AND rule_id=?",
                          (row["rule_id"],)).fetchone()
        bt = json.loads(audit["detail_json"])["backtest"] if audit else {}
        lib.append({
            "rule": plain_english(json.loads(row["predicate_json"])),
            "source": "induced" if row["promoted_from_proposal_id"] else "seeded",
            "backtest": f"{bt.get('precision', 0):.0%}" if bt else "—",
            "agreed": str(bt.get("correct_matches", "—")),
            "contradicted": str(bt.get("wrong_matches", "—")),
            "applied": row["times_applied"]})
    st.dataframe(pd.DataFrame(lib), width='stretch', hide_index=True)
    st.caption("Nothing here was configured. Every 'induced' row was proposed "
               "by the model, then had to survive an occurrence count, a "
               "confidence floor, and a replay against all previously-resolved "
               "records.")

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
                width='stretch', hide_index=True)
            st.caption("A rejection is the gate working. The model proposed "
                       "these confidently; the backtest found records they "
                       "contradicted.")

# ----------------------------------------------------------- exception queue
with tab_exc:
    st.markdown(f"### {s['open_exceptions']} records need a human — "
                f"{format_paise(s['exception_money_paise'])} under review")
    st.caption("Sorted by money at risk. This is the actual work list.")
    rows_e = c.execute(
        "SELECT * FROM exceptions WHERE status='open' AND batch_id=?"
        " ORDER BY money_at_risk_paise DESC LIMIT 100", (batch,)).fetchall()
    if not rows_e:
        st.success("No open exceptions in this batch.")
    else:
        st.dataframe(pd.DataFrame([{
            "record": f"{e['record_type']}:{e['record_id']}",
            "reason": e["reason_code"],
            "money at risk": format_paise(e["money_at_risk_paise"]),
            "considered": ", ".join(
                str(x) for x in json.loads(e["candidates_json"] or "[]")[:2]) or "—",
            "detail": e["reason_text"]} for e in rows_e]),
            width='stretch', hide_index=True)
        st.caption("`NO_SETTLEMENT` — the order exists but no money arrived. "
                   "`ORPHAN_SETTLEMENT` — a payout claims an order that is not "
                   "in the ledger. `FEE_UNEXPLAINED` — no learned rule accounts "
                   "for the deduction.")

# ------------------------------------------------------------------- the chat
with tab_ask:
    st.markdown("### Ask the settlement agent")
    st.caption('It answers only from tools over the reconciled data, and cites '
               'the record ids it used. Try: **"Am I being overcharged?"** or '
               '**"What have you learned about this merchant?"**')

    if "chat" not in st.session_state:
        st.session_state.chat = []
    for role, text in st.session_state.chat:
        with st.chat_message(role):
            st.markdown(text, unsafe_allow_html=True)

    if q := st.chat_input("Ask about the reconciled data"):
        st.session_state.chat.append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            try:
                from src.qa_agent import SettlementQA
                with st.spinner("Looking it up…"):
                    out = SettlementQA(conn=c).ask(q)
                answer = out["answer"]
                if out.get("tools_used"):
                    answer += ('\n\n<span class="foot">tools called: '
                               + ", ".join(t["tool"] for t in out["tools_used"])
                               + "</span>")
            except Exception as e:
                answer = (f"Could not reach the model ({type(e).__name__}). Set "
                          f"an API key to enable chat — every other tab works "
                          f"without one.")
            st.markdown(answer, unsafe_allow_html=True)
            st.session_state.chat.append(("assistant", answer))
