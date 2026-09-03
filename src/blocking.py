"""Candidate generation. Never builds the full n x m matrix.

Three stages, cheapest first:
  1. hash join on the claimed order id                    -> O(1) per record
  2. for survivors, a sorted-array + binary search on amount, then a
     working-day window filter                            -> O(log n + k)
  3. Union-Find over the surviving pairs, so each connected component becomes
     an independent little assignment problem instead of one big one.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import date

from src.calendar_utils import working_days_between
from src.config import load


class DSU:
    """Union-Find with path compression and union by rank."""

    def __init__(self, items=()):
        self.parent = {i: i for i in items}
        self.rank = dict.fromkeys(self.parent, 0)

    def add(self, x):
        self.parent.setdefault(x, x)
        self.rank.setdefault(x, 0)

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:      # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True

    def components(self) -> list[list]:
        groups: dict = {}
        for x in self.parent:
            groups.setdefault(self.find(x), []).append(x)
        return [sorted(v, key=str) for v in groups.values()]


def _day(v) -> date:
    return date.fromisoformat(str(v)[:10])


class AmountIndex:
    """Sorted array of (amount, row) supporting a window lookup by binary search."""

    def __init__(self, rows, key):
        self.entries = sorted(((int(r[key]), r) for r in rows),
                              key=lambda t: t[0])
        self.amounts = [a for a, _ in self.entries]

    def window(self, lo: int, hi: int):
        i = bisect_left(self.amounts, lo)
        j = bisect_right(self.amounts, hi)
        return [r for _, r in self.entries[i:j]]


def hash_join(orders, settlements):
    """Stage 1. -> (pairs, unmatched_orders, unmatched_settlements).

    Note this only proves the ids agree; the amount and timing still have to be
    explained by a rule before it counts as a match.
    """
    by_id: dict[str, list] = {}
    for s in settlements:
        by_id.setdefault(s["order_id_claimed"], []).append(s)

    pairs, leftover_orders = [], []
    claimed = set()
    for o in orders:
        hits = by_id.get(o["order_id"])
        if hits:
            for s in hits:
                pairs.append((o, s))
                claimed.add(s["settlement_txn_id"])
        else:
            leftover_orders.append(o)
    leftover_settlements = [s for s in settlements
                            if s["settlement_txn_id"] not in claimed]
    return pairs, leftover_orders, leftover_settlements


def candidate_pairs(orders, settlements, amount_tolerance=None,
                    max_lag_days=None, gross_slack_ratio=0.6):
    """Stage 2. Pairs an order could plausibly belong to, by amount then date.

    The amount window is deliberately wide (`gross_slack_ratio`) because at the
    start we do not know the fee structure -- a settlement's net can sit well
    below its gross. That is the price of not being told the answer.
    """
    cfg = load()["matching"]
    tol = cfg["amount_tolerance_paise"] if amount_tolerance is None else amount_tolerance
    max_lag = cfg["date_window_working_days"] if max_lag_days is None else max_lag_days

    index = AmountIndex(settlements, "net_amount_paise")
    pairs = []
    for o in orders:
        gross = int(o["gross_amount_paise"])
        lo, hi = sorted((gross - int(abs(gross) * gross_slack_ratio) - tol,
                         gross + tol))
        od = _day(o["order_datetime"])
        for s in index.window(lo, hi):
            lag = working_days_between(od, _day(s["settled_datetime"]))
            if 0 <= lag <= max_lag:
                pairs.append((o, s))
    return pairs


def components(pairs, extra_nodes=()):
    """Stage 3. Connected components over the candidate graph.

    -> list of (orders, settlements) subproblems, each solvable independently.
    """
    dsu = DSU()
    node_of = {}
    for o, s in pairs:
        a = ("order", o["order_id"])
        b = ("settlement", s["settlement_txn_id"])
        node_of[a], node_of[b] = o, s
        dsu.union(a, b)
    for kind, row, key in extra_nodes:
        n = (kind, row[key])
        node_of[n] = row
        dsu.add(n)

    out = []
    for comp in dsu.components():
        os_ = [node_of[n] for n in comp if n[0] == "order"]
        ss = [node_of[n] for n in comp if n[0] == "settlement"]
        out.append((os_, ss))
    return out


def size_distribution(comps) -> dict[int, int]:
    """Component size histogram -- logged every run; the pitch wants this."""
    return dict(sorted(Counter(len(o) + len(s) for o, s in comps).items()))
