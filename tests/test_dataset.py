"""Phase 1 acceptance checks, run against the generated CSVs on disk."""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.calendar_utils import (add_working_days, is_working_day,
                                working_days_between)
from src.generate_data import BROKEN_INVARIANT_CASES, DATA_DIR

SRC = Path(__file__).resolve().parent.parent / "src"
BATCH_DIRS = sorted(DATA_DIR.glob("batch_*"))
ALL_DIRS = BATCH_DIRS + [DATA_DIR / "adversarial"]
MONEY_COLS = {"gross_amount_paise", "mdr_paise", "gst_on_mdr_paise",
              "net_amount_paise", "credit_amount_paise"}


def truth_of(d: Path) -> pd.DataFrame:
    f = d / "truth.csv"
    return pd.read_csv(f if f.exists() else d / "adversarial_truth.csv")


@pytest.fixture(scope="module", autouse=True)
def _require_data():
    if len(BATCH_DIRS) < 4 or not (DATA_DIR / "adversarial").exists():
        pytest.skip("run: python -m src.generate_data --batches 4 && --adversarial")


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
        # and nothing anywhere in a source file is a float
        if f.stem in ("orders", "settlements", "bank_statement"):
            floats = [c for c in df.columns
                      if pd.api.types.is_float_dtype(df[c])]
            assert not floats, f"{f.name} has float columns {floats}"


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_net_equals_gross_minus_mdr_minus_gst(d):
    stl = pd.read_csv(d / "settlements.csv")
    truth = truth_of(d)
    broken = set(truth.loc[truth.case_type.isin(BROKEN_INVARIANT_CASES),
                           "settlement_txn_id"])
    clean = stl[~stl.settlement_txn_id.isin(broken)]
    bad = clean[clean.net_amount_paise !=
                clean.gross_amount_paise - clean.mdr_paise - clean.gst_on_mdr_paise]
    assert bad.empty, bad[["settlement_txn_id"]].to_dict("records")


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_every_bulk_credit_equals_its_batch_net_sum(d):
    stl = pd.read_csv(d / "settlements.csv")
    bank = pd.read_csv(d / "bank_statement.csv")
    sums = sorted(stl.groupby("settlement_batch_id").net_amount_paise.sum())
    assert sorted(bank.credit_amount_paise) == sums
    assert len(bank) == stl.settlement_batch_id.nunique()


@pytest.mark.parametrize("d", ALL_DIRS, ids=lambda p: p.name)
def test_no_settlement_lands_on_a_weekend_or_public_holiday(d):
    for f in ("settlements.csv", "bank_statement.csv"):
        col = "settled_datetime" if f.startswith("settle") else "credit_datetime"
        for s in pd.read_csv(d / f)[col]:
            assert is_working_day(date.fromisoformat(s[:10])), f"{f} {s}"


@pytest.mark.parametrize("d", BATCH_DIRS, ids=lambda p: p.name)
def test_settlement_lag_is_at_least_one_working_day(d):
    orders = pd.read_csv(d / "orders.csv").set_index("order_id")
    stl = pd.read_csv(d / "settlements.csv")
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
        ids = set(pd.read_csv(d / "settlements.csv").settlement_batch_id)
        for n in bank.narration:
            assert not any(b in n for b in ids), n


# --------------------------------------------------------------- no leakage
PIPELINE_MODULES = [p for p in SRC.glob("*.py") if p.name != "generate_data.py"]
GROUND_TRUTH_LITERALS = [r"0\.009", r"0\.02\b", r"0\.18\b", r"\b1200\b"]


@pytest.mark.parametrize("mod", PIPELINE_MODULES, ids=lambda p: p.name)
def test_fee_constants_appear_in_no_module_but_the_generator(mod):
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
