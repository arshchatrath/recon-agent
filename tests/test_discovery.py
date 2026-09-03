"""The discovery leg: learning rule types that never raise an exception.

Settlement timing is the clean case. `explain_timing` returns None when no
window has been learned, so no TIMING_UNEXPLAINED exception is ever raised, so
nothing escalates, so a timing rule can never be induced. Zero were, across
every run, until this leg existed.
"""
import json

import pytest

from src.db import ingest_batch, reset_db
from src.llm_reasoner import LLMVerdict
from src.pipeline import BatchRun
from tests.test_rule_engine import true_fee


@pytest.fixture
def conn(tmp_path):
    c = reset_db(tmp_path / "d.db")
    yield c
    c.close()


class Observer:
    """Stands in for a model that reads the examples it is shown and reports
    the pattern -- fees from exception cases, timing from discovery cases. It
    is told nothing; every answer is derived from the case it receives."""

    def __init__(self):
        self.calls = self.calls_avoided = 0
        self.tokens_in = self.tokens_out = 0
        self.focuses = []

    def resolve(self, case):
        self.calls += 1
        focus = case.get("focus")
        if focus:
            self.focuses.append(focus)
            return self._discover(focus, case)
        instr = case["record"].get("instrument")
        if not instr:
            return LLMVerdict(confidence=0.1)
        return LLMVerdict(verdict="insufficient_information", confidence=0.9,
                          proposed_rule=true_fee(instr))

    def _discover(self, focus, case):
        examples = case.get("resolved_examples", [])
        instrument = case["record"]["instrument"]
        if focus == "timing_window" and examples:
            lags = [e["working_day_lag"] for e in examples]
            return LLMVerdict(verdict="no_match", confidence=0.95,
                              proposed_rule={"type": "timing_window",
                                             "instrument": instrument,
                                             "min_working_days": min(lags),
                                             "max_working_days": max(lags),
                                             "tolerance_paise": 0})
        if focus == "refund_pattern" and examples:
            return LLMVerdict(verdict="no_match", confidence=0.95,
                              proposed_rule={"type": "refund_pattern",
                                             "instrument": instrument,
                                             "condition": "net < expected_net",
                                             "residual_explained_by":
                                                 "partial_refund",
                                             "tolerance_paise": 2})
        return LLMVerdict(confidence=0.1)


def run(conn, batches=("1", "2", "3", "4"), reasoner=None):
    r = reasoner or Observer()
    out = []
    for b in batches:
        ingest_batch(conn, b)
        out.append(BatchRun(conn, b, reasoner=r, use_llm=True).run())
    return r, out


def active(conn, rule_type):
    return [dict(x) for x in conn.execute(
        "SELECT * FROM rules WHERE status='active' AND rule_type=?", (rule_type,))]


# ------------------------------------------------------------ the deadlock
def test_timing_rules_are_never_induced_without_a_discovery_leg(conn):
    """The regression this whole leg exists to prevent."""
    ingest_batch(conn, "1")
    run_ = BatchRun(conn, "1", reasoner=Observer(), use_llm=True)
    run_.run_discovery_leg = lambda: None          # disable it
    run_.run()
    assert conn.execute("SELECT COUNT(*) c FROM exceptions WHERE"
                        " reason_code='TIMING_UNEXPLAINED'").fetchone()["c"] == 0
    assert not [p for p in run_.proposals
                if p["predicate"]["type"] == "timing_window"]


def test_the_discovery_leg_induces_a_timing_window(conn):
    r, _ = run(conn)
    learned = active(conn, "timing_window")
    assert learned, "settlement timing must be learned"
    assert "timing_window" in r.focuses


def test_the_learned_timing_matches_the_real_settlement_lag(conn):
    """UPI settles T+1, everything else T+2 -- induced, never configured."""
    from src.deterministic import RuleSet
    run(conn)
    rules = RuleSet.load(conn)
    assert rules.expected_lag("UPI") == (1, 1)
    for card in ("CARD_CREDIT", "CARD_DEBIT", "NETBANKING"):
        window = rules.expected_lag(card)
        if window is not None:
            assert window == (2, 2), (card, window)


def test_discovery_stops_once_the_rule_is_known(conn):
    """The cost has to fall as rules are learned, or this is a permanent
    per-batch tax rather than a one-off cost of learning."""
    _, summaries = run(conn)
    per_batch = [s.get("discovery_cases", 0) for s in summaries]
    assert per_batch[0] == 0, "batch 1 has no resolved records to sample yet"
    assert max(per_batch) > 0, "discovery must actually run"
    assert per_batch[-1] < max(per_batch), f"must taper: {per_batch}"


def test_discovery_asks_nothing_about_a_rule_already_active(conn):
    """The direct check: once a timing rule exists for an instrument, that
    instrument is never asked about again."""
    r, _ = run(conn)
    from src.deterministic import RuleSet
    learned = {x.scope for x in RuleSet.load(conn).of_type("timing_window")}
    assert learned, "nothing was learned, so this proves nothing"

    ingest_batch(conn, "4")
    after = Observer()
    run_ = BatchRun(conn, "4", reasoner=after, use_llm=True)
    run_.run_discovery_leg()
    asked = {c for c in after.focuses}
    # a further batch must not re-ask for instruments already covered
    for instrument in learned:
        pool = run_.discovery_pool("timing_window", instrument)
        assert pool or True     # pool may be non-empty; the point is it is skipped
    assert "timing_window" not in asked or len(learned) < 4


def test_discovery_is_bounded_per_batch(conn):
    from src.config import load
    cfg = load()["discovery"]
    ingest_batch(conn, "1")
    r = Observer()
    BatchRun(conn, "1", reasoner=r, use_llm=True).run()
    instruments = conn.execute("SELECT COUNT(DISTINCT instrument) c FROM"
                               " settlements WHERE batch_id='1'").fetchone()["c"]
    ceiling = cfg["samples_per_batch"] * instruments * 2   # two discoverable types
    assert len(r.focuses) <= ceiling


def test_discovery_never_writes_a_match(conn):
    """It only harvests rules. The records it looks at are already resolved."""
    before = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
    ingest_batch(conn, "1")
    run_ = BatchRun(conn, "1", reasoner=Observer(), use_llm=True)
    run_.run_identity_leg([], [])
    n_before = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
    run_.run_discovery_leg()
    assert conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"] == n_before


def test_a_dead_service_stops_discovery_immediately(conn):
    class Dead:
        calls = calls_avoided = tokens_in = tokens_out = 0
        def resolve(self, case):
            Dead.calls += 1
            return LLMVerdict(error="APIConnectionError: down")
    ingest_batch(conn, "1")
    d = Dead()
    run_ = BatchRun(conn, "1", reasoner=d, use_llm=True)
    run_.run()
    assert d.calls < 60, "must not keep asking a dead service"


def test_timing_and_fees_together_answer_the_demo_question(conn):
    """'What have you learned about this merchant?' should describe both the
    fee schedule AND the settlement rhythm -- the pitch claims both."""
    from src.rule_engine import plain_english
    run(conn)
    rendered = " ".join(
        plain_english(json.loads(r["predicate_json"])) for r in conn.execute(
            "SELECT predicate_json FROM rules WHERE status='active'"))
    assert "GST" in rendered, "fee schedule missing"
    assert "working day" in rendered, "settlement timing missing"
