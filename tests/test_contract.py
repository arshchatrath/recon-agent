"""Contract compliance, the point of the whole system.

An earlier version induced the fee schedule from settlement data and matched
against what it found, which meant a silently overcharging aggregator would be
learned, accepted, and reported as 100% correct. These tests exist to make sure
that cannot come back.
"""
import pytest

from src.contract import (compare_to_contract, contract_fee, contract_terms,
                          leakage_report, observed_schedule, render)
from src.db import ingest_batch, reset_db
from src.generate_data import GST_RATE, MDR


@pytest.fixture
def honest(tmp_path):
    """Batches 1-4: the aggregator charges what it agreed to."""
    c = reset_db(tmp_path / "h.db")
    for b in ("1", "2", "3", "4"):
        ingest_batch(c, b)
    yield c
    c.close()


@pytest.fixture
def overcharging(tmp_path):
    """Batch 5: the aggregator quietly raised its rates."""
    c = reset_db(tmp_path / "o.db")
    ingest_batch(c, "5")
    yield c
    c.close()


# ------------------------------------------------------------- the contract
def test_the_contract_is_an_input_not_something_discovered():
    """It is in the merchant's signed agreement. Reading it is not cheating;
    learning it from the counterparty's own output is the mistake."""
    terms = contract_terms()
    assert terms["gst"] == 0.18
    assert set(terms["instruments"]) == set(MDR)


def test_contract_fee_applies_the_agreed_rate_plus_gst():
    # 2% of 100000 = 2000, plus 18% GST on the fee = 360
    assert contract_fee(100_000, "CARD_CREDIT") == 2360
    assert contract_fee(100_000, "UPI") == 0
    assert contract_fee(100_000, "NETBANKING") == 1200 + 216


def test_an_unknown_instrument_has_no_contracted_fee():
    assert contract_fee(100_000, "CRYPTO") is None


# ------------------------------------- observed behaviour, without any model
def test_the_fee_schedule_is_recoverable_by_plain_statistics(honest):
    """The honest baseline. If fifteen lines of arithmetic recover the schedule
    exactly, no inference machinery may claim credit for discovering it."""
    seen = observed_schedule(honest)
    assert seen["CARD_CREDIT"]["rate"] == pytest.approx(0.02, abs=1e-4)
    assert seen["CARD_DEBIT"]["rate"] == pytest.approx(0.009, abs=1e-4)
    assert seen["UPI"]["rate"] == pytest.approx(0.0, abs=1e-6)
    assert seen["NETBANKING"]["flat_paise"] == pytest.approx(1200, abs=2)


def test_it_distinguishes_a_percentage_fee_from_a_flat_one(honest):
    seen = observed_schedule(honest)
    assert seen["CARD_CREDIT"]["shape"] == "rate"
    assert seen["NETBANKING"]["shape"] == "flat"


def test_refunds_and_reversals_do_not_poison_the_estimate(honest):
    """A refunded order is short for a reason that is not a fee, and a
    chargeback row is negative."""
    for instrument, seen in observed_schedule(honest).items():
        if seen["shape"] == "rate":
            assert 0 <= seen["rate"] < 0.1, (instrument, seen)


# ------------------------------------------------------------ the comparison
def test_an_honest_aggregator_shows_no_deviation_and_no_leakage(honest):
    for row in compare_to_contract(honest):
        if row["samples"]:
            assert row["agrees"] is True, row
    assert leakage_report(honest)["total_leaked_paise"] == 0


def test_a_silent_overcharge_is_detected_and_priced(overcharging):
    """The headline capability. Nobody told the system the rates changed."""
    rows = {r["instrument"]: r for r in compare_to_contract(overcharging)}
    assert rows["CARD_CREDIT"]["agrees"] is False
    assert rows["CARD_DEBIT"]["agrees"] is False
    assert rows["NETBANKING"]["agrees"] is False
    assert rows["UPI"]["agrees"] is True, "UPI was not overcharged"

    lk = leakage_report(overcharging)
    assert lk["total_leaked_paise"] > 0
    assert lk["transactions_overcharged"] > 0
    assert lk["by_instrument"]["CARD_CREDIT"]["leaked_paise"] > 0


