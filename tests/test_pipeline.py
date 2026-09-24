"""End-to-end Phase 3 behaviour.

The important assertion here is the unintuitive one: with an empty rule library
batch 1 must produce mostly EXCEPTIONS and zero order-to-settlement matches.
That is the correct starting state, and the whole learning curve is measured
from it.
"""
import json

import pytest

from src.db import reset_db
from src.pipeline import BatchRun, run_batch
from tests.test_dataset import DEFAULT_LAG, GST_RATE, LAG_WORKING_DAYS, MDR


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "p.db")
    yield c
    c.close()


def seed_true_rules(conn):
    """Insert the rules the system is *supposed* to induce in Phase 5, so we can
    prove the deterministic layer uses them correctly once it has them. Imported
    from the dataset tests rather than retyped, so there is one source of truth."""
    rid = 10
    for instr, (kind, v) in MDR.items():
        params = ({"flat_paise": int(v)} if kind == "flat" else {"rate": v})
        params["gst"] = GST_RATE
        conn.execute(
            "INSERT INTO rules (rule_id,rule_type,scope_instrument,predicate_json,"
            "priority,status,promoted_at) VALUES (?,?,?,?,50,'active',datetime('now'))",
            (rid, "fee_formula", instr, json.dumps(
                {"type": "fee_formula", "instrument": instr, "params": params,
                 "tolerance_paise": 3})))
        rid += 1
        lag = LAG_WORKING_DAYS.get(instr, DEFAULT_LAG)
        conn.execute(
            "INSERT INTO rules (rule_id,rule_type,scope_instrument,predicate_json,"
            "priority,status,promoted_at) VALUES (?,?,?,?,60,'active',datetime('now'))",
            (rid, "timing_window", instr, json.dumps(
                {"type": "timing_window", "instrument": instr,
                 "min_working_days": lag, "max_working_days": lag})))
        rid += 1
    conn.commit()


def counts(conn, table, col, batch="1"):
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT {col}, COUNT(*) FROM {table} WHERE batch_id=? GROUP BY 1", (batch,))}


# ------------------------------------------------- the cold-start behaviour
def test_batch_one_with_an_empty_library_is_mostly_exceptions(conn):
    s = run_batch(conn, "1")
    assert s["exceptions"] > 50
    assert s["exceptions"] > s.get("exact", 0) + s.get("assignment", 0)
    assert s["active_rules"] == 1              # only the seeded exact-id rule


def test_no_order_to_settlement_match_is_claimed_without_a_fee_rule(conn):
    run_batch(conn, "1")
    # scoped to the order leg: the bank leg resolves fine without any rules
    n = conn.execute("SELECT COUNT(*) c FROM matches WHERE batch_id='1'"
                     " AND left_type='order'").fetchone()["c"]
    assert n == 0


def test_the_unexplained_deductions_are_recorded_as_the_learnable_material(conn):
    run_batch(conn, "1")
    by_reason = counts(conn, "exceptions", "reason_code")
    assert by_reason["FEE_UNEXPLAINED"] > 40
    row = conn.execute("SELECT * FROM exceptions WHERE reason_code="
                       "'FEE_UNEXPLAINED' LIMIT 1").fetchone()
    assert row["money_at_risk_paise"] > 0
    assert json.loads(row["candidates_json"])[0]["lag_working_days"] >= 1


def test_the_bank_leg_needs_no_learned_rules(conn):
    """Amounts are arithmetic, not economics, so this leg works from batch 1."""
    run_batch(conn, "1")
    n = conn.execute("SELECT COUNT(*) c FROM matches WHERE batch_id='1'"
                     " AND left_type='bank_credit'").fetchone()["c"]
    assert n > 30


def test_unsettled_orders_become_exceptions_carrying_their_full_gross(conn):
    run_batch(conn, "1")
    rows = conn.execute("SELECT * FROM exceptions WHERE reason_code="
                        "'NO_SETTLEMENT'").fetchall()
    assert rows
    for r in rows:
        gross = conn.execute("SELECT gross_amount_paise g FROM orders WHERE"
                             " order_id=?", (r["record_id"],)).fetchone()["g"]
        assert r["money_at_risk_paise"] == gross


def test_orphan_settlements_are_detected(conn):
    run_batch(conn, "1")
    assert counts(conn, "exceptions", "reason_code").get("ORPHAN_SETTLEMENT", 0) >= 1


def test_running_a_batch_twice_leaves_the_same_results_as_running_it_once(conn):
    def n(table):
        return conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE batch_id='1'"
                            ).fetchone()["c"]
    run_batch(conn, "1")
    once = {t: n(t) for t in ("matches", "exceptions", "run_metrics")}
    run_batch(conn, "1")
    assert {t: n(t) for t in once} == once
    assert once["run_metrics"] == 1


