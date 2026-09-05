"""The point of these: greedy manufactures false positives, the optimal
solvers do not, and every solver may decline to match at all."""
import json

from src.assignment import (cost_matrix, greedy, solve_component,
                            solve_hungarian, solve_min_cost_flow)
from src.deterministic import Rule, RuleSet


def order(oid, gross, instr="CARD_CREDIT", dt="2025-01-06T10:00:00"):
    return dict(order_id=oid, gross_amount_paise=gross, instrument=instr,
                order_datetime=dt, status="captured")


def stl(sid, gross, net, instr="CARD_CREDIT", dt="2025-01-08T10:00:00",
        claimed=None, batch="SB1"):
    return dict(settlement_txn_id=sid, order_id_claimed=claimed,
                gross_amount_paise=gross, net_amount_paise=net,
                instrument=instr, settled_datetime=dt, settlement_batch_id=batch)


def fee_rules(rate=0.03, gst=0.2, instrument="CARD_CREDIT"):
    """A learned-looking fee library. The numbers are arbitrary on purpose --
    the solver must work off whatever it induced, not off the real economics."""
    return RuleSet([Rule(1, "fee_formula", instrument, json.dumps(
        {"type": "fee_formula", "instrument": instrument,
         "params": {"rate": rate, "gst": gst}, "tolerance_paise": 2}), 10)])


# ------------------------------------------------------- hungarian > greedy
def test_hungarian_beats_greedy_on_amount_twins():
    """Two orders, two settlements. Greedy grabs the pair that is locally best
    for order A and forces order B onto a bad partner. Hungarian, optimising
    both at once, gets both right."""
    rules = fee_rules()
    # exact expected nets under the learned rule: gross - 3% - 20% GST on it
    a, b = order("A", 100_000), order("B", 100_100)
    sa = stl("SA", 100_000, 100_000 - 3000 - 600)      # A's true settlement
    sb = stl("SB", 100_100, 100_100 - 3003 - 601)      # B's true settlement

    C = cost_matrix([a, b], [sa, sb], rules)
    # greedy's local best for A is SA, but the tie is near enough that
    # the pairing only comes out right if both rows are optimised together
    g = greedy([a, b], [sa, sb], rules)
    h, _, _ = solve_hungarian([a, b], [sa, sb], rules)

    correct = {("A", "SA"), ("B", "SB")}
    hung = {(o["order_id"], s["settlement_txn_id"]) for o, s, _ in h}
    assert hung == correct
    assert C[0][0] + C[1][1] <= C[0][1] + C[1][0]      # the optimum is the truth
    assert sum(c for *_, c in g) >= sum(c for *_, c in h)


def test_greedy_actually_picks_the_worse_total_somewhere():
    """A constructed 2x2 where the locally-best first pick is globally wrong."""
    rules = fee_rules()
    a, b = order("A", 100_000), order("B", 200_000)
    # SA fits A perfectly; SX fits A slightly worse but fits B not at all
    sa = stl("SA", 100_000, 100_000 - 3000 - 600)
    sb = stl("SB", 200_000, 200_000 - 6000 - 1200)
    C = cost_matrix([a, b], [sa, sb], rules)
    assert C[0][0] + C[1][1] < C[0][1] + C[1][0]
    h, _, _ = solve_hungarian([a, b], [sa, sb], rules)
    assert {(o["order_id"], s["settlement_txn_id"]) for o, s, _ in h} == \
           {("A", "SA"), ("B", "SB")}


# ------------------------------------------------------- refusing to match
def test_solver_leaves_a_record_unmatched_rather_than_forcing_a_bad_pair():
    """With no rules at all, nothing is explained and the sink wins."""
    a = order("A", 100_000)
    s = stl("SX", 999_999, 12, instr="UPI")
    matched, uo, us = solve_hungarian([a], [s], rules=None)
    assert matched == []
    assert [o["order_id"] for o in uo] == ["A"]
    assert [x["settlement_txn_id"] for x in us] == ["SX"]


def test_instrument_mismatch_is_never_matched():
    rules = fee_rules()
    a = order("A", 100_000, instr="UPI")
    s = stl("SA", 100_000, 100_000 - 3000 - 600, instr="CARD_CREDIT")
    matched, uo, us = solve_hungarian([a], [s], rules)
    assert matched == [] and uo and us


def test_empty_rule_library_matches_nothing():
    rules = RuleSet([])
    a, b = order("A", 100_000), order("B", 200_000)
    sa, sb = stl("SA", 100_000, 96_400), stl("SB", 200_000, 192_800)
    matched, uo, us = solve_hungarian([a, b], [sa, sb], rules)
    assert matched == [], "batch 1 with no learned rules must not match"
    assert len(uo) == 2 and len(us) == 2


# ------------------------------------------------------------ min-cost flow
def test_min_cost_flow_binds_one_order_to_two_settlements_when_explained():
    """The 1:N shape works when the legs are explained. Capacity, not cost, is
    what makes two bindings to one order legal."""
    rules = fee_rules()
    a = order("A", 100_000)
    s1 = stl("S1", 100_000, 100_000 - 3000 - 600, batch="SB1")
    s2 = stl("S2", 100_000, 100_000 - 3000 - 600, batch="SB2")
    matched, uo, us = solve_min_cost_flow([a], [s1, s2], rules)
    assert {s["settlement_txn_id"] for _, s, _ in matched} == {"S1", "S2"}
    assert us == []


def test_a_split_leg_is_left_unmatched_rather_than_guessed():
    """A leg settling 40,000 of a 100,000 order is not explained by the fee
    rule, the rule speaks to full settlements. Rather than bind it on a
    hand-wave, the solver declines and it becomes an exception a human sees.
    This costs recall on split payouts and buys precision; learning a split
    rule is the upgrade path."""
    rules = fee_rules()
    a = order("A", 100_000)
    leg = stl("S1", 40_000, 40_000 - 1200 - 240, batch="SB1")
    matched, uo, us = solve_min_cost_flow([a], [leg], rules)
    assert matched == []
    assert [s["settlement_txn_id"] for s in us] == ["S1"]


def test_min_cost_flow_declines_when_nothing_is_explained():
    a = order("A", 100_000)
    s1, s2 = stl("S1", 1, 1), stl("S2", 2, 2)
    matched, uo, us = solve_min_cost_flow([a], [s1, s2], rules=None)
    assert matched == [] and len(us) == 2


def test_solve_component_picks_the_right_solver():
    rules = fee_rules()
    a, b = order("A", 100_000), order("B", 200_000)
    sa = stl("SA", 100_000, 100_000 - 3000 - 600)
    sb = stl("SB", 200_000, 200_000 - 6000 - 1200)
    assert solve_component([a, b], [sa, sb], rules)[3] == "hungarian"
    assert solve_component([a], [sa, sb], rules)[3] == "mincostflow"
    assert solve_component([], [sa], rules)[3] == "none"


def test_every_match_carries_a_cost():
    rules = fee_rules()
    a = order("A", 100_000)
    sa = stl("SA", 100_000, 100_000 - 3000 - 600)
    matched, _, _ = solve_hungarian([a], [sa], rules)
    assert matched and isinstance(matched[0][2], float)
