"""Phase 5. The gate is the product, so most of these tests are about things
NOT being promoted."""
import json

import pytest

from src.db import ingest_batch, reset_db
from src.deterministic import ALL, RuleSet, backtest
from src.generate_data import DEFAULT_LAG, GST_RATE, LAG_WORKING_DAYS, MDR
from src.llm_reasoner import LLMVerdict
from src.pipeline import BatchRun
from src.rule_engine import (check_gate, intake, plain_english, process_proposals,
                             retire_stale_rules, review_pending)


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "r.db")
    yield c
    c.close()


def fee(instrument="CARD_CREDIT", rate=0.03, gst=0.2, tol=2):
    return {"type": "fee_formula", "instrument": instrument,
            "params": {"rate": rate, "gst": gst}, "tolerance_paise": tol}


def timing(instrument="UPI", lo=1, hi=1):
    return {"type": "timing_window", "instrument": instrument,
            "min_working_days": lo, "max_working_days": hi}


def true_fee(instrument):
    """The rule the system is meant to induce, built from the generator's own
    constants so the test cannot drift from the data."""
    kind, v = MDR[instrument]
    params = {"flat_paise": int(v)} if kind == "flat" else {"rate": v}
    params["gst"] = GST_RATE
    return {"type": "fee_formula", "instrument": instrument, "params": params,
            "tolerance_paise": 3}


def propose(conn, pred, times=3, confidence=0.9, batch="1"):
    for i in range(times):
        out = intake(conn, batch, pred, confidence, case_id=f"case-{i}")
    return out


def seed_matches(conn, instrument, limit=10, clean_only=True):
    """Record resolved order/settlement matches so the backtest has history."""
    sql = "SELECT * FROM settlements WHERE instrument=?"
    if clean_only:
        sql += (" AND settlement_txn_id IN (SELECT settlement_txn_id FROM"
                " settlements WHERE net_amount_paise = gross_amount_paise"
                " - mdr_paise - gst_on_mdr_paise)")
    for s in conn.execute(sql + " LIMIT ?", (instrument, limit)).fetchall():
        if conn.execute("SELECT 1 FROM orders WHERE order_id=?",
                        (s["order_id_claimed"],)).fetchone():
            conn.execute(
                "INSERT INTO matches (batch_id,left_type,left_id,right_type,"
                "right_id,match_kind,resolved_by) VALUES"
                " (?,'order',?,'settlement',?,'exact','deterministic')",
                (s["batch_id"], s["order_id_claimed"], s["settlement_txn_id"]))
    conn.commit()


# ----------------------------------------------------------------- intake
def test_identical_proposals_collapse_and_count_up(conn):
    out = propose(conn, fee(), times=3)
    assert out["occurrence_count"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM rule_proposals").fetchone()["c"] == 1


def test_near_identical_params_collapse_onto_one_fingerprint(conn):
    intake(conn, "1", fee(rate=0.03), 0.9)
    intake(conn, "1", fee(rate=0.030000001), 0.9)
    assert conn.execute("SELECT COUNT(*) c FROM rule_proposals").fetchone()["c"] == 1


def test_confidence_is_a_running_average(conn):
    intake(conn, "1", fee(), 1.0)
    out = intake(conn, "1", fee(), 0.0)
    assert out["avg_confidence"] == pytest.approx(0.5)


def test_an_unparseable_predicate_is_rejected_at_intake_not_at_apply(conn):
    out = intake(conn, "1", {"type": "fee_formula", "params": {"rate": 9.0}}, 0.99)
    assert out["status"] == "rejected" and "unparseable" in out["reason"]
    assert conn.execute("SELECT COUNT(*) c FROM rule_proposals").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM rule_audit WHERE event='rejected'"
                        ).fetchone()["c"] == 1


def test_a_proposal_duplicating_an_active_rule_is_rejected(conn):
    seeded = conn.execute("SELECT predicate_json p FROM rules WHERE"
                          " rule_type='exact_id'").fetchone()["p"]
    out = intake(conn, "1", json.loads(seeded), 0.99)
    assert out["status"] == "rejected" and "duplicates" in out["reason"]


