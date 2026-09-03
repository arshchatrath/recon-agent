"""SQLite access. Raw SQL, no ORM.

The connection has foreign keys on. Money columns are INTEGER and every write
path here passes Python ints straight through -- nothing converts via float.
"""
from __future__ import annotations

import sqlite3
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


def query(conn, sql: str, params=()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


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

    truth.csv is deliberately not read here -- only metrics.py may read it.
    """
    d = Path(data_dir) if data_dir else (
        DATA_DIR / ("adversarial" if batch_id == "adversarial" else f"batch_{batch_id}"))
    counts = {}
    for table, (csv_name, cols, rename) in _INGEST.items():
        df = pd.read_csv(d / f"{csv_name}.csv")[cols]
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


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--ingest", help="batch id, e.g. 1 or adversarial")
    a = p.parse_args()
    conn = reset_db() if a.reset else init_db()
    print(f"db ready at {DB_PATH}")
    if a.ingest:
        print(ingest_batch(conn, a.ingest))
