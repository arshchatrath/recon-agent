"""Scoring. THE ONLY MODULE PERMITTED TO READ THE GROUND-TRUTH FILES.

Everything else in src/ is forbidden from touching truth.csv or
adversarial_truth.csv, and a test greps for it. Scoring lives behind that wall
so that the pipeline cannot accidentally be marking its own homework.

The number that matters is not the match rate. It is the false-positive count.
A wrong match silently corrupts the books and is found months later by an
auditor; an open exception costs a controller two minutes. The cost-weighted
error score prices that at 50:1, and every design decision upstream, the
unmatched sink in the assignment solver, the ambiguity report in subset-sum,
the backtest gate, the model's licence to abstain, is that ratio expressed
in code.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from src.config import load
from src.db import DATA_DIR, get_conn
from src.money import format_paise

UNRESOLVED = {"UNSETTLED", "ORPHAN"}


def truth_path(batch_id: str) -> Path:
    if batch_id == "adversarial":
        return DATA_DIR / "adversarial" / "adversarial_truth.csv"
    return DATA_DIR / f"batch_{batch_id}" / "truth.csv"


def load_truth(batch_id: str) -> pd.DataFrame:
    df = pd.read_csv(truth_path(batch_id), keep_default_na=False)
    return df


def truth_pairs(truth: pd.DataFrame) -> tuple[set, set, dict]:
    """-> (order/settlement pairs, bank/settlement pairs, settlement -> trap)."""
    os_pairs, bank_pairs, traps = set(), set(), {}
    for r in truth.itertuples():
        stl = str(r.settlement_txn_id)
        if r.trap_type:
            traps[stl] = r.trap_type
        if stl in UNRESOLVED:
            continue
        if str(r.order_id) not in UNRESOLVED:
            os_pairs.add((str(r.order_id), stl))
        utr = str(r.utr)
        if utr and utr not in UNRESOLVED and utr != stl:
            bank_pairs.add((utr, stl))
    return os_pairs, bank_pairs, traps


def score_batch(conn, batch_id: str) -> dict:
    """Precision, recall and false positives for one batch, against truth."""
    truth = load_truth(batch_id)
    os_true, bank_true, traps = truth_pairs(truth)
    all_true = os_true | bank_true

    claimed, by_resolver, false_positives = set(), Counter(), []
    for m in conn.execute(
            "SELECT * FROM matches WHERE batch_id=?", (batch_id,)).fetchall():
        pair = (str(m["left_id"]), str(m["right_id"]))
        claimed.add(pair)
        by_resolver[m["resolved_by"]] += 1
        if pair not in all_true:
            false_positives.append({
                "match_id": m["match_id"], "left": m["left_id"],
                "right": m["right_id"], "resolved_by": m["resolved_by"],
                "match_kind": m["match_kind"], "confidence": m["confidence"],
                "trap_type": traps.get(str(m["right_id"]), ""),
                "money_paise": _money_of(conn, m["right_type"], m["right_id"]),
                "explanation": m["explanation"]})

    correct = len(claimed & all_true)
    precision = correct / len(claimed) if claimed else 0.0
    recall = correct / len(all_true) if all_true else 0.0

    exc = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(money_at_risk_paise),0) m FROM exceptions"
        " WHERE batch_id=? AND status='open'", (batch_id,)).fetchone()
    open_exceptions, exception_money = exc["n"], exc["m"]
    # a false positive is 100% at risk: the money is booked against the wrong
    # record and nobody is looking at it
    fp_money = sum(f["money_paise"] for f in false_positives)

    mcfg = load()["metrics"]
    cost_weighted = (len(false_positives) * mcfg["fp_cost_weight"]
                     + open_exceptions * mcfg["exception_cost_weight"])

    run = conn.execute(
        "SELECT * FROM run_metrics WHERE batch_id=? ORDER BY run_id DESC LIMIT 1",
        (batch_id,)).fetchone()
    total = run["total_records"] if run else 0

    return {
        "batch_id": batch_id,
        "total_records": total,
        "claimed_matches": len(claimed),
        "correct_matches": correct,
        "true_matches_available": len(all_true),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "match_rate": round(run["match_rate"], 4) if run else 0.0,
        "matches_by_resolver": dict(by_resolver),
        "false_positive_count": len(false_positives),
        "false_positives_by_trap": dict(Counter(
            f["trap_type"] or "untrapped" for f in false_positives)),
        "false_positives": false_positives,
        "open_exceptions": open_exceptions,
        "exception_money_paise": exception_money,
        "false_positive_money_paise": fp_money,
        "money_at_risk_paise": exception_money + fp_money,
        "cost_weighted_error": cost_weighted,
        "llm_calls": run["llm_calls"] if run else 0,
        "llm_calls_avoided": run["llm_calls_avoided"] if run else 0,
        # unrounded: rounding here and again for display turned 34.848 into
        # 34.85 and then 34.9, where the true one-decimal figure is 34.8
        "llm_calls_per_100_records": (run["llm_calls"] / total * 100)
        if run and total else 0.0,
        "llm_tokens_in": run["llm_tokens_in"] if run else 0,
        "llm_tokens_out": run["llm_tokens_out"] if run else 0,
        "active_rules": run["active_rules_count"] if run else 0,
        "wall_clock_seconds": round(run["wall_clock_seconds"], 3) if run else 0.0,
        "component_sizes": json.loads(run["component_sizes_json"] or "{}")
        if run else {},
        "fee_leakage_paise": (run["fee_leakage_paise"] or 0) if run else 0,
        "transactions_overcharged": (run["transactions_overcharged"] or 0)
        if run else 0,
        "contract_deviations": (run["contract_deviations"] or 0) if run else 0,
    }


def _money_of(conn, record_type, record_id) -> int:
    table, key, col = {
        "settlement": ("settlements", "settlement_txn_id", "net_amount_paise"),
        "order": ("orders", "order_id", "gross_amount_paise"),
        "bank_credit": ("bank_credits", "utr", "credit_amount_paise"),
    }.get(record_type, (None, None, None))
    if table is None:
        return 0
    row = conn.execute(f"SELECT {col} v FROM {table} WHERE {key}=?",
                       (record_id,)).fetchone()
    return abs(row["v"]) if row else 0


def persist(conn, scored: dict):
    """Write the truth-derived numbers back onto the run_metrics row."""
    conn.execute(
        "UPDATE run_metrics SET precision_score=?, recall_score=?,"
        " false_positive_count=?, cost_weighted_error=?, money_at_risk_paise=?"
        " WHERE run_id = (SELECT run_id FROM run_metrics WHERE batch_id=?"
        " ORDER BY run_id DESC LIMIT 1)",
        (scored["precision"], scored["recall"], scored["false_positive_count"],
         scored["cost_weighted_error"], scored["money_at_risk_paise"],
         scored["batch_id"]))
    conn.commit()


def rule_activity(conn) -> dict:
    events = Counter(r["event"] for r in conn.execute(
        "SELECT event FROM rule_audit"))
    return {"proposed": events["proposed"], "promoted": events["promoted"],
            "rejected": events["rejected"], "retired": events["retired"],
            "active": conn.execute("SELECT COUNT(*) c FROM rules WHERE"
                                   " status='active'").fetchone()["c"]}


def learning_curve(conn, batches=None) -> list[dict]:
    """The headline artifact. One row per batch: the numbers that should be
    falling (exceptions, LLM calls, cost-weighted error) and the ones that
    should be rising (match rate, active rules), with false positives ideally
    flat at zero throughout."""
    if batches is None:
        batches = [r["batch_id"] for r in conn.execute(
            "SELECT DISTINCT batch_id FROM run_metrics WHERE batch_id"
            " NOT IN ('adversarial') ORDER BY CAST(batch_id AS INTEGER)")]
    out = []
    for b in batches:
        s = score_batch(conn, b)
        out.append({k: s[k] for k in (
            "batch_id", "open_exceptions", "llm_calls",
            "llm_calls_per_100_records", "llm_calls_avoided", "match_rate",
            "precision", "recall", "false_positive_count", "active_rules",
            "cost_weighted_error", "money_at_risk_paise",
            "fee_leakage_paise", "contract_deviations")})
    return out


# ------------------------------------------------------------------ report
def report(conn, batches=None) -> str:
    lines = []
    # the adversarial set is a trap set, not a step on the learning curve
    curve = learning_curve(conn, batches and [b for b in batches
                                              if b != "adversarial"])
    if curve:
        lines += ["LEARNING CURVE", "=" * 78,
                  f"{'batch':>6} {'excep':>6} {'llm':>5} {'/100rec':>8} "
                  f"{'avoided':>8} {'match':>7} {'prec':>7} {'recall':>7} "
                  f"{'FP':>4} {'rules':>6} {'cost':>6}"]
        for r in curve:
            lines.append(
                f"{r['batch_id']:>6} {r['open_exceptions']:>6} {r['llm_calls']:>5} "
                f"{r['llm_calls_per_100_records']:>8.1f} "
                f"{r['llm_calls_avoided']:>8} {r['match_rate']:>7.1%} "
                f"{r['precision']:>7.1%} {r['recall']:>7.1%} "
                f"{r['false_positive_count']:>4} {r['active_rules']:>6} "
                f"{r['cost_weighted_error']:>6.0f}")
        first, last = curve[0], curve[-1]
        lines += ["", f"batch {first['batch_id']} -> {last['batch_id']}: "
                      f"exceptions {first['open_exceptions']} -> "
                      f"{last['open_exceptions']}, "
                      f"LLM calls {first['llm_calls']} -> {last['llm_calls']}, "
                      f"match rate {first['match_rate']:.1%} -> "
                      f"{last['match_rate']:.1%}, "
                      f"false positives {first['false_positive_count']} -> "
                      f"{last['false_positive_count']}"]

    for b in (batches or [r["batch_id"] for r in conn.execute(
            "SELECT DISTINCT batch_id FROM run_metrics")]):
        s = score_batch(conn, b)
        lines += ["", f"BATCH {b}", "-" * 78,
                  f"  records {s['total_records']}   claimed {s['claimed_matches']}"
                  f"   correct {s['correct_matches']}"
                  f"   of {s['true_matches_available']} true",
                  f"  precision {s['precision']:.1%}   recall {s['recall']:.1%}"
                  f"   match rate {s['match_rate']:.1%}",
                  f"  by resolver: {s['matches_by_resolver']}",
                  f"  open exceptions {s['open_exceptions']} "
                  f"({format_paise(s['exception_money_paise'])})",
                  f"  FALSE POSITIVES {s['false_positive_count']} "
                  f"({format_paise(s['false_positive_money_paise'])})",
                  f"  money at risk {format_paise(s['money_at_risk_paise'])}",
                  f"  fee leakage {format_paise(s['fee_leakage_paise'])}"
                  f"   ({s['transactions_overcharged']} txns overcharged,"
                  f" {s['contract_deviations']} instruments deviating)",
                  f"  cost-weighted error {s['cost_weighted_error']:.0f}"
                  f"  (FP:exception = "
                  f"{load()['metrics']['fp_cost_weight']}:1)",
                  f"  component sizes {s['component_sizes']}"]
        if s["false_positive_count"]:
            lines.append(f"  by trap: {s['false_positives_by_trap']}")
            for f in s["false_positives"][:5]:
                lines.append(f"    {f['left']} -> {f['right']} "
                             f"[{f['resolved_by']}] trap={f['trap_type'] or '-'} "
                             f"{format_paise(f['money_paise'])}")

    act = rule_activity(conn)
    lines += ["", "RULE LIBRARY", "-" * 78,
              f"  proposed {act['proposed']}  promoted {act['promoted']}  "
              f"rejected {act['rejected']}  retired {act['retired']}  "
              f"active {act['active']}"]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="score the reconciliation run")
    p.add_argument("--report", action="store_true")
    p.add_argument("--batch", action="append",
                   help="restrict to these batches (repeatable)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--contract", action="store_true",
                   help="audit settlement fees against the contracted rates")
    a = p.parse_args(argv)

    conn = get_conn()
    if a.contract:
        from src.contract import render
        for b in (a.batch or [r["batch_id"] for r in conn.execute(
                "SELECT DISTINCT batch_id FROM run_metrics ORDER BY batch_id")]):
            print(f"\n### batch {b}\n")
            print(render(conn, b))
        return
    batches = a.batch or [r["batch_id"] for r in conn.execute(
        "SELECT DISTINCT batch_id FROM run_metrics ORDER BY batch_id")]
    for b in batches:
        persist(conn, score_batch(conn, b))
    if a.json:
        print(json.dumps({"learning_curve": learning_curve(conn),
                          "batches": [score_batch(conn, b) for b in batches],
                          "rules": rule_activity(conn)},
                         indent=2, default=str))
    else:
        print(report(conn, batches))


if __name__ == "__main__":
    main()
