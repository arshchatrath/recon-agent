"""Phase 7. The tools are tested against a real populated database; the agent
loop is tested against a stub client so the suite needs no API key."""
import json

import pytest

from src.db import ingest_batch, reset_db
from src.pipeline import BatchRun
from src.qa_agent import TOOLS, SYSTEM_PROMPT, SettlementQA
from tests.test_llm_reasoner import Block, StubClient, tool_results_sent
from tests.test_rule_engine import Proposer


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    conn = reset_db(tmp_path_factory.mktemp("qa") / "qa.db")
    for b in ("1", "2", "3", "4"):
        ingest_batch(conn, b)
        BatchRun(conn, b, reasoner=Proposer(), use_llm=True).run()
    yield conn
    conn.close()


@pytest.fixture
def qa(populated):
    return SettlementQA(conn=populated, client=StubClient([]))


def one(conn, sql):
    return conn.execute(sql).fetchone()


# ------------------------------------------------------------ get_transaction
def test_get_transaction_finds_an_order_with_its_state(qa, populated):
    oid = one(populated, "SELECT order_id FROM orders LIMIT 1")["order_id"]
    out = qa.get_transaction(oid)
    assert out["found"] and out["record_type"] == "order"
    assert out["record"]["gross_amount_paise"]["formatted"].startswith("₹")
    assert isinstance(out["matches"], list)


def test_get_transaction_finds_settlements_and_credits(qa, populated):
    sid = one(populated, "SELECT settlement_txn_id s FROM settlements LIMIT 1")["s"]
    utr = one(populated, "SELECT utr FROM bank_credits LIMIT 1")["utr"]
    assert qa.get_transaction(sid)["record_type"] == "settlement"
    assert qa.get_transaction(utr)["record_type"] == "bank_credit"


def test_get_transaction_says_so_when_the_record_does_not_exist(qa):
    out = qa.get_transaction("ORD-DOES-NOT-EXIST")
    assert out["found"] is False
    assert "not in the reconciled data" in out["note"]


# ------------------------------------------------------------ list_exceptions
def test_exceptions_come_back_largest_money_at_risk_first(qa):
    out = qa.list_exceptions(limit=10)
    risks = [e["money_at_risk"]["paise"] for e in out["exceptions"]]
    assert risks == sorted(risks, reverse=True)
    assert out["total_money_at_risk"]["formatted"].startswith("₹")


def test_exceptions_can_be_filtered_by_money_and_batch(qa):
    big = qa.list_exceptions(min_money_at_risk_paise=100_000)
    assert all(e["money_at_risk"]["paise"] >= 100_000 for e in big["exceptions"])
    b2 = qa.list_exceptions(batch_id="2")
    assert all(e["batch_id"] == "2" for e in b2["exceptions"])


def test_exceptions_carry_what_was_considered_and_rejected(qa):
    out = qa.list_exceptions(limit=20)
    assert any(e["candidates_considered"] for e in out["exceptions"])


# --------------------------------------------------------------- explain_match
def test_explain_match_returns_the_rule_that_produced_it(qa, populated):
    mid = one(populated, "SELECT match_id FROM matches WHERE rule_id IS NOT NULL"
                         " LIMIT 1")["match_id"]
    out = qa.explain_match(mid)
    assert out["found"]
    assert "plain_english" in out["rule"]
    assert out["rule"]["promoted_at"]


def test_explain_match_declines_on_an_unknown_id(qa):
    assert qa.explain_match(999_999)["found"] is False


# -------------------------------------------------------------------- sum_fees
def test_sum_fees_aggregates_mdr_and_gst(qa):
    out = qa.sum_fees("2025-01-01", "2025-12-31")
    assert out["settlement_count"] > 0
    assert out["total_deducted"]["paise"] == (out["mdr"]["paise"]
                                              + out["gst_on_mdr"]["paise"])
    assert out["total_deducted"]["formatted"].startswith("₹")


def test_sum_fees_filters_by_instrument(qa):
    upi = qa.sum_fees("2025-01-01", "2025-12-31", instrument="UPI")
    assert upi["instrument"] == "UPI"
    # the induced rule says UPI is zero-MDR, and the data agrees
    assert upi["mdr"]["paise"] == 0


def test_sum_fees_over_an_empty_range_returns_zero_not_an_error(qa):
    out = qa.sum_fees("2000-01-01", "2000-01-02")
    assert out["settlement_count"] == 0 and out["mdr"]["paise"] == 0


# ---------------------------------------------------------- list_learned_rules
def test_learned_rules_are_reported_in_plain_english_with_provenance(qa):
    out = qa.list_learned_rules()
    assert out["active_count"] >= 5
    induced = [r for r in out["active_rules"] if r["was_induced_from_data"]]
    assert len(induced) >= 4, "the fee rules must be marked as induced"
    text = " ".join(r["plain_english"] for r in induced)
    assert "UPI" in text and "GST" in text
    for r in induced:
        assert r["backtest"]["records_contradicted"] == 0
        assert r["promoted_at"]


def test_rejected_proposals_are_reported_too(populated):
    """Judges care about what the gate blocked, not only what it let through.
    A wrong UPI fee rate is proposed here and must show up as rejected."""
    from src.rule_engine import intake, review_pending
    bad = {"type": "fee_formula", "instrument": "UPI",
           "params": {"rate": 0.025, "gst": 0.18}, "tolerance_paise": 2}
    for i in range(4):
        intake(populated, "4", bad, 0.95, case_id=f"c{i}")
    populated.commit()
    review_pending(populated)

    out = SettlementQA(conn=populated, client=StubClient([])).list_learned_rules(
        include_rejected=True)
    assert out["rejected_count"] > 0
    assert all(r["rejected_because"] for r in out["rejected_proposals"])
    assert any("backtest" in r["rejected_because"]
               for r in out["rejected_proposals"])


