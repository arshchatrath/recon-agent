"""Synthetic three-source reconciliation data.

THIS MODULE IS THE ONLY PLACE THE MERCHANT'S ECONOMICS EXIST. The MDR rates,
the GST rate and the settlement lags below are ground truth that the pipeline
must induce from the data. Never import them anywhere else, the leakage test
fails the build if these numbers appear in another src module.
"""
from __future__ import annotations

import argparse
import random
import string
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

from src.calendar_utils import add_working_days
from src.config import load
from src.money import apply_rate, split_proportional

# ----------------------------------------------------------------- ground truth
MDR = {                             # ("rate", fraction) | ("flat", paise)
    "UPI": ("rate", 0.0),           # zero-MDR government mandate
    "CARD_DEBIT": ("rate", 0.009),
    "CARD_CREDIT": ("rate", 0.02),
    "NETBANKING": ("flat", 1200),
}
GST_RATE = 0.18                     # of the MDR, never of the gross
LAG_WORKING_DAYS = {"UPI": 1}       # everything else:
DEFAULT_LAG = 2

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Case types whose settlement rows deliberately violate net = gross - mdr - gst
BROKEN_INVARIANT_CASES = {"partial_refund", "rounding_drift"}

EXCLUSIVE_CASES = [   # rolled in this order; first hit wins, remainder is 'clean'
    "full_refund", "missing_settlement", "partial_refund",
    "duplicate_ledger", "chargeback", "split_settlement",
]


# What the aggregator ACTUALLY charges, when it is not honouring the contract.
# Set by --overcharge. This is the thing the system is supposed to catch, and
# like every other ground truth it lives only in this file.
OVERCHARGE: dict = {}


def fees(gross: int, instrument: str) -> tuple[int, int, int]:
    """-> (mdr, gst_on_mdr, net). Ground truth; generator only."""
    kind, v = OVERCHARGE.get(instrument, MDR[instrument])
    mdr = apply_rate(gross, v) if kind == "rate" else int(v)
    gst = apply_rate(mdr, GST_RATE)
    return mdr, gst, gross - mdr - gst


def lag_for(instrument: str) -> int:
    return LAG_WORKING_DAYS.get(instrument, DEFAULT_LAG)


