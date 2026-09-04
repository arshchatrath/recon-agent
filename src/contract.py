"""Contract compliance: were you charged what you agreed to?

This is the point of the system, and it is worth being precise about why.

An earlier version induced the fee schedule from settlement data and matched
against what it found. That is backwards. If the aggregator quietly bills 2.1%
against a contracted 1.8%, a system that learns from their output observes
2.1%, finds it perfectly consistent, and silently marks every overcharged
transaction as correct. It launders the leakage into the books and reports
100% precision while doing it.

Reconciliation exists to verify that what happened matches *what was agreed*.
So the contract is an INPUT -- the merchant has it, it is in their signed
agreement -- and the settlement data is the thing under audit.

Two independent readings of what actually happened:

  observed_schedule()  a deterministic statistical estimate, no model involved
  the learned rules    what the LLM induced and the gate promoted

They should agree. Where they disagree with the CONTRACT, that is leakage, and
it is reported in rupees against named transactions.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from src.config import load
from src.money import apply_rate, format_paise

__all__ = ["contract_terms", "contract_fee", "observed_schedule",
           "compare_to_contract", "leakage_report"]


def contract_terms(cfg=None) -> dict:
    """The merchant's agreed rates. Legitimately known -- it is their contract."""
    return (cfg or load()).get("contract", {})


def contract_fee(gross: int, instrument: str, terms=None) -> int | None:
    """Fee + GST the contract says should be deducted from this amount."""
    terms = terms or contract_terms()
    t = (terms.get("instruments") or {}).get(instrument)
    if t is None:
        return None
    fee = (int(t["flat_paise"]) if "flat_paise" in t
           else apply_rate(gross, t["rate"]))
    return fee + apply_rate(fee, terms.get("gst", 0))


# ------------------------------------------------------- what actually happened
def _rows(conn, batch_id=None):
    """Settlements whose deduction is purely a fee.

    Refunded orders are short for a different reason and chargeback reversals
    are negative, so both would poison the estimate.
    """
    # Split payouts are excluded. A flat contracted fee is charged once per
    # settlement, but a split spreads one fee across its legs, so every leg
    # looks undercharged and the total comes out negative on an aggregator
    # that is in fact overcharging. Rate-based instruments are unaffected,
    # but the exclusion has to be uniform to keep the comparison honest.
    sql = ("SELECT s.instrument, s.gross_amount_paise, s.net_amount_paise"
           " FROM settlements s JOIN orders o ON o.order_id = s.order_id_claimed"
           " WHERE o.status = 'captured' AND s.gross_amount_paise > 0"
           " AND s.order_id_claimed IN (SELECT order_id_claimed FROM settlements"
           "   GROUP BY order_id_claimed, batch_id HAVING COUNT(*) = 1)")
    params = []
    if batch_id is not None:
        sql += " AND s.batch_id = ?"
        params.append(batch_id)
    return conn.execute(sql, params).fetchall()


def observed_schedule(conn, batch_id=None, gst=None) -> dict:
    """Estimate the fee schedule actually applied, deterministically.

    No model. Group by instrument, look at what was deducted, and decide
    whether it behaves like a percentage (constant ratio to gross) or a flat
    amount (constant absolute value). This recovers the schedule exactly on our
    data, which is the honest baseline any inference approach has to beat --
    and it costs nothing to run.
    """
    gst = contract_terms().get("gst", 0) if gst is None else gst
    by_instrument = defaultdict(list)
    for r in _rows(conn, batch_id):
        by_instrument[r["instrument"]].append(
            (int(r["gross_amount_paise"]), int(r["net_amount_paise"])))

    out = {}
    for instrument, rows in by_instrument.items():
        if len(rows) < 3:
            continue                          # too thin to say anything
        deductions = [g - n for g, n in rows]
        ratios = [d / g for d, (g, _) in zip(deductions, rows) if g]
        if not ratios:
            continue

        # A percentage fee holds its RATIO steady; a flat fee holds its ABSOLUTE
        # value steady. Compare the two spreads on a common scale.
        ratio_spread = statistics.pstdev(ratios)
        mean_gross = statistics.fmean(g for g, _ in rows)
        flat_spread = statistics.pstdev(deductions) / mean_gross if mean_gross else 1

        if ratio_spread <= flat_spread:
            rate = statistics.median(ratios) / (1 + gst)
            out[instrument] = {"shape": "rate", "rate": round(rate, 6),
                               "samples": len(rows)}
        else:
            flat = statistics.median(deductions) / (1 + gst)
            out[instrument] = {"shape": "flat", "flat_paise": round(flat),
                               "samples": len(rows)}
    return out


