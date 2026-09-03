import json

import pytest

from src.deterministic import (ALL, INAPPLICABLE, PredicateError, Rule,
                               RuleConflictError, RuleSet, canonical_fingerprint,
                               detect_overlaps, evaluate, fee_expected_net,
                               topological_order, validate_predicate)


def rule(rid, rtype, scope, pred, priority=100):
    return Rule(rid, rtype, scope, json.dumps(pred), priority)


def fee_pred(rate=0.03, gst=0.2, instrument="CARD_CREDIT", tol=2):
    return {"type": "fee_formula", "instrument": instrument,
            "params": {"rate": rate, "gst": gst}, "tolerance_paise": tol}


def order(oid="O1", gross=100_000, instr="CARD_CREDIT",
          dt="2025-01-06T10:00:00"):
    return dict(order_id=oid, gross_amount_paise=gross, instrument=instr,
                order_datetime=dt)


def stl(sid="S1", gross=100_000, net=96_400, instr="CARD_CREDIT",
        dt="2025-01-08T10:00:00", claimed="O1", batch="SB1"):
    return dict(settlement_txn_id=sid, order_id_claimed=claimed,
                gross_amount_paise=gross, net_amount_paise=net,
                instrument=instr, settled_datetime=dt,
                settlement_batch_id=batch)


# ------------------------------------------------------------- validation
def test_valid_predicates_pass():
    for p in (fee_pred(),
              {"type": "timing_window", "instrument": "UPI",
               "min_working_days": 1, "max_working_days": 1},
              {"type": "refund_pattern", "instrument": ALL,
               "condition": "net < expected_net", "tolerance_paise": 2},
              {"type": "narration_pattern",
               "regex": r"NEFT-RAZORPAY-([A-Z0-9]+)",
               "maps_to": "settlement_batch_id"},
              {"type": "fee_formula", "instrument": "NETBANKING",
               "params": {"flat_paise": 500, "gst": 0.2}}):
        assert validate_predicate(p)["type"] == p["type"]


@pytest.mark.parametrize("bad", [
    "not json at all",
    {"type": "vibes"},
    {"type": "fee_formula"},
    {"type": "fee_formula", "params": {"gst": 0.2}},            # no rate or flat
    {"type": "fee_formula", "params": {"rate": 3.0, "gst": 0.2}},   # rate > 1
    {"type": "fee_formula", "params": {"rate": 0.03, "gst": 0.2},
     "expr": "net = gross - os.system('rm -rf /')"},            # not a template
    {"type": "timing_window", "min_working_days": 5, "max_working_days": 1},
    {"type": "refund_pattern", "condition": "net > 0"},
    {"type": "narration_pattern", "regex": "([", "maps_to": "settlement_batch_id"},
    {"type": "narration_pattern", "regex": "(x)", "maps_to": "utr"},
])
def test_unparseable_predicates_are_rejected_at_proposal_time(bad):
    with pytest.raises(PredicateError):
        validate_predicate(bad)


def test_expr_is_never_evaluated_as_code():
    """The expr string is checked against a template and then ignored; the
    arithmetic is done in Python from the params."""
    p = validate_predicate({
        "type": "fee_formula", "params": {"rate": 0.03, "gst": 0.2},
        "expr": ("net = gross - round(gross * {rate})"
                 " - round(round(gross * {rate}) * {gst})")})
    assert fee_expected_net(100_000, p["params"]) == 100_000 - 3000 - 600


def test_fingerprint_collapses_near_identical_proposals():
    a = canonical_fingerprint(fee_pred(rate=0.03))
    b = canonical_fingerprint(fee_pred(rate=0.0300000001))
    assert a == b
    assert a != canonical_fingerprint(fee_pred(rate=0.04))


# -------------------------------------------------------------- evaluation
def test_exact_id_rule():
    p = {"type": "exact_id"}
    assert evaluate(p, order(), stl(claimed="O1")) is True
    assert evaluate(p, order(), stl(claimed="O2")) is False
    assert evaluate(p, {}, stl()) == INAPPLICABLE


def test_fee_formula_within_and_outside_tolerance():
    p = fee_pred()                       # 100000 - 3000 - 600 = 96400
    assert evaluate(p, order(), stl(net=96_400)) is True
    assert evaluate(p, order(), stl(net=96_402)) is True      # inside tol
    assert evaluate(p, order(), stl(net=96_410)) is False


def test_flat_fee_formula():
    p = {"type": "fee_formula", "instrument": "NETBANKING",
         "params": {"flat_paise": 500, "gst": 0.2}, "tolerance_paise": 0}
    assert evaluate(p, order(instr="NETBANKING"),
                    stl(instr="NETBANKING", net=100_000 - 500 - 100)) is True


def test_a_rule_out_of_scope_is_inapplicable_not_false():
    """The difference matters: False means 'this rule says no', INAPPLICABLE
    means 'this rule has no opinion' and the next rule gets a turn."""
    assert evaluate(fee_pred(instrument="CARD_CREDIT"),
                    order(instr="UPI"), stl(instr="UPI")) == INAPPLICABLE


def test_timing_window_counts_working_days_over_a_weekend():
    p = {"type": "timing_window", "instrument": ALL,
         "min_working_days": 2, "max_working_days": 2}
    # Friday order, Tuesday settlement = 2 working days
    assert evaluate(p, order(dt="2025-01-10T10:00:00"),
                    stl(dt="2025-01-14T10:00:00")) is True
    assert evaluate(p, order(dt="2025-01-10T10:00:00"),
                    stl(dt="2025-01-13T10:00:00")) is False