# --------------------------------------------------------------------- builders
class Emitter:
    """Accumulates rows, then writes the three source CSVs plus a truth file."""

    def __init__(self, rng: random.Random, tag: str):
        self.rng, self.tag = rng, tag
        self.orders: list[dict] = []
        self.settlements: list[dict] = []
        self.truth: list[dict] = []

    def rand(self, n=6) -> str:
        return "".join(self.rng.choices(string.ascii_uppercase + string.digits, k=n))

    def order_id(self, i) -> str:
        return f"ORD-{self.tag}-{i:04d}"

    def add_order(self, order_id, name, dt: datetime, gross, instrument, status):
        self.orders.append(dict(
            order_id=order_id, customer_name=name,
            order_datetime=dt.isoformat(timespec="seconds"),
            gross_amount_paise=int(gross), instrument=instrument, status=status))

    def add_settlement(self, order_id_claimed, settled: datetime, gross, mdr, gst,
                       net, instrument, txn_id=None) -> str:
        txn_id = txn_id or f"STL-{self.rand(8)}"
        self.settlements.append(dict(
            settlement_txn_id=txn_id, order_id=order_id_claimed,
            settled_datetime=settled.isoformat(timespec="seconds"),
            gross_amount_paise=int(gross), mdr_paise=int(mdr),
            gst_on_mdr_paise=int(gst), net_amount_paise=int(net),
            settlement_batch_id="", instrument=instrument))
        return txn_id

    def add_truth(self, order_id, settlement_txn_id, case_type, trap_type=""):
        self.truth.append(dict(order_id=order_id,
                               settlement_txn_id=settlement_txn_id,
                               utr="", case_type=case_type, trap_type=trap_type))

    def settle_datetime(self, order_dt: datetime, instrument, extra_days=0) -> datetime:
        d = add_working_days(order_dt.date(), lag_for(instrument) + extra_days)
        return datetime.combine(d, time(self.rng.randrange(9, 20),
                                        self.rng.randrange(60)))

    def narration(self) -> str:
        # Deliberately does NOT carry the settlement_batch_id: the bank leg has
        # to be solved by amount, not by string matching.
        fmt = self.rng.choice([
            "NEFT-RAZORPAY-SETTLEMENT-{}", "RTGS/RAZORPAYSOFTWARE/{}",
            "NEFT-RZPY-STLMT-{}/CR", "IMPS-RAZORPAY-{}-SETTL",
        ])
        return fmt.format(self.rand(4))

    def write(self, outdir: Path, truth_name="truth"):
        """Batches settlements by settled date, derives the bank credits, writes."""
        for s in self.settlements:
            d = s["settled_datetime"][:10].replace("-", "")
            s["settlement_batch_id"] = f"SB-{self.tag}-{d}"

        credits, txn_to_utr = [], {}
        for sb in sorted({s["settlement_batch_id"] for s in self.settlements}):
            members = [s for s in self.settlements if s["settlement_batch_id"] == sb]
            utr = f"UTR{self.rand(9)}"
            credits.append(dict(
                utr=utr,
                credit_datetime=datetime.combine(
                    date.fromisoformat(members[0]["settled_datetime"][:10]),
                    time(21, 30)).isoformat(timespec="seconds"),
                credit_amount_paise=sum(s["net_amount_paise"] for s in members),
                narration=self.narration()))
            for s in members:
                txn_to_utr[s["settlement_txn_id"]] = utr

        for t in self.truth:
            t["utr"] = txn_to_utr.get(t["settlement_txn_id"], t["settlement_txn_id"])

        outdir.mkdir(parents=True, exist_ok=True)
        money = {"gross_amount_paise", "mdr_paise", "gst_on_mdr_paise",
                 "net_amount_paise", "credit_amount_paise"}
        for name, rows in (("orders", self.orders),
                           ("settlements", self.settlements),
                           ("bank_statement", credits),
                           (truth_name, self.truth)):
            df = pd.DataFrame(rows)
            for c in df.columns.intersection(money):
                df[c] = df[c].astype("int64")
            df.to_csv(outdir / f"{name}.csv", index=False)
        return len(self.orders), len(self.settlements), len(credits)


