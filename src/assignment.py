"""Globally optimal matching within a component. Never greedy.

Greedy takes the locally cheapest pair and, on amount twins, binds the wrong
one, a false positive, which is the expensive kind of error here. Hungarian
(1:1) and min-cost max-flow (1:N) both optimise the whole component at once.

Every node also gets an edge to an "unmatched" sink priced at
`assignment.unmatched_sink_cost`. That edge is the mathematical statement of
"be conservative with money": if no pairing is worth less than the sink, the
solver is free to leave the record unmatched, and it will.
"""
from __future__ import annotations

import logging
from datetime import date

import networkx as nx
import numpy as np
from scipy.optimize import linear_sum_assignment

from src.calendar_utils import working_days_between
from src.config import load
from src.deterministic import evaluate

log = logging.getLogger(__name__)
SCALE = 1000          # min_cost_flow needs integer weights


def _day(v) -> date:
    return date.fromisoformat(str(v)[:10])


def _field(row, key, default=None):
    """Rows are dicts or sqlite3.Row; neither .get nor [] works on both."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def pair_cost(order, settlement, rules=None, cfg=None) -> float:
    """Cost of binding this order to this settlement. Lower is better.

    Every term that depends on the merchant's economics comes from `rules`,
    which is the learned library, with an empty library the amount and date
    terms contribute nothing and the unexplained-pair penalty dominates, so
    batch 1 correctly refuses to match much of anything.
    """
    cfg = cfg or load()["assignment"]
    gross = int(order["gross_amount_paise"])
    net = int(settlement["net_amount_paise"])
    instr = settlement["instrument"]

    cost = 0.0
    if order["instrument"] != instr:
        cost += cfg["w_instrument"] * cfg["instrument_mismatch_big"]

    # The merchant's own ledger says this order was refunded in full, so no
    # settlement should exist for it. Binding one anyway invents money.
    if _field(order, "status") == "refunded_full":
        cost += cfg["w_instrument"] * cfg["instrument_mismatch_big"]

    expected_net = rules.expected_net(gross, instr) if rules else None
    if expected_net is not None and gross:
        # normalised so a big-ticket order is not automatically expensive
        cost += cfg["w_amount"] * min(abs(expected_net - net) / abs(gross), 1.0)

    window = rules.expected_lag(instr) if rules else None
    if window is not None:
        lag = working_days_between(_day(order["order_datetime"]),
                                   _day(settlement["settled_datetime"]))
        lo, hi = window
        cost += cfg["w_date"] * max(lo - lag, lag - hi, 0)

    # Only a rule that accounts for the MONEY explains a pair, the same test
    # pipeline.explain_amount applies. A timing window agrees with any two
    # records settled on the usual day, whatever their amounts, so letting it
    # count would bind an unrelated order and settlement at cost ~0.
    explained = rules is not None and any(
        evaluate(r.predicate, order, settlement, rules) is True
        for r in rules.rules if r.rule_type in ("fee_formula", "refund_pattern"))
    if not explained:
        cost += cfg["w_rule"] * cfg["unexplained_penalty"]
    return cost


def cost_matrix(orders, settlements, rules=None, cfg=None) -> np.ndarray:
    cfg = cfg or load()["assignment"]
    return np.array([[pair_cost(o, s, rules, cfg) for s in settlements]
                     for o in orders], dtype=float) if orders and settlements \
        else np.zeros((len(orders), len(settlements)))


def solve_hungarian(orders, settlements, rules=None):
    """Square 1:1 case. Padded with sink rows/columns so the solver may decline
    to match rather than being forced into a bad pairing."""
    cfg = load()["assignment"]
    sink = cfg["unmatched_sink_cost"]
    n, m = len(orders), len(settlements)
    size = n + m
    C = np.full((size, size), 0.0)
    C[:n, :m] = cost_matrix(orders, settlements, rules, cfg)
    C[:n, m:] = sink          # order i -> unmatched
    C[n:, :m] = sink          # settlement j -> unmatched
    rows, cols = linear_sum_assignment(C)

    matched, unmatched_o, unmatched_s = [], [], []
    taken_s = set()
    for i, j in zip(rows, cols):
        if i < n and j < m:
            matched.append((orders[i], settlements[j], float(C[i, j])))
            taken_s.add(j)
        elif i < n:
            unmatched_o.append(orders[i])
    unmatched_s = [settlements[j] for j in range(m) if j not in taken_s]
    return matched, unmatched_o, unmatched_s


def solve_min_cost_flow(orders, settlements, rules=None, max_bindings=None):
    """One-to-many / many-to-one. Each settlement must be explained by exactly
    one unit of flow: either an order, or the expensive `unmatched` bypass."""
    cfg = load()["assignment"]
    max_bindings = max_bindings or cfg["max_bindings_per_order"]
    sink_cost = int(cfg["unmatched_sink_cost"] * SCALE)

    G = nx.DiGraph()
    F = len(settlements)
    G.add_node("S", demand=-F)
    G.add_node("T", demand=F)
    for i, o in enumerate(orders):
        G.add_edge("S", ("o", i), capacity=max_bindings, weight=0)
    for j, s in enumerate(settlements):
        G.add_edge(("s", j), "T", capacity=1, weight=0)
        G.add_edge("S", ("s", j), capacity=1, weight=sink_cost)   # leave unmatched
    for i, o in enumerate(orders):
        for j, s in enumerate(settlements):
            c = pair_cost(o, s, rules, cfg)
            if c < cfg["unmatched_sink_cost"]:      # never offer a worse-than-sink edge
                G.add_edge(("o", i), ("s", j), capacity=1, weight=int(c * SCALE))

    flow = nx.min_cost_flow(G)
    matched, taken_s = [], set()
    for i in range(len(orders)):
        for tgt, f in flow.get(("o", i), {}).items():
            if f and isinstance(tgt, tuple) and tgt[0] == "s":
                j = tgt[1]
                matched.append((orders[i], settlements[j],
                                G[("o", i)][tgt]["weight"] / SCALE))
                taken_s.add(j)
    unmatched_o = [o for i, o in enumerate(orders)
                   if not any(f and isinstance(t, tuple) and t[0] == "s"
                              for t, f in flow.get(("o", i), {}).items())]
    unmatched_s = [s for j, s in enumerate(settlements) if j not in taken_s]
    return matched, unmatched_o, unmatched_s


def solve_component(orders, settlements, rules=None):
    """Pick the solver the shape of the component calls for.

    -> (matched, unmatched_orders, unmatched_settlements, method)
    """
    if not orders or not settlements:
        return [], list(orders), list(settlements), "none"
    if len(orders) == len(settlements):
        m, uo, us = solve_hungarian(orders, settlements, rules)
        return m, uo, us, "hungarian"
    m, uo, us = solve_min_cost_flow(orders, settlements, rules)
    return m, uo, us, "mincostflow"