def test_the_deviation_is_quantified_in_the_right_direction(overcharging):
    rows = {r["instrument"]: r for r in compare_to_contract(overcharging)}
    # contracted 2.0%, actually charged 2.1%
    assert rows["CARD_CREDIT"]["deviation"] == pytest.approx(0.001, abs=2e-4)
    assert rows["NETBANKING"]["deviation"] == pytest.approx(100, abs=3)


def test_leakage_names_the_transactions(overcharging):
    """A total nobody can trace is not an audit finding."""
    worst = leakage_report(overcharging)["worst_offenders"]
    assert worst
    for w in worst:
        assert w["settlement_txn_id"].startswith(("STL-", "CB-"))
        assert w["charged_paise"] > w["agreed_paise"]
        assert w["leaked_paise"] == w["charged_paise"] - w["agreed_paise"]


def test_zero_percent_is_not_reported_as_a_shape_deviation(overcharging):
    """UPI carries no fee, so "0%" and "flat 0p" describe the same thing.
    Calling that a deviation cries wolf on the one instrument that is fine."""
    upi = {r["instrument"]: r for r in compare_to_contract(overcharging)}["UPI"]
    assert upi["agrees"] is True
    assert upi["deviation"] == 0


def test_split_payouts_do_not_create_phantom_undercharging(honest):
    """A flat fee is charged once per settlement, but a split spreads it across
    legs, so counting legs individually made an overcharging aggregator look
    like it was undercharging."""
    assert leakage_report(honest)["total_leaked_paise"] == 0


def test_paise_level_drift_is_not_called_a_breach(honest):
    """Rounding noise is not a contractual violation."""
    assert leakage_report(honest)["transactions_overcharged"] == 0


# ------------------------------------------------------------------- output
def test_render_produces_a_readable_audit(overcharging):
    text = render(overcharging, "5")
    assert "CONTRACT COMPLIANCE" in text
    assert "DEVIATES FROM CONTRACT" in text
    assert "₹" in text                       # money is formatted
    assert "worst single transactions" in text


def test_render_is_quiet_when_everything_agrees(honest):
    text = render(honest)
    assert "DEVIATES FROM CONTRACT" not in text
    assert "matches contract" in text


# ----------------------------------------------- integrated into every run
def test_the_audit_runs_on_every_batch_not_on_request(tmp_path):
    """A silent overcharge is exactly the thing nobody thinks to go and look
    for, so it cannot be an opt-in report."""
    from src.pipeline import BatchRun
    c = reset_db(tmp_path / "r.db")
    ingest_batch(c, "5")
    summary = BatchRun(c, "5", use_llm=False).run()
    assert summary["fee_leakage_paise"] > 0
    assert summary["contract_deviations"] == 3

    row = c.execute("SELECT * FROM run_metrics WHERE batch_id='5'").fetchone()
    assert row["fee_leakage_paise"] == summary["fee_leakage_paise"]
    assert row["transactions_overcharged"] > 0
    c.close()


def test_an_honest_batch_records_zero_leakage(tmp_path):
    from src.pipeline import BatchRun
    c = reset_db(tmp_path / "h2.db")
    ingest_batch(c, "4")
    summary = BatchRun(c, "4", use_llm=False).run()
    assert summary["fee_leakage_paise"] == 0
    assert summary["contract_deviations"] == 0
    c.close()


def test_the_qa_agent_can_answer_am_i_being_overcharged(tmp_path):
    """The demo question. It has to be answerable from a tool, not from prose."""
    from src.pipeline import BatchRun
    from src.qa_agent import TOOLS, SettlementQA
    c = reset_db(tmp_path / "q.db")
    ingest_batch(c, "5")
    BatchRun(c, "5", use_llm=False).run()

    assert any(t["name"] == "check_contract_compliance" for t in TOOLS)
    out = SettlementQA(conn=c, client=object()).check_contract_compliance("5")
    assert set(out["instruments_deviating"]) == {"CARD_DEBIT", "CARD_CREDIT",
                                                 "NETBANKING"}
    assert out["total_fee_leakage_paise"] > 0
    assert out["worst_transactions"][0]["over_by"].startswith("₹")
    c.close()
