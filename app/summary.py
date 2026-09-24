"""Aggregation behind the dashboard's Summary tab.

Kept out of dashboard.py so it can be tested without running Streamlit. All
money stays integer paise; formatting is the page's job.

Status is ORDER-level: an order is "Matched" when it is bound to a settlement
(an order<->settlement row in matches), a settlement when it is bound to an
order. Everything else is "Exception". That makes the two statuses a clean
split, and it is deliberately stricter than the record-level match_rate, which
also counts settlements the bank leg confirmed.
"""
from __future__ import annotations

from datetime import date

from src.contract import leakage_report

MATCHED, EXCEPTION = "Matched", "Exception"


def _day(v) -> date:
    return date.fromisoformat(str(v)[:10])


def batch_instruments(conn, batch_id) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT instrument FROM orders WHERE batch_id=? UNION"
        " SELECT instrument FROM settlements WHERE batch_id=? ORDER BY 1",
        (batch_id, batch_id))]


def order_date_range(conn, batch_id) -> tuple[date | None, date | None]:
    r = conn.execute("SELECT MIN(order_datetime), MAX(order_datetime) FROM orders"
                     " WHERE batch_id=?", (batch_id,)).fetchone()
    return (_day(r[0]), _day(r[1])) if r[0] else (None, None)


def summary_totals(conn, batch_id, instruments=None, date_from=None,
                   date_to=None, statuses=None) -> dict:
    """Totals for one batch. A filter left as None is not applied.

    A settlement is dated by the order it claims, so an orphan settlement drops
    out as soon as a date filter is set. Bank credits carry no payment method,
    order date or status, so any filter makes their total unknowable, and it is
    returned as None with the reason rather than as a wrong number. The same
    goes for the overcharge, which the contract audit prices per instrument
    but not per date or status.
    """
    dated = date_from is not None or date_to is not None
    reconciled_o, reconciled_s = set(), set()
    for m in conn.execute("SELECT left_id, right_id FROM matches WHERE batch_id=?"
                          " AND left_type='order' AND right_type='settlement'",
                          (batch_id,)):
        reconciled_o.add(m[0])
        reconciled_s.add(m[1])

    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE batch_id=?", (batch_id,))]
    settlements = [dict(r) for r in conn.execute(
        "SELECT * FROM settlements WHERE batch_id=?", (batch_id,))]
    order_day = {o["order_id"]: _day(o["order_datetime"]) for o in orders}

    def keep(instrument, day, matched):
        if instruments is not None and instrument not in instruments:
            return False
        if dated and (day is None or (date_from and day < date_from)
                      or (date_to and day > date_to)):
            return False
        return statuses is None or (MATCHED if matched else EXCEPTION) in statuses

    kept_o = [o for o in orders if keep(o["instrument"], order_day[o["order_id"]],
                                        o["order_id"] in reconciled_o)]
    kept_s = [s for s in settlements
              if keep(s["instrument"], order_day.get(s["order_id_claimed"]),
                      s["settlement_txn_id"] in reconciled_s)]

    bank, bank_note = None, None
    if instruments is not None:
        bank_note = ("A bank deposit bundles payouts from every payment method, "
                     "so it cannot be split by method.")
    elif dated or statuses is not None:
        bank_note = ("A bank deposit bundles many orders, so it cannot be "
                     "filtered by order date or status.")
    else:
        bank = conn.execute("SELECT COALESCE(SUM(credit_amount_paise),0) FROM"
                            " bank_credits WHERE batch_id=?",
                            (batch_id,)).fetchone()[0]

    lk = leakage_report(conn, batch_id)
    leaked = {i: b["leaked_paise"] for i, b in lk["by_instrument"].items()}
    over_only = {i: b["overcharged_paise"] for i, b in lk["by_instrument"].items()}
    over_note = None
    if dated or statuses is not None:
        over_note = ("The contract audit prices the whole batch per payment "
                     "method; it cannot be split by order date or status.")

    def method_over(ms):
        return None if over_note else sum(leaked.get(m, 0) for m in ms)

    methods = sorted({o["instrument"] for o in kept_o}
                     | {s["instrument"] for s in kept_s})
    by_method = []
    for m in methods:
        mo = [o for o in kept_o if o["instrument"] == m]
        ms = [s for s in kept_s if s["instrument"] == m]
        by_method.append({
            "instrument": m, "transactions": len(mo),
            "gross_paise": sum(o["gross_amount_paise"] for o in mo),
            "fees_paise": sum(s["mdr_paise"] for s in ms),
            "gst_paise": sum(s["gst_on_mdr_paise"] for s in ms),
            "net_paise": sum(s["net_amount_paise"] for s in ms),
            "overcharge_paise": method_over([m])})

    selected = batch_instruments(conn, batch_id) if instruments is None \
        else instruments
    return {
        "gross_sales_paise": sum(o["gross_amount_paise"] for o in kept_o),
        "fees_paise": sum(s["mdr_paise"] for s in kept_s),
        "gst_paise": sum(s["gst_on_mdr_paise"] for s in kept_s),
        "net_settled_paise": sum(s["net_amount_paise"] for s in kept_s),
        "bank_received_paise": bank, "bank_note": bank_note,
        "overcharge_paise": method_over(selected),
        # only the positive overcharges, never netted against undercharges
        "gross_overcharge_paise": None if over_note else sum(
            over_only.get(m, 0) for m in selected),
        "overcharge_note": over_note,
        "orders_reconciled": sum(1 for o in kept_o
                                 if o["order_id"] in reconciled_o),
        "orders_total": len(kept_o),
        "by_method": by_method,
    }