# ---------------------------------------------------------- trace_bulk_credit
def test_trace_bulk_credit_returns_the_constituents_that_sum_to_it(qa, populated):
    utr = one(populated, "SELECT left_id u FROM matches WHERE"
                         " left_type='bank_credit' LIMIT 1")["u"]
    out = qa.trace_bulk_credit(utr)
    assert out["found"] and out["constituent_count"] > 1
    assert out["reconciles_exactly"] is True
    assert out["constituents_total"]["paise"] == out["credit_amount"]["paise"]
    assert out["how_it_was_resolved"]


def test_trace_bulk_credit_declines_on_an_unknown_utr(qa):
    assert qa.trace_bulk_credit("UTR-NOPE")["found"] is False


# ----------------------------------------------------------- get_batch_metrics
def test_batch_metrics_and_learning_curve(qa):
    one_batch = qa.get_batch_metrics("1")
    assert one_batch["precision"] == 1.0
    curve = qa.get_batch_metrics()["learning_curve"]
    assert len(curve) == 4
    assert curve[-1]["open_exceptions"] < curve[0]["open_exceptions"]


# ------------------------------------------------------------------ dispatch
def test_every_declared_tool_is_actually_implemented(qa):
    for t in TOOLS:
        assert callable(getattr(qa, t["name"], None)), t["name"]


def test_an_unknown_tool_is_an_error_not_a_crash(qa):
    assert "error" in json.loads(qa.run_tool("drop_tables", {}))


def test_bad_tool_arguments_come_back_as_an_error(qa):
    assert "error" in json.loads(qa.run_tool("get_transaction", {"nope": 1}))


# ------------------------------------------------------------- the agent loop
def answer(text):
    return [Block(type="text", text=text)]


def test_the_agent_calls_a_tool_then_answers(populated):
    script = [
        [Block(type="tool_use", id="t1", name="list_learned_rules", input={})],
        answer("UPI settles at par. Sources: rule 2"),
    ]
    qa = SettlementQA(conn=populated, client=StubClient(script))
    out = qa.ask("What have you learned about this merchant?")
    assert out["tools_used"][0]["tool"] == "list_learned_rules"
    assert "Sources:" in out["answer"]


def test_the_demo_question_reaches_the_induced_rules(populated):
    """'What have you learned about this merchant?' must be answerable purely
    from what the system induced, this is the pitch's best moment."""
    script = [
        [Block(type="tool_use", id="t1", name="list_learned_rules", input={})],
        answer("Induced from the data. Sources: rules 2,3,4,5"),
    ]
    qa = SettlementQA(conn=populated, client=StubClient(script))
    qa.ask("What have you learned about this merchant?")
    payload = json.loads(tool_results_sent(qa.client, 1)[0]["content"])
    rendered = " ".join(r["plain_english"] for r in payload["active_rules"])
    assert "at par" in rendered                       # UPI zero-MDR
    assert "2% of gross" in rendered                  # credit cards
    assert "flat 1200 paise" in rendered              # netbanking
    assert "18% GST" in rendered


def test_a_question_with_no_answer_in_the_data_gets_a_refusal(populated):
    """The tool truthfully returns 'not found'; the prompt requires the model
    to say so rather than invent."""
    script = [
        [Block(type="tool_use", id="t1", name="get_transaction",
               input={"record_id": "ORD-9999-NOPE"})],
        answer("I don't have that in the reconciled data. Sources: none"),
    ]
    qa = SettlementQA(conn=populated, client=StubClient(script))
    out = qa.ask("What happened to order ORD-9999-NOPE?")
    sent = json.loads(tool_results_sent(qa.client, 1)[0]["content"])
    assert sent["found"] is False
    assert "don't have that in the reconciled data" in out["answer"]


def test_an_api_outage_produces_an_honest_non_answer(populated):
    class Boom(Exception):
        pass
    qa = SettlementQA(conn=populated, client=StubClient([Boom("503")]))
    out = qa.ask("anything")
    assert "no answer" in out["answer"] and "Boom" in out["error"]
    assert "unreachable" in out["answer"]


def test_a_missing_key_says_how_to_fix_it(populated):
    """The README advertises this command; failing it should teach, not stump."""
    auth = TypeError("Could not resolve authentication method. Expected one of "
                     "api_key, auth_token, or credentials to be set.")
    qa = SettlementQA(conn=populated, client=StubClient([auth]))
    out = qa.ask("What have you learned about this merchant?")
    assert "ANTHROPIC_API_KEY" in out["answer"]
    assert "src.metrics --report" in out["answer"]


def test_the_tool_budget_is_bounded(populated):
    loop = [[Block(type="tool_use", id="t", name="list_learned_rules", input={})]]
    qa = SettlementQA(conn=populated, client=StubClient(loop * 30))
    out = qa.ask("go forever", max_turns=4)
    assert qa.calls == 4 and "tool budget" in out["answer"]


# ---------------------------------------------------------------- the prompt
def test_the_system_prompt_forbids_unsourced_figures_and_requires_citations():
    assert "Never state a number" in SYSTEM_PROMPT
    assert "Sources: " in SYSTEM_PROMPT
    assert "I don't have that in the reconciled data" in SYSTEM_PROMPT
    assert "caveated wrong number is still a wrong number" in SYSTEM_PROMPT