def test_narration_pattern_matches_the_batch_id():
    p = {"type": "narration_pattern", "regex": r"NEFT-RAZORPAY-([A-Z0-9]+)",
         "maps_to": "settlement_batch_id"}
    credit = dict(narration="NEFT-RAZORPAY-SB1")
    assert evaluate(p, credit, stl(batch="SB1")) is True
    assert evaluate(p, credit, stl(batch="SB2")) is False


def test_refund_pattern_is_inapplicable_until_a_fee_rule_exists():
    p = {"type": "refund_pattern", "instrument": ALL,
         "condition": "net < expected_net", "tolerance_paise": 2}
    empty = RuleSet([])
    assert evaluate(p, order(), stl(net=50_000), empty) == INAPPLICABLE
    learned = RuleSet([rule(1, "fee_formula", "CARD_CREDIT", fee_pred())])
    assert evaluate(p, order(), stl(net=50_000), learned) is True
    assert evaluate(p, order(), stl(net=96_400), learned) is False


# --------------------------------------------------------------- rule set
def test_ruleset_exposes_learned_economics_only():
    empty = RuleSet([])
    assert empty.expected_net(100_000, "CARD_CREDIT") is None
    assert empty.expected_lag("UPI") is None
    learned = RuleSet([
        rule(1, "fee_formula", "CARD_CREDIT", fee_pred()),
        rule(2, "timing_window", "UPI", {"type": "timing_window",
                                         "instrument": "UPI",
                                         "min_working_days": 1,
                                         "max_working_days": 1})])
    assert learned.expected_net(100_000, "CARD_CREDIT") == 96_400
    assert learned.expected_lag("UPI") == (1, 1)


def test_first_match_returns_the_rule_that_fired():
    rs = RuleSet([rule(7, "fee_formula", "CARD_CREDIT", fee_pred())])
    assert rs.first_match(order(), stl(net=96_400)).rule_id == 7
    assert rs.first_match(order(), stl(net=1)) is None


# ------------------------------------------------------------ precedence
def test_specific_scope_is_evaluated_before_a_general_one():
    general = rule(1, "fee_formula", ALL, fee_pred(instrument=ALL), 100)
    specific = rule(2, "fee_formula", "UPI", fee_pred(instrument="UPI"), 100)
    assert [r.rule_id for r in topological_order([general, specific])] == [2, 1]


def test_lower_priority_number_wins():
    a = rule(1, "timing_window", "UPI", {"type": "timing_window",
                                         "min_working_days": 1,
                                         "max_working_days": 1}, 50)
    b = rule(2, "fee_formula", "UPI", fee_pred(instrument="UPI"), 10)
    assert [r.rule_id for r in topological_order([a, b])] == [2, 1]


def test_a_precedence_cycle_raises_rather_than_guessing():
    """Specificity says the UPI rule wins; priority says the ALL rule wins.
    The library contradicts itself, so refuse to order it."""
    general = rule(1, "fee_formula", ALL, fee_pred(instrument=ALL), 10)
    specific = rule(2, "fee_formula", "UPI", fee_pred(instrument="UPI"), 50)
    with pytest.raises(RuleConflictError) as e:
        topological_order([general, specific])
    assert set(e.value.rule_ids) == {1, 2}


def test_ordering_is_stable_across_runs():
    rules = [rule(i, "fee_formula", "UPI", fee_pred(rate=i / 100,
                                                    instrument="UPI"), 100)
             for i in (3, 1, 2)]
    once = [r.rule_id for r in topological_order(rules)]
    assert once == [r.rule_id for r in topological_order(list(reversed(rules)))]


# -------------------------------------------------------------- overlaps
def test_overlapping_tolerance_intervals_are_detected():
    a = rule(1, "fee_formula", "UPI", fee_pred(rate=0.03, instrument="UPI",
                                               tol=5000))
    b = rule(2, "fee_formula", "UPI", fee_pred(rate=0.0301, instrument="UPI",
                                               tol=5000))
    assert detect_overlaps([a, b])


def test_non_overlapping_rules_are_not_flagged():
    a = rule(1, "fee_formula", "UPI", fee_pred(rate=0.01, instrument="UPI", tol=2))
    b = rule(2, "fee_formula", "UPI", fee_pred(rate=0.09, instrument="UPI", tol=2))
    assert detect_overlaps([a, b]) == []


def test_overlapping_timing_windows_are_detected():
    def tw(rid, lo, hi):
        return rule(rid, "timing_window", "UPI",
                    {"type": "timing_window", "instrument": "UPI",
                     "min_working_days": lo, "max_working_days": hi})
    assert detect_overlaps([tw(1, 1, 3), tw(2, 2, 4)])
    assert detect_overlaps([tw(1, 1, 1), tw(2, 3, 4)]) == []


def test_ruleset_load_skips_an_unparseable_active_rule(tmp_path):
    from src.db import reset_db
    conn = reset_db(tmp_path / "d.db")
    conn.execute("INSERT INTO rules (rule_type, scope_instrument, predicate_json,"
                 " priority) VALUES ('fee_formula','UPI','{\"type\":\"fee_formula\"}',5)")
    conn.commit()
    rs = RuleSet.load(conn)      # must not raise
    assert [r.rule_type for r in rs.rules] == ["exact_id"]
    conn.close()
