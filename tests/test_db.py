import sqlite3

import pytest

from src.db import get_conn, ingest_batch, init_db, query, reset_db


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "t.db")
    yield c
    c.close()


def test_schema_creates_cleanly_and_is_idempotent(conn):
    init_db(conn)      # running it twice must not blow up or double-seed
    tables = {r["name"] for r in query(
        conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "settlements", "bank_credits", "matches", "exceptions",
            "rules", "rule_proposals", "rule_audit", "run_metrics"} <= tables


def test_rules_seeded_with_only_an_exact_id_rule(conn):
    rules = query(conn, "SELECT * FROM rules")
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "exact_id"
    assert rules[0]["status"] == "active"
    # nothing about fees or timing may be present at the start
    assert not query(conn, "SELECT 1 FROM rules WHERE rule_type IN"
                           " ('fee_formula','timing_window')")


def test_all_money_columns_are_integer_typed(conn):
    for table in ("orders", "settlements", "bank_credits", "exceptions",
                  "run_metrics"):
        for col in query(conn, f"PRAGMA table_info({table})"):
            if col["name"].endswith("_paise"):
                assert col["type"] == "INTEGER", f"{table}.{col['name']}"


def test_foreign_keys_are_enforced(conn):
    assert query(conn, "PRAGMA foreign_keys")[0][0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
            "match_kind,resolved_by,rule_id) VALUES"
            " ('1','order','O','settlement','S','rule','deterministic',9999)")


def test_check_constraints_reject_unknown_enums(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO matches (batch_id,left_type,left_id,right_type,right_id,"
            "match_kind,resolved_by) VALUES"
            " ('1','order','O','settlement','S','vibes','deterministic')")


def test_ingest_loads_all_three_sources_as_ints(conn):
    counts = ingest_batch(conn, "1")
    assert counts["orders"] >= 50 and counts["settlements"] > 0
    assert counts["bank_credits"] > 0
    row = query(conn, "SELECT * FROM settlements LIMIT 1")[0]
    for k in ("gross_amount_paise", "mdr_paise", "net_amount_paise"):
        assert isinstance(row[k], int)
    assert isinstance(query(conn, "SELECT * FROM bank_credits LIMIT 1")[0]
                      ["credit_amount_paise"], int)


def test_ingest_is_idempotent(conn):
    a = ingest_batch(conn, "1")
    ingest_batch(conn, "1")
    assert query(conn, "SELECT COUNT(*) c FROM orders")[0]["c"] == a["orders"]


def test_orphan_settlements_survive_ingestion(conn):
    """order_id_claimed is intentionally not a FK, orphans are a case to detect."""
    ingest_batch(conn, "1")
    orphans = query(conn, "SELECT COUNT(*) c FROM settlements s WHERE NOT EXISTS"
                          " (SELECT 1 FROM orders o WHERE o.order_id=s.order_id_claimed)")
    assert orphans[0]["c"] > 0


def test_reset_db_clears_ingested_data(tmp_path):
    p = tmp_path / "r.db"
    c = reset_db(p)
    ingest_batch(c, "1")
    c.close()
    c2 = reset_db(p)
    assert query(c2, "SELECT COUNT(*) c FROM orders")[0]["c"] == 0
    assert query(c2, "SELECT COUNT(*) c FROM rules")[0]["c"] == 1
    c2.close()


def test_get_conn_returns_row_mapping(tmp_path):
    c = init_db(get_conn(tmp_path / "g.db"))
    assert query(c, "SELECT 1 AS x")[0]["x"] == 1
    c.close()