def test_every_intake_is_audited(conn):
    propose(conn, fee(), times=2)
    assert conn.execute("SELECT COUNT(*) c FROM rule_audit WHERE event='proposed'"
                        ).fetchone()["c"] == 2


# ------------------------------------------------------------------- gates
def test_too_few_occurrences_stays_pending_rather_than_rejected(conn):
    """Not enough evidence yet is a different thing from proven wrong."""
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI")
    intake(conn, "1", true_fee("UPI"), 0.95)
    out = review_pending(conn)
    assert out["promoted"] == [] and out["rejected"] == []
    assert len(out["pending"]) == 1


def test_low_confidence_blocks_promotion(conn):
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI")
    propose(conn, true_fee("UPI"), times=5, confidence=0.4)
    assert review_pending(conn)["promoted"] == []


def test_a_correct_rule_with_enough_evidence_is_promoted(conn):
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10)
    propose(conn, true_fee("UPI"), times=3, confidence=0.9)
    out = review_pending(conn)
    assert len(out["promoted"]) == 1
    rule = conn.execute("SELECT * FROM rules WHERE rule_id=?",
                        (out["promoted"][0],)).fetchone()
    assert rule["status"] == "active" and rule["promoted_at"]
    assert rule["promoted_from_proposal_id"]


def test_the_backtest_rejects_a_rule_that_contradicts_history(conn):
    """The headline failure case: a fee rate that fits one case and breaks the
    records we have already resolved."""
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10)
    propose(conn, fee(instrument="UPI", rate=0.02, gst=0.18), times=5,
            confidence=0.99)
    out = review_pending(conn)
    assert out["promoted"] == [] and len(out["rejected"]) == 1

    audit = conn.execute("SELECT detail_json FROM rule_audit WHERE"
                         " event='rejected' ORDER BY audit_id DESC").fetchone()
    detail = json.loads(audit["detail_json"])
    assert "backtest" in detail["failed_gates"]
    assert detail["backtest"]["wrong_matches"] > 0
    assert detail["backtest"]["counterexamples"], "must record what broke it"


def test_insufficient_backtest_support_blocks_promotion(conn):
    """A rule nothing in history exercises is unproven, not proven safe -- and
    not proven WRONG either, so it stays pending rather than being rejected.
    Rejecting it would be permanent: a refund rule proposed before any fee rule
    exists has no expected net to compare against, and would be killed off
    before the evidence it needs could ever arrive."""
    ingest_batch(conn, "1")
    propose(conn, true_fee("UPI"), times=5, confidence=0.99)
    out = review_pending(conn)
    assert out["promoted"] == []
    assert out["rejected"] == []
    assert len(out["pending"]) == 1

    passed, report = check_gate(conn, conn.execute(
        "SELECT * FROM rule_proposals").fetchone())
    assert not passed
    assert report["gates"]["backtest"]["support"] == 0
    assert report["gates"]["backtest"]["insufficient_support"] is True


def test_a_rule_rejected_for_lack_of_evidence_can_still_be_promoted_later(conn):
    """The regression: early rejection must not be a death sentence."""
    ingest_batch(conn, "1")
    propose(conn, true_fee("UPI"), times=3, confidence=0.95)
    assert review_pending(conn)["promoted"] == []      # no history yet

    seed_matches(conn, "UPI", limit=10)                # evidence arrives
    out = review_pending(conn)
    assert len(out["promoted"]) == 1, "must be reconsidered once evidence exists"


def test_a_rule_overlapping_an_active_one_is_rejected(conn):
    """Two timing windows for the same instrument that could both fire on one
    record. (Fee formulas differing only in tolerance are now merged as one
    hypothesis, so they cannot reach this gate -- see the tolerance tests.)"""
    ingest_batch(conn, "1")
    conn.execute("INSERT INTO rules (rule_type,scope_instrument,predicate_json,"
                 "priority,status,promoted_at) VALUES"
                 " ('timing_window','UPI',?,50,'active',datetime('now'))",
                 (json.dumps(timing("UPI", 1, 3)),))
    conn.commit()
    propose(conn, timing("UPI", 2, 4), times=5, confidence=0.99)
    out = review_pending(conn)
    detail = json.loads(conn.execute(
        "SELECT detail_json FROM rule_audit WHERE event='rejected'"
        " ORDER BY audit_id DESC").fetchone()["detail_json"])
    assert out["promoted"] == []
    assert detail["gates"]["conflict"]["passed"] is False