# ----------------------------------------------------------------- main batches
def generate_batch(batch_no: int, n_orders: int, seed: int) -> Path:
    cfg = load()
    g = cfg["generator"]
    rng = random.Random(seed + batch_no * 1000)
    fake = Faker("en_IN")
    Faker.seed(seed + batch_no * 1000)
    e = Emitter(rng, str(batch_no))

    start = date.fromisoformat(g["first_batch_start"]) + timedelta(
        days=g["batch_days"] * (batch_no - 1))
    instruments = list(g["instrument_weights"])
    weights = [g["instrument_weights"][i] for i in instruments]
    mess = g["messiness"]

    for i in range(n_orders):
        oid = e.order_id(i)
        odt = datetime.combine(
            start + timedelta(days=rng.randrange(g["batch_days"])),
            time(rng.randrange(8, 23), rng.randrange(60)))
        gross = rng.randrange(g["min_gross_paise"], g["max_gross_paise"])
        instr = rng.choices(instruments, weights)[0]
        name = fake.name()

        case, roll, acc = "clean", rng.random(), 0.0
        for c in EXCLUSIVE_CASES:
            acc += mess[c]
            if roll < acc:
                case = c
                break
        if case == "clean" and rng.random() < mess["rounding_drift"]:
            case = "rounding_drift"

        mdr, gst, net = fees(gross, instr)

        if case == "full_refund":
            e.add_order(oid, name, odt, gross, instr, "refunded_full")
            e.add_truth(oid, "UNSETTLED", case)

        elif case == "missing_settlement":          # still in transit at cut-off
            e.add_order(oid, name, odt, gross, instr, "captured")
            e.add_truth(oid, "UNSETTLED", case)

        elif case == "duplicate_ledger":            # logged twice, settled once
            e.add_order(oid, name, odt, gross, instr, "captured")
            txn = e.add_settlement(oid, e.settle_datetime(odt, instr),
                                   gross, mdr, gst, net, instr)
            e.add_truth(oid, txn, "clean")
            dup = f"{oid}-D"
            e.add_order(dup, name, odt + timedelta(seconds=rng.randrange(30, 600)),
                        gross, instr, "captured")
            e.add_truth(dup, "UNSETTLED", case)

        elif case == "partial_refund":
            refund = apply_rate(gross, rng.uniform(0.10, 0.50))
            e.add_order(oid, name, odt, gross, instr, "refunded_partial")
            txn = e.add_settlement(oid, e.settle_datetime(odt, instr),
                                   gross, mdr, gst, net - refund, instr)
            e.add_truth(oid, txn, case)

        elif case == "rounding_drift":
            e.add_order(oid, name, odt, gross, instr, "captured")
            txn = e.add_settlement(oid, e.settle_datetime(odt, instr), gross, mdr,
                                   gst, net + rng.choice([-3, -2, -1, 1, 2, 3]), instr)
            e.add_truth(oid, txn, case)

        elif case == "split_settlement":            # one order, two payout batches
            e.add_order(oid, name, odt, gross, instr, "captured")
            first_w = rng.randrange(30, 70)
            w = [first_w, 100 - first_w]
            parts = [split_proportional(x, w) for x in (gross, mdr, gst)]
            first = e.settle_datetime(odt, instr)
            for leg in (0, 1):
                sdt = first if leg == 0 else datetime.combine(
                    add_working_days(first.date(), 1), first.time())
                txn = e.add_settlement(
                    oid, sdt, parts[0][leg], parts[1][leg], parts[2][leg],
                    parts[0][leg] - parts[1][leg] - parts[2][leg], instr)
                e.add_truth(oid, txn, case)

        elif case == "chargeback":                  # settles, then reverses later
            e.add_order(oid, name, odt, gross, instr, "captured")
            sdt = e.settle_datetime(odt, instr)
            txn = e.add_settlement(oid, sdt, gross, mdr, gst, net, instr)
            e.add_truth(oid, txn, "clean")
            cb_dt = datetime.combine(
                add_working_days(sdt.date(), rng.randrange(5, 16)), sdt.time())
            cb = e.add_settlement(oid, cb_dt, -gross, -mdr, -gst, -net, instr,
                                  txn_id=f"CB-{e.rand(8)}")
            e.add_truth(oid, cb, "chargeback_reversal")

        else:                                       # clean
            e.add_order(oid, name, odt, gross, instr, "captured")
            txn = e.add_settlement(oid, e.settle_datetime(odt, instr),
                                   gross, mdr, gst, net, instr)
            e.add_truth(oid, txn, "clean")

    for _ in range(round(mess["orphan_settlement"] * n_orders)):   # no such order
        instr = rng.choices(instruments, weights)[0]
        gross = rng.randrange(g["min_gross_paise"], g["max_gross_paise"])
        mdr, gst, net = fees(gross, instr)
        odt = datetime.combine(start + timedelta(days=rng.randrange(g["batch_days"])),
                               time(12, 0))
        txn = e.add_settlement(f"ORD-{e.tag}-9{rng.randrange(100, 999)}",
                               e.settle_datetime(odt, instr), gross, mdr, gst,
                               net, instr)
        e.add_truth("ORPHAN", txn, "orphan_settlement")

    out = DATA_DIR / f"batch_{batch_no}"
    o, s, c = e.write(out)
    print(f"batch {batch_no}: {o} orders, {s} settlements, {c} bank credits -> {out}")
    return out


