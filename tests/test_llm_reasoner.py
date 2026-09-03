"""Phase 4 with a stubbed client -- the suite must never need an API key.

What matters here is the fencing: abstention works, low confidence is discarded
whatever the model claimed, malformed output is retried and then given up on,
an API outage produces an exception rather than a crash, and the arithmetic
tool cannot be talked into executing anything.
"""
import json

import pytest

from src.config import load
from src.db import ingest_batch, reset_db
from src.deterministic import RuleSet, backtest
from src.llm_reasoner import LLMReasoner, safe_calculate
from src.pipeline import run_batch


# --------------------------------------------------------------- stub client
class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Usage:
    input_tokens, output_tokens = 100, 50


class Resp:
    def __init__(self, content):
        self.content, self.usage = content, Usage()


class StubClient:
    """Replays a scripted list of responses and records the requests made."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        if not self.script:
            raise AssertionError("stub ran out of scripted responses")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return Resp(nxt)


def tool_results_sent(client, call_index):
    """The tool_result message the stub was handed. The stub captures the live
    messages list, which keeps growing, so find the entry rather than index it."""
    msgs = client.requests[call_index]["messages"]
    return next(m["content"] for m in msgs
                if isinstance(m.get("content"), list) and m["content"]
                and isinstance(m["content"][0], dict)
                and m["content"][0].get("type") == "tool_result")


def text(payload):
    return [Block(type="text", text=json.dumps(payload))]


def verdict_payload(**over):
    p = {"verdict": "match", "matched_candidate_id": "STL-1", "confidence": 0.92,
         "reasoning": "the deduction is 3% plus GST on the fee",
         "residual_explanation": "fee and GST account for the whole difference",
         "proposed_rule": {"type": "fee_formula", "instrument": "CARD_CREDIT",
                           "params": {"rate": 0.03, "gst": 0.2},
                           "tolerance_paise": 2,
                           "generalisation_note": "every card settles this way"}}
    p.update(over)
    return p


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "l.db")
    yield c
    c.close()


def reasoner(conn, script):
    return LLMReasoner(conn, RuleSet.load(conn), client=StubClient(script))


CASE = {"record": {"settlement_txn_id": "STL-1", "gross_amount_paise": 100_000,
                   "net_amount_paise": 96_400, "instrument": "CARD_CREDIT"},
        "candidates": [{"order_id": "ORD-1", "gross_amount_paise": 100_000}],
        "reason_code": "FEE_UNEXPLAINED"}


# ------------------------------------------------------------ safe_calculate
def test_calculator_does_arithmetic():
    assert safe_calculate("100000 - round(100000 * 0.03) - round(3000 * 0.2)") \
        == 96400
    assert safe_calculate("abs(-5) + min(2, 3)") == 7


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwned')",
    "open('/etc/passwd').read()",
    "os.system('ls')",
    "(1).__class__.__bases__",
    "[x for x in range(10)]",
    "lambda: 1",
    "1/0",
    "not python at all",
])
def test_calculator_refuses_anything_but_arithmetic(expr):
    with pytest.raises(ValueError):
        safe_calculate(expr)


# ------------------------------------------------------------------ verdicts
def test_a_confident_match_is_returned_with_its_proposed_rule(conn):
    r = reasoner(conn, [text(verdict_payload())])
    v = r.resolve(CASE)
    assert v.usable and v.matched_candidate_id == "STL-1"
    assert v.proposed_rule["type"] == "fee_formula"
    assert v.proposed_rule["params"]["rate"] == 0.03
    assert v.calls == 1 and v.tokens_in == 100 and v.tokens_out == 50


def test_insufficient_information_is_a_valid_answer_and_writes_no_match(conn):
    r = reasoner(conn, [text(verdict_payload(
        verdict="insufficient_information", matched_candidate_id=None,
        confidence=0.3, proposed_rule=None))])
    v = r.resolve(CASE)
    assert not v.usable and v.matched_candidate_id is None
    assert v.proposed_rule is None


def test_a_match_below_the_confidence_floor_is_downgraded(conn):
    """The model said 'match'. It does not get to decide that."""
    r = reasoner(conn, [text(verdict_payload(confidence=0.5))])
    v = r.resolve(CASE)
    assert v.verdict == "insufficient_information"
    assert v.matched_candidate_id is None
    assert "below the 0.75 floor" in v.residual_explanation
    # the rule proposal survives -- proposing is cheap and gated downstream
    assert v.proposed_rule is not None


def test_an_unparseable_proposed_rule_is_dropped_at_proposal_time(conn):
    r = reasoner(conn, [text(verdict_payload(proposed_rule={
        "type": "fee_formula", "instrument": "UPI",
        "params": {"rate": 47.0},          # a 4700% fee
        "tolerance_paise": 2, "generalisation_note": "nonsense"}))])
    assert r.resolve(CASE).proposed_rule is None


def test_a_rule_of_an_invented_type_is_dropped(conn):
    r = reasoner(conn, [text(verdict_payload(proposed_rule={
        "type": "vibes_based", "instrument": "UPI", "params": {},
        "tolerance_paise": 2, "generalisation_note": "trust me"}))])
    assert r.resolve(CASE).proposed_rule is None


# ------------------------------------------------------------- tool plumbing
def test_the_model_can_call_the_calculator_and_then_answer(conn):
    script = [
        [Block(type="tool_use", id="t1", name="calculate",
               input={"expression": "100000 - 3000 - 600"})],
        text(verdict_payload()),
    ]
    r = reasoner(conn, script)
    v = r.resolve(CASE)
    assert v.usable
    assert v.tool_calls[0]["tool"] == "calculate"
    assert v.calls == 2                       # two round trips, both counted
    sent = tool_results_sent(r.client, 1)[0]
    assert json.loads(sent["content"])["result"] == 96400


def test_a_tool_error_is_handed_back_rather_than_crashing(conn):
    script = [
        [Block(type="tool_use", id="t1", name="calculate",
               input={"expression": "import os"})],
        text(verdict_payload(verdict="insufficient_information",
                             matched_candidate_id=None, confidence=0.2)),
    ]
    r = reasoner(conn, script)
    v = r.resolve(CASE)
    assert v.verdict == "insufficient_information"
    sent = tool_results_sent(r.client, 1)[0]
    assert "error" in json.loads(sent["content"])


def test_check_rule_against_history_is_wired_to_the_real_backtest(conn):
    ingest_batch(conn, "1")
    r = reasoner(conn, [])
    out = json.loads(r.run_tool("check_rule_against_history", {
        "predicate_json": json.dumps(
            {"type": "fee_formula", "instrument": "UPI",
             "params": {"rate": 0.03, "gst": 0.2}, "tolerance_paise": 2})}))
    assert set(out) >= {"correct_matches", "wrong_matches", "support"}


def test_working_day_lag_tool(conn):
    r = reasoner(conn, [])
    out = json.loads(r.run_tool("get_working_day_lag",
                                {"date1": "2025-01-10", "date2": "2025-01-14"}))
    assert out["working_days"] == 2


# ------------------------------------------------------------ failure modes
def test_malformed_json_is_retried_then_becomes_an_exception(conn):
    bad = [Block(type="text", text="here is my answer, roughly: maybe?")]
    tries = load()["llm"]["max_retries"]
    r = reasoner(conn, [bad] * tries)
    v = r.resolve(CASE)
    assert v.verdict == "insufficient_information"
    assert v.error and not v.usable
    assert len(r.client.requests) == tries     # retried, not given up on at once


def test_malformed_then_valid_recovers(conn):
    r = reasoner(conn, [[Block(type="text", text="nope")], text(verdict_payload())])
    assert r.resolve(CASE).usable


def test_an_api_outage_produces_an_exception_not_a_crash(conn):
    class Boom(Exception):
        pass
    r = reasoner(conn, [Boom("503 upstream")] * load()["llm"]["max_retries"])
    v = r.resolve(CASE)
    assert v.verdict == "insufficient_information"
    assert "Boom" in v.error


def test_an_outage_that_recovers_is_not_fatal(conn):
    class Boom(Exception):
        pass
    r = reasoner(conn, [Boom("429"), text(verdict_payload())])
    assert r.resolve(CASE).usable


# ------------------------------------------------------------- prompt shape
def test_the_prompt_carries_the_active_library_and_never_the_ground_truth(conn):
    r = reasoner(conn, [text(verdict_payload())])
    prompt = r.build_prompt(CASE)
    assert "active_rule_library" in prompt
    assert "exact_id" in prompt               # the one seeded rule
    for leaked in ("0.009", "0.018", "1200", "truth"):
        assert leaked not in prompt, f"prompt leaks {leaked}"


def test_the_system_prompt_demands_abstention_and_bounds_authority():
    from src.llm_reasoner import SYSTEM_PROMPT
    assert "insufficient_information" in SYSTEM_PROMPT
    assert "check_rule_against_history" in SYSTEM_PROMPT
    assert "calculate" in SYSTEM_PROMPT


def test_token_counters_accumulate_across_cases(conn):
    r = reasoner(conn, [text(verdict_payload()), text(verdict_payload())])
    r.resolve(CASE)
    r.resolve(CASE)
    assert r.calls == 2 and r.tokens_in == 200 and r.tokens_out == 100


# ------------------------------------------------------------- the backtest
def test_backtest_rewards_a_rule_that_agrees_with_history(conn):
    """Seed matches, then check a rule that describes them exactly."""
    ingest_batch(conn, "1")
    # net == gross picks the genuinely clean zero-fee rows; a partial refund or
    # a paise of rounding drift would make this a test of the data, not the gate
    s = conn.execute("SELECT * FROM settlements WHERE instrument='UPI'"
                     " AND net_amount_paise = gross_amount_paise LIMIT 5"
                     ).fetchall()
    for row in s:
        conn.execute("INSERT INTO matches (batch_id,left_type,left_id,right_type,"
                     "right_id,match_kind,resolved_by) VALUES"
                     " ('1','order',?,'settlement',?,'exact','deterministic')",
                     (row["order_id_claimed"], row["settlement_txn_id"]))
    conn.commit()
    # UPI carries no fee in this data, so a zero-rate rule should agree
    good = backtest(conn, {"type": "fee_formula", "instrument": "UPI",
                           "params": {"rate": 0.0, "gst": 0.0},
                           "tolerance_paise": 2})
    bad = backtest(conn, {"type": "fee_formula", "instrument": "UPI",
                          "params": {"rate": 0.05, "gst": 0.2},
                          "tolerance_paise": 2})
    assert good["wrong_matches"] == 0 and good["correct_matches"] > 0
    assert bad["wrong_matches"] > 0
    assert bad["counterexamples"]


def test_backtest_is_silent_about_rules_that_do_not_apply(conn):
    ingest_batch(conn, "1")
    run_batch(conn, "1")
    out = backtest(conn, {"type": "narration_pattern", "regex": "(NOPE)",
                          "maps_to": "settlement_batch_id"})
    assert out["support"] == 0 and out["precision"] == 0.0


# ------------------------------------------------- escalation in the pipeline
class ScriptedReasoner:
    """Stands in for LLMReasoner inside a full batch run."""

    def __init__(self, verdict_factory):
        self.make = verdict_factory
        self.calls = self.calls_avoided = 0
        self.tokens_in = self.tokens_out = 0
        self.seen = []

    def resolve(self, case):
        self.calls += 1
        self.tokens_in += 500
        self.tokens_out += 200
        self.seen.append(case)
        return self.make(case)


def test_only_escalatable_exceptions_reach_the_model(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    r = ScriptedReasoner(lambda case: LLMVerdict(
        verdict="insufficient_information", confidence=0.1,
        residual_explanation="not enough evidence"))
    ingest_batch(conn, "1")
    BatchRun(conn, "1", reasoner=r, use_llm=True).run()

    assert r.calls > 40, "the unexplained fees must be escalated"
    assert r.calls_avoided > 0, "unsettled orders must NOT be escalated"
    reasons = {c["reason_code"] for c in r.seen}
    assert reasons <= BatchRun.ESCALATABLE
    assert "NO_SETTLEMENT" not in reasons


def test_the_model_declining_leaves_the_exception_open(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    r = ScriptedReasoner(lambda case: LLMVerdict(
        verdict="insufficient_information", confidence=0.1,
        residual_explanation="the residual is unexplained"))
    ingest_batch(conn, "1")
    s = BatchRun(conn, "1", reasoner=r, use_llm=True).run()
    assert conn.execute("SELECT COUNT(*) c FROM matches WHERE resolved_by='llm'"
                        ).fetchone()["c"] == 0
    still_open = conn.execute("SELECT reason_text FROM exceptions WHERE"
                              " reason_code='FEE_UNEXPLAINED' LIMIT 1").fetchone()
    assert "model: the residual is unexplained" in still_open["reason_text"]


def test_an_llm_match_is_stamped_as_llm_resolved_never_exact(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    def verdict(case):
        return LLMVerdict(verdict="match", confidence=0.95,
                          matched_candidate_id=case["record"].get(
                              "settlement_txn_id"),
                          reasoning="fee and GST account for the difference")

    ingest_batch(conn, "1")
    BatchRun(conn, "1", reasoner=ScriptedReasoner(verdict), use_llm=True).run()
    rows = conn.execute("SELECT * FROM matches WHERE resolved_by='llm'").fetchall()
    assert rows
    for m in rows:
        assert m["match_kind"] == "llm_resolved"
        assert m["confidence"] >= 0.75
    assert conn.execute("SELECT COUNT(*) c FROM matches WHERE match_kind='exact'"
                        " AND left_type='order'").fetchone()["c"] == 0


def test_an_api_outage_marks_llm_unavailable_and_the_batch_still_finishes(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    r = ScriptedReasoner(lambda case: LLMVerdict(error="APIConnectionError: down"))
    ingest_batch(conn, "1")
    summary = BatchRun(conn, "1", reasoner=r, use_llm=True).run()
    assert summary["total_records"] > 100          # the run completed
    assert conn.execute("SELECT COUNT(*) c FROM exceptions WHERE"
                        " reason_code='LLM_UNAVAILABLE'").fetchone()["c"] > 0


def test_proposals_are_collected_for_the_rule_engine(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    rule = {"type": "fee_formula", "instrument": "CARD_CREDIT",
            "params": {"rate": 0.03, "gst": 0.2}, "tolerance_paise": 2}
    r = ScriptedReasoner(lambda case: LLMVerdict(
        verdict="insufficient_information", confidence=0.85,
        proposed_rule=rule, residual_explanation="looks like a percentage fee"))
    ingest_batch(conn, "1")
    run = BatchRun(conn, "1", reasoner=r, use_llm=True)
    run.run()
    assert len(run.proposals) > 10
    assert run.proposals[0]["predicate"]["type"] == "fee_formula"


def test_llm_counters_land_in_run_metrics(conn):
    from src.llm_reasoner import LLMVerdict
    from src.pipeline import BatchRun
    from src.db import ingest_batch

    r = ScriptedReasoner(lambda case: LLMVerdict(confidence=0.1))
    ingest_batch(conn, "1")
    BatchRun(conn, "1", reasoner=r, use_llm=True).run()
    row = conn.execute("SELECT * FROM run_metrics WHERE batch_id='1'").fetchone()
    assert row["llm_calls"] == r.calls > 0
    assert row["llm_calls_avoided"] == r.calls_avoided > 0
    assert row["llm_tokens_in"] > 0 and row["llm_tokens_out"] > 0


def test_no_llm_makes_no_calls_at_all(conn):
    from src.pipeline import run_batch
    run_batch(conn, "1", use_llm=False)
    row = conn.execute("SELECT * FROM run_metrics WHERE batch_id='1'").fetchone()
    assert row["llm_calls"] == 0


def test_missing_credentials_disable_escalation_once_not_per_case(conn):
    """A keyless clean clone must not spend seven minutes retrying sixty
    identical auth failures with exponential backoff."""
    auth = TypeError("Could not resolve authentication method. Expected one of "
                     "api_key, auth_token, or credentials to be set.")
    r = reasoner(conn, [auth] * 10)
    first = r.resolve(CASE)
    assert first.error and r.disabled

    before = len(r.client.requests)
    for _ in range(5):
        v = r.resolve(CASE)
        assert v.verdict == "insufficient_information" and v.error
    assert len(r.client.requests) == before, "must not call again once disabled"


def test_a_transient_error_does_not_disable_escalation(conn):
    """Only auth failures are unrecoverable; a 503 gets retried."""
    class Boom(Exception):
        pass
    r = reasoner(conn, [Boom("503"), text(verdict_payload())])
    assert r.resolve(CASE).usable
    assert r.disabled is None


def test_repeated_total_failures_stop_the_run(conn):
    """An exhausted daily quota is not recoverable within a batch. Grinding
    every remaining case through the full backoff wastes wall-clock and
    achieves nothing."""
    class Quota(Exception):
        pass
    tries = load()["llm"]["max_retries"]
    give_up = load()["llm"]["give_up_after_failures"]
    r = reasoner(conn, [Quota("429 RESOURCE_EXHAUSTED")] * (tries * give_up))

    for _ in range(give_up):
        assert r.resolve(CASE).error
    assert r.disabled and "in a row" in r.disabled

    before = len(r.client.requests)
    r.resolve(CASE)
    assert len(r.client.requests) == before, "must stop calling once disabled"


def test_a_success_resets_the_failure_streak(conn):
    """Intermittent failures must not accumulate into a false give-up."""
    class Boom(Exception):
        pass
    tries = load()["llm"]["max_retries"]
    script = ([Boom("503")] * tries + [text(verdict_payload())]
              + [Boom("503")] * tries)
    r = reasoner(conn, script)
    assert r.resolve(CASE).error          # streak 1
    assert r.resolve(CASE).usable         # resets
    assert r.resolve(CASE).error          # streak 1 again, not 2
    assert r.disabled is None