def test_the_promotion_audit_records_the_full_backtest(conn):
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10)
    propose(conn, true_fee("UPI"), times=3, confidence=0.9)
    rid = review_pending(conn)["promoted"][0]
    detail = json.loads(conn.execute(
        "SELECT detail_json FROM rule_audit WHERE event='promoted'"
        " AND rule_id=?", (rid,)).fetchone()["detail_json"])
    assert detail["backtest"]["correct_matches"] > 0
    assert detail["gates"]["occurrence"]["passed"]


# -------------------------------------------------------------- retirement
def test_a_rule_that_stops_being_right_is_retired_and_rolled_back(conn):
    ingest_batch(conn, "1")
    conn.execute("INSERT INTO rules (rule_id,rule_type,scope_instrument,"
                 "predicate_json,priority,status,times_applied,times_correct)"
                 " VALUES (99,'fee_formula','UPI',?,50,'active',20,10)",
                 (json.dumps(fee(instrument="UPI")),))
    s = conn.execute("SELECT * FROM settlements LIMIT 3").fetchall()
    for row in s:
        conn.execute("INSERT INTO matches (batch_id,left_type,left_id,right_type,"
                     "right_id,match_kind,resolved_by,rule_id) VALUES"
                     " ('1','order',?,'settlement',?,'rule','deterministic',99)",
                     (row["order_id_claimed"], row["settlement_txn_id"]))
    conn.commit()

    assert retire_stale_rules(conn) == [99]
    assert conn.execute("SELECT status FROM rules WHERE rule_id=99"
                        ).fetchone()["status"] == "retired"
    # every match it produced is withdrawn and re-opened
    assert conn.execute("SELECT COUNT(*) c FROM matches WHERE rule_id=99"
                        ).fetchone()["c"] == 0
    reopened = conn.execute("SELECT * FROM exceptions WHERE"
                            " reason_code='RULE_RETIRED'").fetchall()
    assert len(reopened) == 3
    assert all(r["money_at_risk_paise"] > 0 for r in reopened)


def test_a_healthy_rule_is_not_retired(conn):
    conn.execute("INSERT INTO rules (rule_id,rule_type,scope_instrument,"
                 "predicate_json,priority,status,times_applied,times_correct)"
                 " VALUES (98,'fee_formula','UPI',?,50,'active',20,20)",
                 (json.dumps(fee(instrument="UPI")),))
    conn.commit()
    assert retire_stale_rules(conn) == []


def test_a_rule_with_too_few_applications_is_not_judged_yet(conn):
    conn.execute("INSERT INTO rules (rule_id,rule_type,scope_instrument,"
                 "predicate_json,priority,status,times_applied,times_correct)"
                 " VALUES (97,'fee_formula','UPI',?,50,'active',3,0)",
                 (json.dumps(fee(instrument="UPI")),))
    conn.commit()
    assert retire_stale_rules(conn) == []


# ------------------------------------------------------- the whole loop
class Proposer:
    """A stand-in reasoner that proposes the true economics for whatever
    instrument it is shown -- i.e. a model doing its job. It is never told the
    answer for an instrument it has not seen a case for."""

    def __init__(self):
        self.calls = self.calls_avoided = 0
        self.tokens_in = self.tokens_out = 0

    def resolve(self, case):
        self.calls += 1
        self.tokens_in += 400
        self.tokens_out += 150
        instr = case["record"].get("instrument")
        # This double only knows about fees. Discovery cases (which carry a
        # "focus" and may be library-wide) are answered by Observer in
        # tests/test_discovery.py.
        if case.get("focus") or instr not in MDR:
            return LLMVerdict(confidence=0.1)
        return LLMVerdict(verdict="insufficient_information", confidence=0.9,
                          proposed_rule=true_fee(instr),
                          residual_explanation="the deduction looks like a fee")


