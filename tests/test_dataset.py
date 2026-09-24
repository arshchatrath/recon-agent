"""Integrity checks on the fixed dataset in data/.

The dataset is synthetic and committed as-is. These tests pin down what makes
it trustworthy: the money arithmetic holds, every bank credit is exactly its
payout batch, nothing settles on a holiday, and the answer key stays out of the
source files.
"""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.calendar_utils import (add_working_days, is_working_day,
                                working_days_between)
from src.db import DATA_DIR, read_razorpay_settlements

# The economics the dataset was built with, for tests that need the "right
# answer". They equal the contracted rates in config.yaml (batch 5 is the batch
# where the aggregator charged above them). No module in src/ may hardcode
# them; see the leakage test at the bottom.
MDR = {"UPI": ("rate", 0.0), "CARD_DEBIT": ("rate", 0.009),
       "CARD_CREDIT": ("rate", 0.02), "NETBANKING": ("flat", 1200)}
GST_RATE = 0.18                     # of the MDR, never of the gross
LAG_WORKING_DAYS = {"UPI": 1}       # everything else settles at DEFAULT_LAG
DEFAULT_LAG = 2

# case types whose settlement rows deliberately break net = gross - mdr - gst
BROKEN_INVARIANT_CASES = {"partial_refund", "rounding_drift"}

SRC = Path(__file__).resolve().parent.parent / "src"
BATCH_DIRS = sorted(DATA_DIR.glob("batch_*"))
ALL_DIRS = BATCH_DIRS + [DATA_DIR / "adversarial"]
MONEY_COLS = {"gross_amount_paise", "credit_amount_paise",
              # Razorpay settlement recon export
              "debit", "credit", "amount", "fee", "tax"}
# GET /v1/settlements/recon/combined, fields in the documented order
RAZORPAY_RECON_COLUMNS = [
    "entity_id", "type", "debit", "credit", "amount", "currency", "fee", "tax",
    "on_hold", "settled", "created_at", "settled_at", "settlement_id",
    "posted_at", "credit_type", "description", "notes", "payment_id",
    "settlement_utr", "order_id", "order_receipt", "method", "card_network",
    "card_issuer", "card_type", "dispute_id"]


def truth_of(d: Path) -> pd.DataFrame:
    f = d / "truth.csv"
    return pd.read_csv(f if f.exists() else d / "adversarial_truth.csv")


def settlements_of(d: Path) -> pd.DataFrame:
    """The settlement export as the pipeline sees it, after translation."""
    return read_razorpay_settlements(d / "settlements.csv")


def test_the_full_dataset_is_present():
    assert [d.name for d in BATCH_DIRS] == [f"batch_{i}" for i in range(1, 6)]
    assert (DATA_DIR / "adversarial").exists()


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_all_three_files_exist_with_rows(d):
    for name in ("orders", "settlements", "bank_statement"):
        assert len(pd.read_csv(d / f"{name}.csv")) > 0, name


@pytest.mark.parametrize("d", BATCH_DIRS, ids=lambda p: p.name)
def test_each_batch_has_at_least_50_orders(d):
    assert len(pd.read_csv(d / "orders.csv")) >= 50


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_money_columns_are_integers_never_floats(d):
    for f in d.glob("*.csv"):
        df = pd.read_csv(f)
        for col in df.columns.intersection(MONEY_COLS):
            assert pd.api.types.is_integer_dtype(df[col]), f"{f.name}:{col}"
        # and nothing anywhere in a source file is a float (an always-empty
        # optional column, like Razorpay's posted_at, reads as NaN: skip it)
        if f.stem in ("orders", "settlements", "bank_statement"):
            floats = [c for c in df.columns
                      if pd.api.types.is_float_dtype(df[c]) and df[c].notna().any()]
            assert not floats, f"{f.name} has float columns {floats}"


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_net_equals_gross_minus_mdr_minus_gst(d):
    stl = settlements_of(d)
    truth = truth_of(d)
    broken = set(truth.loc[truth.case_type.isin(BROKEN_INVARIANT_CASES),
                           "settlement_txn_id"])
    clean = stl[~stl.settlement_txn_id.isin(broken)]
    bad = clean[clean.net_amount_paise !=
                clean.gross_amount_paise - clean.mdr_paise - clean.gst_on_mdr_paise]
    assert bad.empty, bad[["settlement_txn_id"]].to_dict("records")


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_every_bulk_credit_equals_its_batch_net_sum(d):
    stl = settlements_of(d)
    bank = pd.read_csv(d / "bank_statement.csv")
    sums = sorted(stl.groupby("settlement_batch_id").net_amount_paise.sum())
    assert sorted(bank.credit_amount_paise) == sums
    assert len(bank) == stl.settlement_batch_id.nunique()


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_no_settlement_lands_on_a_weekend_or_public_holiday(d):
    for f, s in ([("settlements", s) for s in settlements_of(d).settled_datetime]
                 + [("bank_statement", s) for s in
                    pd.read_csv(d / "bank_statement.csv").credit_datetime]):
        assert is_working_day(date.fromisoformat(s[:10])), f"{f} {s}"


