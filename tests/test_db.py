import sqlite3

import pytest

from src.db import get_conn, ingest_batch, init_db, reset_db


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "t.db")
    yield c
    c.close()


def test_schema_creates_cleanly_and_is_idempotent(conn):
    init_db(conn)      # running it twice must not blow up or double-seed
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "settlements", "bank_credits", "matches", "exceptions",
            "rules", "rule_proposals", "rule_audit", "run_metrics"} <= tables


def test_rules_seeded_with_only_an_exact_id_rule(conn):
    rules = conn.execute("SELECT * FROM rules").fetchall()
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "exact_id"
    assert rules[0]["status"] == "active"
    # nothing about fees or timing may be present at the start
    assert not conn.execute("SELECT 1 FROM rules WHERE rule_type IN"
                            " ('fee_formula','timing_window')").fetchall()


def test_all_money_columns_are_integer_typed(conn):
    for table in ("orders", "settlements", "bank_credits", "exceptions",
                  "run_metrics"):
        for col in conn.execute(f"PRAGMA table_info({table})"):
            if col["name"].endswith("_paise"):
                assert col["type"] == "INTEGER", f"{table}.{col['name']}"


def test_foreign_keys_are_enforced(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
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
    row = conn.execute("SELECT * FROM settlements LIMIT 1").fetchone()
    for k in ("gross_amount_paise", "mdr_paise", "net_amount_paise"):
        assert isinstance(row[k], int)
    assert isinstance(conn.execute("SELECT * FROM bank_credits LIMIT 1")
                      .fetchone()["credit_amount_paise"], int)


def test_ingest_is_idempotent(conn):
    a = ingest_batch(conn, "1")
    ingest_batch(conn, "1")
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == a["orders"]


def test_orphan_settlements_survive_ingestion(conn):
    """order_id_claimed is intentionally not a FK, orphans are a case to detect."""
    ingest_batch(conn, "1")
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM settlements s WHERE NOT EXISTS"
        " (SELECT 1 FROM orders o WHERE o.order_id=s.order_id_claimed)").fetchone()
    assert orphans["c"] > 0


def test_reset_db_clears_ingested_data(tmp_path):
    p = tmp_path / "r.db"
    c = reset_db(p)
    ingest_batch(c, "1")
    c.close()
    c2 = reset_db(p)
    assert c2.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    assert c2.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"] == 1
    c2.close()


def test_get_conn_returns_row_mapping(tmp_path):
    c = init_db(get_conn(tmp_path / "g.db"))
    assert c.execute("SELECT 1 AS x").fetchone()["x"] == 1
    c.close()


# ------------------------------------------------ Razorpay settlement export
RECON_HEADER = ("entity_id,type,debit,credit,amount,currency,fee,tax,on_hold,"
                "settled,created_at,settled_at,settlement_id,posted_at,"
                "credit_type,description,notes,payment_id,settlement_utr,"
                "order_id,order_receipt,method,card_network,card_issuer,"
                "card_type,dispute_id")


def recon_file(tmp_path, *rows):
    f = tmp_path / "settlements.csv"
    f.write_text("\n".join((RECON_HEADER,) + rows) + "\n", encoding="utf-8")
    return f


def test_razorpay_export_is_translated_to_the_internal_shape(tmp_path):
    from src.db import read_razorpay_settlements
    # 1736745540 = 2025-01-13 10:49 IST. Fee 2360 INCLUDES 360 GST.
    f = recon_file(
        tmp_path,
        "pay_A,payment,0,97640,100000,INR,2360,360,False,True,1736400000,"
        "1736745540,SB-1,,default,,,,UTR1,order_X,ORD-1,card,,,credit,",
        "rfnd_A,refund,5000,0,5000,INR,0,0,False,True,1736745540,1736745540,"
        "SB-1,,default,Partial refund,,pay_A,UTR1,order_X,ORD-1,card,,,credit,",
        # a chargeback reverses a payment, fee and GST with it: debit = amount - fee
        "adj_B,adjustment,3764,0,4000,INR,236,36,False,True,1736745540,"
        "1736745540,SB-1,,default,Chargeback,,pay_A,UTR1,order_X,ORD-1,card,,,credit,disp_1")
    df = read_razorpay_settlements(f)
    pay, cb = df.iloc[0], df.iloc[1]
    assert len(df) == 2, "the refund folds into its payment, not a row of its own"
    assert (pay.settlement_txn_id, pay.order_id, pay.instrument) == (
        "pay_A", "ORD-1", "CARD_CREDIT")
    assert (pay.gross_amount_paise, pay.mdr_paise, pay.gst_on_mdr_paise) == (
        100000, 2000, 360)
    assert pay.net_amount_paise == 97640 - 5000
    assert pay.settled_datetime == "2025-01-13T10:49:00"
    assert (cb.gross_amount_paise, cb.mdr_paise, cb.gst_on_mdr_paise,
            cb.net_amount_paise) == (-4000, -200, -36, -3764)


def test_an_unsupported_recon_row_type_fails_loudly(tmp_path):
    from src.db import read_razorpay_settlements
    f = recon_file(tmp_path, "trf_A,transfer,100296,0,100000,INR,296,46,False,"
                             "True,1,1,SB-1,,default,,,pay_A,UTR1,,,,,,,")
    with pytest.raises(ValueError, match="transfer"):
        read_razorpay_settlements(f)


def test_a_refund_without_its_payment_fails_loudly(tmp_path):
    from src.db import read_razorpay_settlements
    f = recon_file(tmp_path, "rfnd_A,refund,5000,0,5000,INR,0,0,False,True,1,1,"
                             "SB-1,,default,,,pay_MISSING,UTR1,,,upi,,,,")
    with pytest.raises(ValueError, match="pay_MISSING"):
        read_razorpay_settlements(f)