def test_four_batches_induce_the_fee_structure_without_being_told(conn):
    """The Phase 5 acceptance criterion. Nothing anywhere in the pipeline is
    given MDR rates; they arrive only as proposals that survive the gate."""
    proposer = Proposer()
    per_batch = []
    for b in ("1", "2", "3", "4"):
        ingest_batch(conn, b)
        run = BatchRun(conn, b, reasoner=proposer, use_llm=True)
        per_batch.append(run.run())

    active = conn.execute(
        "SELECT scope_instrument, predicate_json FROM rules"
        " WHERE status='active' AND rule_type='fee_formula'").fetchall()
    learned = {r["scope_instrument"] for r in active}
    assert "UPI" in learned, "the zero-MDR UPI rule must be induced"
    assert learned & {"CARD_CREDIT", "CARD_DEBIT"}, "a card fee rule must be induced"

    # and the learning must actually pay off
    assert per_batch[-1]["exceptions"] < per_batch[0]["exceptions"]
    assert per_batch[-1]["match_rate"] > per_batch[0]["match_rate"]
    assert per_batch[-1]["active_rules"] > per_batch[0]["active_rules"]


def test_the_induced_upi_rule_is_actually_zero_fee(conn):
    proposer = Proposer()
    for b in ("1", "2", "3", "4"):
        ingest_batch(conn, b)
        BatchRun(conn, b, reasoner=proposer, use_llm=True).run()
    rules = RuleSet.load(conn)
    # a 1,00,000 paise UPI order should be expected to settle at 1,00,000
    assert rules.expected_net(100_000, "UPI") == 100_000


def test_a_bad_proposal_is_rejected_while_good_ones_are_promoted(conn):
    """Both outcomes in one run -- the audit log has to show the gate working
    in both directions."""
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10)
    propose(conn, true_fee("UPI"), times=3, confidence=0.9)
    propose(conn, fee(instrument="UPI", rate=0.02, gst=0.18), times=3,
            confidence=0.95)
    out = review_pending(conn)
    assert len(out["promoted"]) == 1 and len(out["rejected"]) == 1


def test_process_proposals_is_the_single_entry_point(conn):
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10)
    proposals = [{"predicate": true_fee("UPI"), "confidence": 0.9,
                  "case_id": f"c{i}"} for i in range(3)]
    out = process_proposals(conn, "1", proposals)
    assert len(out["promoted"]) == 1
    assert out["retired"] == []


# --------------------------------------------------------- plain english
def test_learned_rules_render_in_plain_english():
    assert "2%" in plain_english(fee(rate=0.02, gst=0.18))
    assert "at par" in plain_english(fee(instrument="UPI", rate=0.0, gst=0.0))
    assert "GST on that fee" in plain_english(fee())
    assert "1 working day" in plain_english(timing())
    assert "1-2 working days" in plain_english(timing(lo=1, hi=2))
    assert "flat 1200 paise" in plain_english(
        {"type": "fee_formula", "instrument": "NETBANKING",
         "params": {"flat_paise": 1200, "gst": 0.18}})


# ------------------------------------------- tolerance is not part of the claim
def test_proposals_differing_only_in_tolerance_are_one_hypothesis(conn):
    """Observed live: the model converged on the right fee formula but kept
    refining tolerance_paise, so occurrences split across tol=0/1/2/3 variants
    and none ever reached MIN_OCCURRENCES. The claim is the formula."""
    for tol in (0, 1, 2, 3):
        intake(conn, "1", dict(true_fee("CARD_DEBIT"), tolerance_paise=tol), 0.9)
    conn.commit()
    rows = conn.execute("SELECT * FROM rule_proposals").fetchall()
    assert len(rows) == 1, "must collapse to a single proposal"
    assert rows[0]["occurrence_count"] == 4


