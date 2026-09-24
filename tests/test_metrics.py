"""Phase 6. Scoring is only worth anything if it is capable of reporting bad
news, so several of these deliberately inject a wrong match and check it is
counted."""
import json
from pathlib import Path

import pytest

from src.db import ingest_batch, reset_db
from src.metrics import (learning_curve, load_truth, persist, report,
                         rule_activity, score_batch, truth_pairs)
from src.pipeline import BatchRun
from tests.test_rule_engine import Proposer

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "m.db")
    yield c
    c.close()


@pytest.fixture
def scored(conn):
    for b in ("1", "2", "3", "4"):
        ingest_batch(conn, b)
        BatchRun(conn, b, reasoner=Proposer(), use_llm=True).run()
    return conn


# ------------------------------------------------------------ the truth wall
def test_only_metrics_reads_the_truth_files():
    """The static guard, restated here so the wall is asserted from both sides."""
    for mod in SRC.glob("*.py"):
        if mod.name == "metrics.py":
            continue
        text = mod.read_text(encoding="utf-8")
        for lit in ('truth.csv"', "truth.csv'", 'adversarial_truth"',
                    "adversarial_truth'"):
            assert lit not in text, f"{mod.name} reads ground truth"


def test_metrics_is_the_module_that_does_read_it():
    assert "truth.csv" in (SRC / "metrics.py").read_text(encoding="utf-8")


def test_truth_pairs_separates_the_two_legs_and_skips_unresolved():
    truth = load_truth("1")
    os_pairs, bank_pairs, traps = truth_pairs(truth)
    assert os_pairs and bank_pairs
    assert all("UNSETTLED" not in p for pair in os_pairs for p in pair)
    assert all(o.startswith("ORD-") for o, _ in os_pairs)
    assert all(u.startswith("UTR") for u, _ in bank_pairs)


def test_adversarial_traps_are_read_only_from_the_truth_file():
    _, _, traps = truth_pairs(load_truth("adversarial"))
    assert traps
    assert any("amount_twins" in t for t in traps.values())
    assert any("near_fee_trap" in t for t in traps.values())


# ----------------------------------------------------------------- scoring
def test_a_correct_run_scores_full_precision(scored):
    s = score_batch(scored, "1")
    assert s["precision"] == 1.0
    assert s["false_positive_count"] == 0
    assert s["correct_matches"] == s["claimed_matches"]


def test_an_injected_wrong_match_is_caught_as_a_false_positive(scored):
    """If this test cannot fail the run, the metric is decorative."""
    before = score_batch(scored, "1")["false_positive_count"]
    scored.execute(
        "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
        "match_kind,resolved_by,confidence) VALUES"
        " ('1','order','ORD-1-0000','settlement','STL-NOT-REAL','exact',"
        "'deterministic',0.99)")
    scored.commit()
    after = score_batch(scored, "1")
    assert after["false_positive_count"] == before + 1
    assert after["precision"] < 1.0
    assert after["false_positives"][0]["right"] == "STL-NOT-REAL"


def test_a_false_positive_puts_its_full_value_at_risk(scored):
    s = score_batch(scored, "1")
    stl = scored.execute("SELECT * FROM settlements WHERE batch_id='1'"
                         " LIMIT 1").fetchone()
    scored.execute(
        "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
        "match_kind,resolved_by) VALUES ('1','order','ORD-1-9999','settlement',"
        "?,'exact','deterministic')", (stl["settlement_txn_id"],))
    scored.commit()
    after = score_batch(scored, "1")
    assert after["false_positive_money_paise"] == abs(stl["net_amount_paise"])
    assert after["money_at_risk_paise"] > s["money_at_risk_paise"]


def test_recall_counts_the_true_matches_that_were_missed(scored):
    s = score_batch(scored, "1")
    assert 0 < s["recall"] < 1.0        # batch 1 cannot resolve everything
    assert s["correct_matches"] < s["true_matches_available"]


def test_matches_are_broken_down_by_resolver(scored):
    s = score_batch(scored, "2")
    assert set(s["matches_by_resolver"]) <= {
        "deterministic", "hungarian", "mincostflow", "subset_sum", "llm"}
    assert sum(s["matches_by_resolver"].values()) == s["claimed_matches"]