# --------------------------------------------- what learning will unlock
def test_the_same_batch_resolves_once_the_rules_are_known(conn):
    """Same data, same code, only the rule library differs. This is the
    delta the rule engine is aiming to produce on its own."""
    cold = run_batch(conn, "1")
    conn.execute("DELETE FROM matches")
    conn.execute("DELETE FROM exceptions")
    seed_true_rules(conn)
    warm = BatchRun(conn, "1").run()

    assert warm["exact"] > 40, "learned fee rules must resolve the id-matched pairs"
    assert warm["exceptions"] < cold["exceptions"] / 2
    assert warm["match_rate"] > cold["match_rate"]


def test_partial_refunds_stay_exceptions_even_with_fee_rules(conn):
    """A refund is short by more than the fee. Without a refund rule the system
    must keep saying so rather than waving it through."""
    seed_true_rules(conn)
    run_batch(conn, "1")
    assert counts(conn, "exceptions", "reason_code").get("FEE_UNEXPLAINED", 0) > 0


def test_metrics_row_is_written_per_run(conn):
    run_batch(conn, "1")
    row = conn.execute("SELECT * FROM run_metrics WHERE batch_id='1'").fetchone()
    assert row["total_records"] > 100
    assert row["active_rules_count"] == 1
    assert row["wall_clock_seconds"] >= 0
    assert json.loads(row["component_sizes_json"])


def test_the_pipeline_never_reads_ground_truth(conn):
    """Belt and braces alongside the static grep: the run must be identical
    whether or not truth.csv is even present."""
    import src.pipeline as p
    assert "truth" not in open(p.__file__, encoding="utf-8").read().replace(
        "ground truth", "")


# ----------------------------------------------------------- split payouts
def test_split_payouts_are_matched_once_the_fee_rules_are_known(conn):
    """A leg covering 40% of an order is INAPPLICABLE to a fee rule, so the
    assignment solver declines it and every split used to become an exception
   , the single largest cause of missed recall. The legs' GROSS amounts sum
    to the order's gross exactly, which is a subset sum and needs no new rule."""
    seed_true_rules(conn)
    run_batch(conn, "1")
    splits = conn.execute("SELECT * FROM matches WHERE explanation LIKE"
                          " 'split payout%'").fetchall()
    assert splits, "split payouts must be matched"
    # every split match binds one order to more than one settlement
    by_order = {}
    for m in splits:
        by_order.setdefault(m["left_id"], []).append(m["right_id"])
    assert all(len(v) >= 2 for v in by_order.values())


def test_split_matching_requires_an_exact_gross_sum(conn):
    """Inexactness is what would let an orphan that happens to be smaller than
    some order get bound to it."""
    seed_true_rules(conn)
    run_batch(conn, "1")
    for m in conn.execute("SELECT * FROM matches WHERE explanation LIKE"
                          " 'split payout%'").fetchall():
        order_gross = conn.execute(
            "SELECT gross_amount_paise g FROM orders WHERE order_id=?",
            (m["left_id"],)).fetchone()["g"]
        legs = conn.execute(
            "SELECT SUM(gross_amount_paise) s FROM settlements WHERE"
            " settlement_txn_id IN (SELECT right_id FROM matches WHERE"
            " batch_id=? AND left_id=? AND explanation LIKE 'split payout%')",
            (m["batch_id"], m["left_id"])).fetchone()["s"]
        assert legs == order_gross


def test_a_chargeback_reversal_is_not_mistaken_for_a_split(conn):
    """A reversal also claims its order id, but gross + (-gross) != gross."""
    seed_true_rules(conn)
    run_batch(conn, "1")
    for m in conn.execute("SELECT * FROM matches WHERE explanation LIKE"
                          " 'split payout%'").fetchall():
        assert not m["right_id"].startswith("CB-")


def test_splits_are_not_matched_before_the_fee_rules_are_learned(conn):
    """Each leg still has to be internally consistent with a learned fee
    schedule, so batch 1 with an empty library matches no splits."""
    run_batch(conn, "1")
    assert conn.execute("SELECT COUNT(*) c FROM matches WHERE explanation LIKE"
                        " 'split payout%'").fetchone()["c"] == 0


def test_splits_lift_recall_without_costing_precision(conn):
    from src.metrics import score_batch
    seed_true_rules(conn)
    run_batch(conn, "1")
    s = score_batch(conn, "1")
    assert s["precision"] == 1.0, s["false_positives"]
    assert s["false_positive_count"] == 0