def test_the_widest_proposed_tolerance_wins(conn):
    for tol in (0, 7, 2):
        intake(conn, "1", dict(true_fee("UPI"), tolerance_paise=tol), 0.9)
    conn.commit()
    pred = json.loads(conn.execute(
        "SELECT predicate_json p FROM rule_proposals").fetchone()["p"])
    assert pred["tolerance_paise"] == 7


def test_a_runaway_tolerance_is_capped(conn):
    from src.config import load
    cap = load()["rule_engine"]["max_tolerance_paise"]
    intake(conn, "1", dict(true_fee("UPI"), tolerance_paise=0), 0.9)
    intake(conn, "1", dict(true_fee("UPI"), tolerance_paise=999_999), 0.9)
    conn.commit()
    pred = json.loads(conn.execute(
        "SELECT predicate_json p FROM rule_proposals").fetchone()["p"])
    assert pred["tolerance_paise"] == cap


def test_merging_lets_a_converging_model_actually_promote(conn):
    """End to end: the exact proposal sequence the live model produced."""
    ingest_batch(conn, "1")
    seed_matches(conn, "CARD_DEBIT", limit=10)
    for tol in (0, 1, 2, 3):
        intake(conn, "1", dict(true_fee("CARD_DEBIT"), tolerance_paise=tol), 0.95)
    conn.commit()
    out = review_pending(conn)
    assert len(out["promoted"]) == 1, out


def test_different_formulas_still_stay_separate(conn):
    """Merging must not collapse genuinely different claims."""
    intake(conn, "1", fee(instrument="UPI", rate=0.02), 0.9)
    intake(conn, "1", fee(instrument="UPI", rate=0.03), 0.9)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM rule_proposals").fetchone()["c"] == 2


# ------------------------------------------- noise in the data vs a wrong rule
def test_a_correct_rule_contradicted_only_by_rounding_drift_is_promoted(conn):
    """Both look like a contradicted record. Counting them the same rejected
    four correct fee formulas that a live model had induced, each missed by a
    paise or two on one drifted row."""
    ingest_batch(conn, "1")
    seed_matches(conn, "CARD_DEBIT", limit=12, clean_only=False)
    strict = dict(true_fee("CARD_DEBIT"), tolerance_paise=0)
    bt = backtest(conn, strict)
    assert bt["wrong_matches"] > 0, "this test needs a drifted row to be real"
    assert bt["max_deviation_paise"] <= 5

    propose(conn, strict, times=3, confidence=0.95)
    out = review_pending(conn)
    assert len(out["promoted"]) == 1, "drift must not block a correct rule"
    detail = json.loads(conn.execute(
        "SELECT detail_json FROM rule_audit WHERE event='promoted'"
        " ORDER BY audit_id DESC").fetchone()["detail_json"])
    assert detail["gates"]["backtest"]["contradictions_within_noise"] is True


def test_a_wrong_rate_is_still_rejected_however_small_its_error_count(conn):
    """The escape hatch must be a noise band, not a loophole."""
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=12, clean_only=False)
    wrong = {"type": "fee_formula", "instrument": "UPI",
             "params": {"rate": 0.025, "gst": 0.18}, "tolerance_paise": 0}
    bt = backtest(conn, wrong)
    assert bt["max_deviation_paise"] > 1000, "a wrong rate misses by a lot"
    propose(conn, wrong, times=5, confidence=0.99)
    out = review_pending(conn)
    assert out["promoted"] == []
    detail = json.loads(conn.execute(
        "SELECT detail_json FROM rule_audit WHERE event='rejected'"
        " ORDER BY audit_id DESC").fetchone()["detail_json"])
    assert detail["gates"]["backtest"]["contradictions_within_noise"] is False


def test_the_backtest_reports_how_wrong_not_merely_that_it_is_wrong(conn):
    ingest_batch(conn, "1")
    seed_matches(conn, "UPI", limit=10, clean_only=False)
    bt = backtest(conn, {"type": "fee_formula", "instrument": "UPI",
                         "params": {"rate": 0.05, "gst": 0.18},
                         "tolerance_paise": 0})
    assert bt["max_deviation_paise"] is not None
    assert all("deviation_paise" in c for c in bt["counterexamples"])
