"""Bulk-credit disaggregation: which settlements make up this one bank credit?

Bitset DP over Python big integers -- `reachable |= reachable << amount` tests
every partial sum a machine word at a time, which is fast enough that the
interesting problem is not speed but *ambiguity*. If more than one subset hits
the target, saying so is the whole job; picking one at random is how a
reconciliation system quietly corrupts a ledger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from src.config import load

log = logging.getLogger(__name__)

# Above this the bitset gets unwieldy and we switch to meet-in-the-middle.
MAX_DP_BITS = 40_000_000
MITM_HALF_LIMIT = 22          # 2**22 = 4M sums per half


@dataclass
class SubsetResult:
    solutions: list[list] = field(default_factory=list)   # each a list of item ids
    sums: list[int] = field(default_factory=list)
    method: str = "bitset_dp"
    truncated: bool = False    # more solutions existed than we enumerated

    @property
    def ambiguous(self) -> bool:
        return len(self.solutions) > 1

    @property
    def found(self) -> bool:
        return bool(self.solutions)


def _shift(bits: int, amount: int) -> int:
    return bits << amount if amount >= 0 else bits >> -amount


def solve(target: int, items, delta: int | None = None,
          max_solutions: int | None = None) -> SubsetResult:
    """Subsets of `items` summing into [target-delta, target+delta].

    `items` is an iterable of (id, amount_paise). Amounts may be negative --
    a chargeback reversal rides in the same payout batch as ordinary credits.
    """
    cfg = load()["matching"]
    delta = cfg["subset_sum_delta_paise"] if delta is None else delta
    max_solutions = cfg["max_solutions"] if max_solutions is None else max_solutions

    items = [(i, int(a)) for i, a in items]
    if not items:
        return SubsetResult()

    neg = sum(a for _, a in items if a < 0)
    pos = sum(a for _, a in items if a > 0)
    if not (neg - delta <= target <= pos + delta):
        return SubsetResult()          # unreachable before we do any work

    # The DP's real cost is the width of the bitset, not the span of the pool.
    # With no negative amounts we can mask everything above target+delta, so
    # the width is bounded by the target however large the pool's total is --
    # a 45-row payout batch summing to crores is still a cheap DP.
    width = (target + delta - neg) if neg == 0 else (pos - neg)
    if width > MAX_DP_BITS:
        try:
            return _meet_in_the_middle(target, items, delta, max_solutions)
        except ValueError as e:
            # Too big for either method. Report it as unsolved rather than
            # killing the batch: an unexplained credit is an exception, and
            # exceptions are the thing this system is allowed to produce.
            log.warning("subset sum gave up on target %s: %s", target, e)
            return SubsetResult(method="intractable")
    return _bitset_dp(target, items, delta, max_solutions, neg)


def _bitset_dp(target, items, delta, max_solutions, neg) -> SubsetResult:
    """`prev[k]` is the set of sums reachable using only the first k items,
    which is what lets us walk the choices back out again."""
    offset = -neg                       # bit index = sum + offset, always >= 0
    # Only mask when everything is non-negative: with a negative item present a
    # partial sum may legitimately overshoot the target and come back down.
    mask = ((1 << (target + delta + offset + 1)) - 1) if neg == 0 else None

    reachable = 1 << offset             # the empty subset sums to 0
    prev = [reachable]
    for _, a in items:
        reachable |= _shift(reachable, a)
        if mask is not None:
            reachable &= mask
        prev.append(reachable)

    hits = [s for s in range(target - delta, target + delta + 1)
            if 0 <= s + offset and (reachable >> (s + offset)) & 1]
    if not hits:
        return SubsetResult()

    # Prefer an exact hit, then the nearest misses.
    hits.sort(key=lambda s: (abs(s - target), s))

    result = SubsetResult()
    for s in hits:
        for subset in _recover(s, items, prev, offset,
                               max_solutions - len(result.solutions)):
            result.solutions.append(subset)
            result.sums.append(s)
            if len(result.solutions) >= max_solutions:
                result.truncated = True
                return result
    return result


def _recover(target_sum, items, prev, offset, limit):
    """Walk `prev` backwards, branching wherever an item is optional, to yield
    up to `limit` distinct subsets. Reachability prunes every dead branch."""
    out = []

    def walk(k, remaining, chosen):
        if len(out) >= limit:
            return
        if k == 0:
            if remaining == 0:
                out.append(list(reversed(chosen)))
            return
        idx, amount = items[k - 1]
        # branch 1: item k-1 is not in this subset
        bit = remaining + offset
        if 0 <= bit and (prev[k - 1] >> bit) & 1:
            walk(k - 1, remaining, chosen)
        # branch 2: it is
        rest = remaining - amount
        bit = rest + offset
        if 0 <= bit and (prev[k - 1] >> bit) & 1:
            chosen.append(idx)
            walk(k - 1, rest, chosen)
            chosen.pop()

    walk(len(items), target_sum, [])
    return out


def _meet_in_the_middle(target, items, delta, max_solutions) -> SubsetResult:
    """Fallback for pools too large for the DP range: enumerate each half's
    sums, sort, and two-pointer across the tolerance window."""
    half = len(items) // 2
    if max(half, len(items) - half) > MITM_HALF_LIMIT:
        raise ValueError(f"pool of {len(items)} is too large to enumerate")

    def sums_of(part):
        acc = [(0, ())]
        for idx, a in part:
            acc += [(s + a, m + (idx,)) for s, m in acc]
        return sorted(acc)

    left, right = sums_of(items[:half]), sums_of(items[half:])
    rsums = [s for s, _ in right]
    result = SubsetResult(method="meet_in_the_middle")
    from bisect import bisect_left, bisect_right
    for ls, lm in left:
        lo = bisect_left(rsums, target - delta - ls)
        hi = bisect_right(rsums, target + delta - ls)
        for rs, rm in right[lo:hi]:
            result.solutions.append(list(lm + rm))
            result.sums.append(ls + rs)
            if len(result.solutions) >= max_solutions:
                result.truncated = True
                return result
    return result


# ------------------------------------------------------------- tiebreaking
def disambiguate(result: SubsetResult, rows_by_id: dict):
    """Apply the declared tiebreakers in order.

    -> (chosen_subset | None, confidence, reason). None means genuinely
    ambiguous: escalate rather than guess.
    """
    if not result.found:
        return None, 0.0, "NO_SUBSET_FOUND"
    if result.truncated:
        # We stopped enumerating at the cap, so we are holding an arbitrary
        # sample of the solution set and the true answer may not even be in
        # it. Tiebreaking a sample is how you get a confident wrong answer:
        # this exact path once bound a bank credit to an 8-member subset drawn
        # from four different payout batches, because that happened to be the
        # smallest of the five we had looked at. There is nothing to break a
        # tie between; escalate.
        return None, 0.0, "AMBIGUOUS_SUBSET"
    if len(result.solutions) == 1:
        return result.solutions[0], 0.99, "unique subset"

    cands = list(result.solutions)

    # (a) every member drawn from the same settlement batch
    def one_batch(sub):
        return len({rows_by_id[i]["settlement_batch_id"] for i in sub}) == 1
    same = [s for s in cands if one_batch(s)]
    if len(same) == 1:
        return same[0], 0.85, "unique subset sharing one settlement batch"
    cands = same or cands

    # (b) smallest spread of settlement dates
    def spread(sub):
        ds = [date.fromisoformat(str(rows_by_id[i]["settled_datetime"])[:10])
              for i in sub]
        return (max(ds) - min(ds)).days
    best = min(spread(s) for s in cands)
    tight = [s for s in cands if spread(s) == best]
    if len(tight) == 1:
        return tight[0], 0.75, f"unique subset with minimal date spread ({best}d)"
    cands = tight

    # (c) fewest members
    fewest = min(len(s) for s in cands)
    small = [s for s in cands if len(s) == fewest]
    if len(small) == 1:
        return small[0], 0.65, f"unique subset with fewest members ({fewest})"

    return None, 0.0, "AMBIGUOUS_SUBSET"