# ------------------------------------------------------------------ adversarial
def generate_adversarial(seed: int) -> Path:
    """Traps built to induce false positives. trap_type lives only in the truth
    file, which the pipeline must never read."""
    rng = random.Random(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)
    e = Emitter(rng, "ADV")
    base = date(2025, 3, 3)   # a Monday
    counter = [0]

    def at(day_offset, hour=11) -> datetime:
        return datetime.combine(base + timedelta(days=day_offset), time(hour, 0))

    def trap(dt, gross, instr, trap_type, status="captured", extra_days=0,
             net_override=None, case="clean"):
        oid = e.order_id(counter[0])
        counter[0] += 1
        e.add_order(oid, fake.name(), dt, gross, instr, status)
        mdr, gst, net = fees(gross, instr)
        txn = e.add_settlement(oid, e.settle_datetime(dt, instr, extra_days),
                               gross, mdr, gst,
                               net if net_override is None else net_override, instr)
        e.add_truth(oid, txn, case, trap_type)

    # 1. amount twins, identical gross + instrument, settling one day apart.
    #    A greedy matcher will bind the wrong pair.
    for k in range(4):
        for day in (0, 1):
            trap(at(day), 250000 + k * 7000, "CARD_CREDIT", "amount_twins")

    # 2. coincidental subsets, an unrelated subset sums to the same total as
    #    the true constituents. UPI is zero-fee, which pins net == gross so the
    #    collision is exact.
    for label, amounts, day in (("true", [100000, 200000, 300000], 2),
                                ("decoy", [150000, 450000], 3)):
        for amt in amounts:
            assert fees(amt, "UPI")[2] == amt, "UPI must be zero-fee here"
            trap(at(day), amt, "UPI", f"coincidental_subset_{label}")

    # 3. near-fee trap, a UPI order (0% MDR) partially refunded by exactly what
    #    a 2% card fee plus GST would have taken. A rule induced from this case
    #    alone is wrong, and backtesting has to catch it.
    for k in range(3):
        gross = 480000 + k * 13000
        fake_mdr = apply_rate(gross, 0.02)
        refund = fake_mdr + apply_rate(fake_mdr, GST_RATE)
        trap(at(4), gross, "UPI", "near_fee_trap", status="refunded_partial",
             net_override=fees(gross, "UPI")[2] - refund, case="partial_refund")

    # 4. off-by-one-day, same amount, same instrument; one settles inside the
    #    normal window, its twin one working day late.
    for k in range(3):
        gross = 315000 + k * 5000
        for extra, tag in ((0, "on_time"), (1, "late")):
            trap(at(5), gross, "CARD_DEBIT", f"off_by_one_day_{tag}",
                 extra_days=extra)

    out = DATA_DIR / "adversarial"
    o, s, c = e.write(out, truth_name="adversarial_truth")
    print(f"adversarial: {o} orders, {s} settlements, {c} bank credits -> {out}")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="generate reconciliation source data")
    p.add_argument("--batch", type=int, help="generate a single batch number")
    p.add_argument("--batches", type=int, help="generate batches 1..N")
    p.add_argument("--orders", type=int, default=60)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--adversarial", action="store_true")
    p.add_argument("--overcharge", action="store_true",
                   help="the aggregator quietly bills above the contracted "
                        "rates; the pipeline should detect and price this")
    a = p.parse_args(argv)

    if a.overcharge:
        # A realistic silent overcharge: a few basis points on cards and a
        # rupee on netbanking. Small enough that nobody eyeballing a
        # spreadsheet would notice; large enough to matter at volume.
        OVERCHARGE.update({"CARD_CREDIT": ("rate", 0.021),
                           "CARD_DEBIT": ("rate", 0.0095),
                           "NETBANKING": ("flat", 1300)})
        print("generating WITH a silent overcharge: cards +0.1/+0.05 pts, "
              "netbanking +Rs 1")

    if a.adversarial:
        generate_adversarial(a.seed)
    elif a.batch:
        generate_batch(a.batch, a.orders, a.seed)
    elif a.batches:
        for b in range(1, a.batches + 1):
            generate_batch(b, a.orders, a.seed)
    else:
        p.error("one of --batch, --batches or --adversarial is required")


if __name__ == "__main__":
    main()
