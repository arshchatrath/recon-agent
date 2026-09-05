import pytest

from src.subset_sum import _meet_in_the_middle, disambiguate, solve


def items(*amounts):
    return [(f"S{i}", a) for i, a in enumerate(amounts)]


def rows(mapping):
    """id -> {settlement_batch_id, settled_datetime}"""
    return {k: dict(settlement_batch_id=v[0], settled_datetime=v[1])
            for k, v in mapping.items()}


def test_clean_bulk_credit_recovers_the_exact_subset():
    r = solve(500, items(100, 200, 300, 700), delta=0)
    assert r.found and not r.ambiguous
    assert sorted(r.solutions[0]) == ["S1", "S2"]     # 200 + 300
    assert r.sums[0] == 500


def test_whole_pool_is_a_valid_subset():
    r = solve(600, items(100, 200, 300), delta=0)
    assert sorted(r.solutions[0]) == ["S0", "S1", "S2"]


def test_unreachable_target_reports_nothing_found():
    r = solve(999_999, items(100, 200), delta=0)
    assert not r.found
    assert disambiguate(r, {})[2] == "NO_SUBSET_FOUND"


def test_tolerance_window_accepts_a_near_miss_and_prefers_the_exact_hit():
    r = solve(302, items(100, 200, 700), delta=3)
    assert r.found
    assert r.sums[0] == 300            # nearest achievable sum, inside delta
    assert sorted(r.solutions[0]) == ["S0", "S1"]


def test_coincidental_subset_is_reported_as_ambiguous_not_guessed():
    # 100+200+300 == 150+450 == 600: two valid answers.
    pool = [("A1", 100), ("A2", 200), ("A3", 300), ("B1", 150), ("B2", 450)]
    r = solve(600, pool, delta=0)
    assert r.ambiguous, r.solutions
    found = {tuple(sorted(s)) for s in r.solutions}
    assert ("A1", "A2", "A3") in found and ("B1", "B2") in found


def test_negative_amounts_are_handled_for_chargeback_reversals():
    # a payout batch carrying a reversal: 500 + 300 - 200 = 600
    r = solve(600, [("P1", 500), ("P2", 300), ("CB", -200)], delta=0)
    assert r.found
    assert sorted(r.solutions[0]) == ["CB", "P1", "P2"]


def test_a_purely_negative_target_is_solvable():
    r = solve(-250, [("CB1", -250), ("P1", 400)], delta=0)
    assert r.solutions[0] == ["CB1"]


def test_solution_count_is_capped():
    r = solve(4, items(1, 1, 1, 1, 2, 2, 2, 3, 4), delta=0, max_solutions=3)
    assert len(r.solutions) == 3 and r.truncated


def test_empty_pool():
    assert not solve(100, []).found


# ------------------------------------------------------------ tiebreakers
def test_single_solution_is_confident():
    r = solve(500, items(100, 200, 300, 700), delta=0)
    chosen, conf, why = disambiguate(r, rows({
        "S1": ("SB1", "2025-01-06"), "S2": ("SB1", "2025-01-06")}))
    assert sorted(chosen) == ["S1", "S2"] and conf > 0.9


def test_tiebreak_a_prefers_one_settlement_batch():
    pool = [("A1", 100), ("A2", 200), ("A3", 300), ("B1", 150), ("B2", 450)]
    r = solve(600, pool, delta=0)
    meta = rows({"A1": ("SB1", "2025-01-06"), "A2": ("SB1", "2025-01-06"),
                 "A3": ("SB1", "2025-01-06"), "B1": ("SB1", "2025-01-06"),
                 "B2": ("SB2", "2025-01-09")})
    chosen, conf, why = disambiguate(r, meta)
    assert sorted(chosen) == ["A1", "A2", "A3"]
    assert conf < 0.99 and "settlement batch" in why


def test_tiebreak_b_prefers_the_tightest_date_spread():
    pool = [("A1", 100), ("A2", 200), ("A3", 300), ("B1", 150), ("B2", 450)]
    r = solve(600, pool, delta=0)
    meta = rows({"A1": ("SB1", "2025-01-06"), "A2": ("SB2", "2025-01-06"),
                 "A3": ("SB3", "2025-01-06"), "B1": ("SB4", "2025-01-06"),
                 "B2": ("SB5", "2025-01-31")})
    chosen, conf, why = disambiguate(r, meta)
    assert sorted(chosen) == ["A1", "A2", "A3"] and "date spread" in why


def test_genuinely_ambiguous_escalates_rather_than_guessing():
    # two solutions, identical batch story, identical dates, identical size
    pool = [("A1", 300), ("A2", 300), ("B1", 300), ("B2", 300)]
    r = solve(600, pool, delta=0, max_solutions=5)
    meta = rows({k: ("SB1", "2025-01-06") for k in ("A1", "A2", "B1", "B2")})
    chosen, conf, why = disambiguate(r, meta)
    assert chosen is None and conf == 0.0 and why == "AMBIGUOUS_SUBSET"


# ---------------------------------------------------- meet in the middle
def test_meet_in_the_middle_agrees_with_the_bitset_dp():
    pool = items(11, 23, 37, 41, 59, 67, 71, 83)
    target = 11 + 41 + 83
    dp = solve(target, pool, delta=0, max_solutions=20)
    mitm = _meet_in_the_middle(target, [(i, a) for i, a in pool], 0, 20)
    assert {tuple(sorted(s)) for s in dp.solutions} == \
           {tuple(sorted(s)) for s in mitm.solutions}
    assert mitm.method == "meet_in_the_middle"


def test_meet_in_the_middle_refuses_an_unenumerable_pool():
    with pytest.raises(ValueError):
        _meet_in_the_middle(1, [(f"S{i}", i + 1) for i in range(60)], 0, 1)
