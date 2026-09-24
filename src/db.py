"""SQLite access. Raw SQL, no ORM.

The connection has foreign keys on. Money columns are INTEGER and every write
path here passes Python ints straight through, nothing converts via float.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "recon.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DATA_DIR = ROOT / "data"

# The only rule the system starts with. Everything about fees and timing has
# to be induced from the data by the rule engine.
SEED_RULE = (
    "exact_id", "ALL",
    '{"type": "exact_id", "left": "order.order_id",'
    ' "right": "settlement.order_id_claimed"}',
    0,   # highest precedence
)


def get_conn(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    conn = conn or get_conn()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not conn.execute("SELECT 1 FROM rules WHERE rule_type='exact_id'").fetchone():
        conn.execute(
            "INSERT INTO rules (rule_type, scope_instrument, predicate_json,"
            " priority, status, promoted_at) VALUES (?,?,?,?,'active',datetime('now'))",
            SEED_RULE)
    conn.commit()
    return conn


def reset_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Drop the file entirely and rebuild. Used by --reset-db and by tests."""
    p = Path(path or DB_PATH)
    if p.exists():
        p.unlink()
    return init_db(get_conn(p))


# ------------------------------------------------ Razorpay settlement export
IST = timezone(timedelta(hours=5, minutes=30))
_INSTRUMENT = {("upi", ""): "UPI", ("netbanking", ""): "NETBANKING",
               ("card", "credit"): "CARD_CREDIT", ("card", "debit"): "CARD_DEBIT"}


def read_razorpay_settlements(path) -> pd.DataFrame:
    """Razorpay's combined settlement recon export -> one internal row per
    payment or adjustment.

    The export (GET /v1/settlements/recon/combined) differs from the internal
    table in four ways, each translated here:
      - `fee` INCLUDES GST and `tax` is the GST part of it, so the MDR is
        fee - tax;
      - a refund is its own `refund` row pointing at its payment through
        `payment_id`; it is folded into that payment's net;
      - a chargeback is an `adjustment` row debiting the merchant, so it becomes
        a negative row, as a reversal is internally;
      - timestamps are unix seconds; they become IST ISO strings.
    `settlement_utr` is deliberately not read: it is the aggregator's own claim
    about which bank credit each payment landed in, and the bank leg proves
    that grouping independently, from the bank statement.
    """
    rx = pd.read_csv(path, keep_default_na=False, dtype={
        "payment_id": str, "card_type": str, "order_receipt": str})
    unknown = set(rx["type"]) - {"payment", "refund", "adjustment"}
    if unknown:
        raise ValueError(f"{path}: unsupported recon row type(s) {sorted(unknown)}")

    rows = rx[rx["type"].isin(["payment", "adjustment"])]
    refunds = rx[rx["type"] == "refund"].groupby("payment_id")["debit"].sum()
    orphaned = set(refunds.index) - set(rows["entity_id"])
    if orphaned:
        raise ValueError(f"{path}: refunds for payments not in the file {sorted(orphaned)}")

    sign = rows["type"].map({"payment": 1, "adjustment": -1})
    return pd.DataFrame({
        "settlement_txn_id": rows["entity_id"],
        "order_id": rows["order_receipt"],
        "settled_datetime": rows["settled_at"].map(
            lambda ts: datetime.fromtimestamp(int(ts), IST).strftime("%Y-%m-%dT%H:%M:%S")),
        "gross_amount_paise": sign * rows["amount"],
        "mdr_paise": sign * (rows["fee"] - rows["tax"]),
        "gst_on_mdr_paise": sign * rows["tax"],
        "net_amount_paise": (rows["credit"] - rows["debit"]
                             - rows["entity_id"].map(refunds).fillna(0).astype(int)),
        "settlement_batch_id": rows["settlement_id"],
        "instrument": [_INSTRUMENT[m, c] for m, c in
                       zip(rows["method"], rows["card_type"])],
    }).reset_index(drop=True)


# ---------------------------------------------------------------- ingestion
_INGEST = {
    "orders": ("orders", ["order_id", "customer_name", "order_datetime",
                          "gross_amount_paise", "instrument", "status"], None),
    "settlements": ("settlements",
                    ["settlement_txn_id", "order_id", "settled_datetime",
                     "gross_amount_paise", "mdr_paise", "gst_on_mdr_paise",
                     "net_amount_paise", "settlement_batch_id", "instrument"],
                    {"order_id": "order_id_claimed"}),
    "bank_credits": ("bank_statement", ["utr", "credit_datetime",
                                        "credit_amount_paise", "narration"], None),
}


def ingest_batch(conn: sqlite3.Connection, batch_id: str,
                 data_dir: Path | None = None) -> dict[str, int]:
    """Load one batch's three source CSVs. Idempotent (INSERT OR REPLACE).

    truth.csv is not read here, only metrics.py may read it.
    """
    d = Path(data_dir) if data_dir else (
        DATA_DIR / ("adversarial" if batch_id == "adversarial" else f"batch_{batch_id}"))
    counts = {}
    for table, (csv_name, cols, rename) in _INGEST.items():
        path = d / f"{csv_name}.csv"
        df = (read_razorpay_settlements(path) if table == "settlements"
              else pd.read_csv(path))[cols]
        if rename:
            df = df.rename(columns=rename)
            cols = list(df.columns)
        else:
            cols = list(cols)
        df.insert(1, "batch_id", batch_id)
        # Series.tolist() hands back native Python ints; sqlite3 will not bind
        # numpy.int64, and coercing via float would be a money bug.
        rows = list(zip(*[df[c].tolist() for c in df.columns]))
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(df.columns)})"
            f" VALUES ({','.join('?' * len(df.columns))})", rows)
        counts[table] = len(df)
    conn.commit()
    return counts