def test_the_cost_weighting_prices_a_false_positive_far_above_an_exception(scored):
    """One false positive must hurt more than forty-nine open exceptions."""
    base = score_batch(scored, "1")
    scored.execute(
        "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
        "match_kind,resolved_by) VALUES ('1','order','ORD-X','settlement',"
        "'STL-X','exact','deterministic')")
    scored.commit()
    with_fp = score_batch(scored, "1")
    assert with_fp["cost_weighted_error"] - base["cost_weighted_error"] == 50


def test_llm_calls_per_100_records_is_normalised(scored):
    s = score_batch(scored, "1")
    assert s["llm_calls_per_100_records"] == pytest.approx(
        s["llm_calls"] / s["total_records"] * 100, abs=0.02)


# ----------------------------------------------------------- learning curve
def test_the_learning_curve_has_a_row_per_batch(scored):
    curve = learning_curve(scored)
    assert [r["batch_id"] for r in curve] == ["1", "2", "3", "4"]


def test_exceptions_and_llm_calls_fall_while_match_rate_rises(scored):
    """The headline claim, asserted rather than eyeballed."""
    curve = learning_curve(scored)
    first, last = curve[0], curve[-1]
    assert last["open_exceptions"] < first["open_exceptions"]
    assert last["llm_calls"] < first["llm_calls"]
    assert last["llm_calls_per_100_records"] < first["llm_calls_per_100_records"]
    assert last["match_rate"] > first["match_rate"]
    assert last["active_rules"] > first["active_rules"]


def test_false_positives_stay_at_zero_across_the_run(scored):
    """If this ever fails, report the number, do not tune it away."""
    curve = learning_curve(scored)
    assert [r["false_positive_count"] for r in curve] == [0, 0, 0, 0], \
        f"false positives appeared: {curve}"


def test_precision_never_degrades_as_rules_accumulate(scored):
    """Learning must not be bought with wrong matches."""
    for row in learning_curve(scored):
        assert row["precision"] >= 0.98, row


def test_the_adversarial_set_produces_no_false_positives(conn):
    ingest_batch(conn, "adversarial")
    BatchRun(conn, "adversarial", reasoner=Proposer(), use_llm=True).run()
    s = score_batch(conn, "adversarial")
    assert s["false_positive_count"] == 0, s["false_positives"]
    assert s["false_positives_by_trap"] == {}


# ------------------------------------------------------------ persistence
def test_scores_are_written_back_to_run_metrics(scored):
    persist(scored, score_batch(scored, "1"))
    row = scored.execute("SELECT * FROM run_metrics WHERE batch_id='1'"
                         " ORDER BY run_id DESC LIMIT 1").fetchone()
    assert row["precision_score"] == 1.0
    assert row["false_positive_count"] == 0
    assert row["cost_weighted_error"] is not None


def test_rule_activity_counts_promotions_and_rejections(scored):
    act = rule_activity(scored)
    assert act["promoted"] >= 4
    assert act["active"] >= 5
    assert act["proposed"] > 0


def test_report_renders_without_blowing_up(scored):
    text = report(scored, ["1", "4"])
    assert "LEARNING CURVE" in text
    assert "FALSE POSITIVES" in text
    assert "RULE LIBRARY" in text
    assert "₹" in text          # money formatted, not raw paise


def test_calls_per_100_records_is_rounded_once(tmp_path):
    """46 calls over 132 records is 34.848...; rounding it to 34.85 first and
    then to one decimal printed 34.9. The report must print 34.8."""
    from src.db import reset_db
    from src.metrics import report
    conn = reset_db(tmp_path / "r.db")
    conn.execute("INSERT INTO run_metrics (batch_id, total_records, llm_calls,"
                 " llm_calls_avoided, match_rate, active_rules_count,"
                 " wall_clock_seconds, component_sizes_json)"
                 " VALUES ('4', 132, 46, 0, 0.5, 1, 1.0, '{}')")
    assert score_batch(conn, "4")["llm_calls_per_100_records"] == pytest.approx(
        46 / 132 * 100)
    assert "    34.8 " in report(conn, ["4"])