# ---------------------------------------------------------------- the comparison
def compare_to_contract(conn, batch_id=None, cfg=None) -> list[dict]:
    """Contracted terms against observed behaviour, per instrument."""
    terms = contract_terms(cfg)
    observed = observed_schedule(conn, batch_id)
    rows = []
    for instrument, agreed in (terms.get("instruments") or {}).items():
        seen = observed.get(instrument)
        row = {"instrument": instrument, "samples": seen["samples"] if seen else 0}
        if "flat_paise" in agreed:
            row["contracted"] = f"flat {agreed['flat_paise']}p"
            row["contracted_value"] = agreed["flat_paise"]
        else:
            row["contracted"] = f"{agreed['rate'] * 100:g}%"
            row["contracted_value"] = agreed["rate"]

        if seen is None:
            row.update(observed="no data", agrees=None, deviation=None)
        elif seen["shape"] == "flat":
            row.update(observed=f"flat {seen['flat_paise']}p",
                       observed_value=seen["flat_paise"])
        else:
            row.update(observed=f"{seen['rate'] * 100:.4g}%",
                       observed_value=seen["rate"])

        if seen is not None:
            zero_both = (row["contracted_value"] == 0
                         and row.get("observed_value", 1) == 0)
            same_shape = zero_both or (("flat_paise" in agreed)
                                       == (seen["shape"] == "flat"))
            row["deviation"] = (0 if zero_both else
                                row["observed_value"] - row["contracted_value"]
                                if same_shape else None)
            # a hair of tolerance for rounding drift in the estimate
            row["agrees"] = bool(same_shape and abs(row["deviation"])
                                 <= (2 if "flat_paise" in agreed else 1e-4))
        rows.append(row)
    return rows


def leakage_report(conn, batch_id=None, cfg=None) -> dict:
    """Transaction by transaction: fee charged vs fee agreed.

    This is the number a finance team actually wants. Not "did the records
    match" -- they can match perfectly while you are being overcharged on every
    one of them -- but "how much was taken that the contract did not allow".
    """
    terms = contract_terms(cfg)
    noise = (cfg or load()).get("rule_engine", {}).get("noise_band_paise", 5)
    sql = ("SELECT s.settlement_txn_id, s.batch_id, s.instrument,"
           " s.gross_amount_paise, s.net_amount_paise, s.order_id_claimed"
           " FROM settlements s JOIN orders o ON o.order_id = s.order_id_claimed"
           " WHERE o.status = 'captured' AND s.gross_amount_paise > 0"
           " AND s.order_id_claimed IN (SELECT order_id_claimed FROM settlements"
           "   GROUP BY order_id_claimed, batch_id HAVING COUNT(*) = 1)")
    params = []
    if batch_id is not None:
        sql += " AND s.batch_id = ?"
        params.append(batch_id)

    per_instrument = defaultdict(lambda: {"transactions": 0, "leaked_paise": 0,
                                          "charged_paise": 0, "agreed_paise": 0})
    worst, total, n_over = [], 0, 0
    for r in conn.execute(sql, params):
        agreed = contract_fee(int(r["gross_amount_paise"]), r["instrument"], terms)
        if agreed is None:
            continue
        charged = int(r["gross_amount_paise"]) - int(r["net_amount_paise"])
        diff = charged - agreed
        if abs(diff) <= noise:
            diff = 0            # paise-level drift, not a contractual breach

        b = per_instrument[r["instrument"]]
        b["transactions"] += 1
        b["charged_paise"] += charged
        b["agreed_paise"] += agreed
        b["leaked_paise"] += diff
        total += diff
        if diff > 0:
            n_over += 1
            worst.append({"settlement_txn_id": r["settlement_txn_id"],
                          "order_id": r["order_id_claimed"],
                          "instrument": r["instrument"],
                          "gross_paise": int(r["gross_amount_paise"]),
                          "charged_paise": charged, "agreed_paise": agreed,
                          "leaked_paise": diff})

    worst.sort(key=lambda x: -x["leaked_paise"])
    return {
        "batch_id": batch_id,
        "total_leaked_paise": total,
        "total_leaked": format_paise(total),
        "transactions_overcharged": n_over,
        "transactions_checked": sum(v["transactions"] for v in per_instrument.values()),
        "by_instrument": {k: dict(v, leaked=format_paise(v["leaked_paise"]))
                          for k, v in per_instrument.items()},
        "worst_offenders": worst[:10],
    }


def render(conn, batch_id=None) -> str:
    """Human-readable, for the CLI."""
    lines = ["CONTRACT COMPLIANCE", "=" * 78,
             f"{'instrument':<14}{'contracted':>14}{'observed':>14}"
             f"{'n':>6}  verdict"]
    for row in compare_to_contract(conn, batch_id):
        verdict = ("matches contract" if row["agrees"]
                   else "no data" if row["agrees"] is None
                   else ">>> DEVIATES FROM CONTRACT")
        lines.append(f"{row['instrument']:<14}{row['contracted']:>14}"
                     f"{row.get('observed', '-'):>14}{row['samples']:>6}  {verdict}")

    lk = leakage_report(conn, batch_id)
    lines += ["", f"Fee leakage across {lk['transactions_checked']} transactions:"
                  f"  {lk['total_leaked']}",
              f"  {lk['transactions_overcharged']} transactions charged more "
              f"than the contract allows"]
    for instrument, b in sorted(lk["by_instrument"].items(),
                                key=lambda kv: -kv[1]["leaked_paise"]):
        if b["leaked_paise"]:
            lines.append(f"    {instrument:<14}{b['leaked']:>14}"
                         f"   over {b['transactions']} transactions")
    if lk["worst_offenders"]:
        lines += ["", "  worst single transactions:"]
        for w in lk["worst_offenders"][:5]:
            lines.append(f"    {w['settlement_txn_id']:<16}{w['instrument']:<14}"
                         f"charged {format_paise(w['charged_paise'])}, "
                         f"agreed {format_paise(w['agreed_paise'])}, "
                         f"over by {format_paise(w['leaked_paise'])}")
    return "\n".join(lines)