@pytest.mark.parametrize("d", BATCH_DIRS, ids=lambda p: p.name)
def test_settlement_lag_is_at_least_one_working_day(d):
    orders = pd.read_csv(d / "orders.csv").set_index("order_id")
    stl = settlements_of(d)
    joined = stl[stl.order_id.isin(orders.index)]
    for _, r in joined.iterrows():
        # Lag is counted from the effective order day: an order placed on a
        # weekend or holiday is treated as landing on the next working day.
        effective = add_working_days(
            date.fromisoformat(orders.loc[r.order_id, "order_datetime"][:10]), 0)
        lag = working_days_between(effective,
                                   date.fromisoformat(r.settled_datetime[:10]))
        assert 1 <= lag <= 20, f"{r.settlement_txn_id} lag={lag}"


def test_adversarial_traps_are_all_present_and_labelled():
    truth = truth_of(DATA_DIR / "adversarial")
    kinds = {t.split("_on_time")[0].split("_late")[0].split("_true")[0]
                 .split("_decoy")[0] for t in truth.trap_type.dropna()}
    assert {"amount_twins", "coincidental_subset", "near_fee_trap",
            "off_by_one_day"} <= kinds


def test_trap_type_appears_only_in_the_adversarial_truth_file():
    adv = DATA_DIR / "adversarial"
    for name in ("orders", "settlements", "bank_statement"):
        assert "trap_type" not in pd.read_csv(adv / f"{name}.csv").columns


def test_narration_never_leaks_the_settlement_batch_id():
    for d in ALL_DIRS:
        bank = pd.read_csv(d / "bank_statement.csv")
        ids = set(settlements_of(d).settlement_batch_id)
        for n in bank.narration:
            assert not any(b in n for b in ids), n


# ------------------------------------------------- Razorpay export fidelity
@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_settlements_use_razorpays_recon_export_layout(d):
    rx = pd.read_csv(d / "settlements.csv", keep_default_na=False)
    assert list(rx.columns) == RAZORPAY_RECON_COLUMNS
    assert set(rx["type"]) <= {"payment", "refund", "adjustment"}
    assert set(rx["currency"]) == {"INR"}
    # every refund and adjustment points at a payment in the same export
    pays = set(rx.loc[rx["type"] == "payment", "entity_id"])
    assert set(rx.loc[rx["type"] != "payment", "payment_id"]) <= pays


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_razorpay_fee_includes_its_gst(d):
    """Razorpay: fee is "Fee (including GST)", tax is "GST charged". So a clean
    payment credits amount - fee, never amount - fee - tax."""
    rx = pd.read_csv(d / "settlements.csv", keep_default_na=False)
    truth = truth_of(d)
    drift = set(truth.loc[truth.case_type == "rounding_drift", "settlement_txn_id"])
    pay = rx[(rx["type"] == "payment") & ~rx["entity_id"].isin(drift)]
    assert (pay["credit"] == pay["amount"] - pay["fee"]).all()
    assert (pay["tax"] <= pay["fee"]).all()


# --------------------------------------------------------------- no leakage
PIPELINE_MODULES = list(SRC.glob("*.py"))
GROUND_TRUTH_LITERALS = [r"0\.009", r"0\.02\b", r"0\.18\b", r"\b1200\b"]


@pytest.mark.parametrize("mod", PIPELINE_MODULES, ids=lambda p: p.name)
def test_no_pipeline_module_hardcodes_the_fee_rates(mod):
    text = mod.read_text(encoding="utf-8")
    hits = [p for p in GROUND_TRUTH_LITERALS if re.search(p, text)]
    assert not hits, f"{mod.name} leaks ground-truth constants {hits}"


@pytest.mark.parametrize("mod", PIPELINE_MODULES, ids=lambda p: p.name)
def test_only_metrics_may_read_the_truth_files(mod):
    if mod.name == "metrics.py":
        return
    # a quoted literal means it is being opened; prose about it is fine
    text = mod.read_text(encoding="utf-8")
    hits = [lit for lit in ('truth.csv"', "truth.csv'",
                            'adversarial_truth"', "adversarial_truth'")
            if lit in text]
    assert not hits, f"{mod.name} reads a ground-truth file {hits}"
