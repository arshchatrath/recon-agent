from src.blocking import (DSU, AmountIndex, candidate_pairs, components,
                          hash_join, size_distribution)


def order(oid, gross, instr="UPI", dt="2025-01-06T10:00:00"):
    return dict(order_id=oid, gross_amount_paise=gross, instrument=instr,
                order_datetime=dt)


def stl(sid, net, claimed=None, dt="2025-01-07T10:00:00", instr="UPI"):
    return dict(settlement_txn_id=sid, order_id_claimed=claimed,
                net_amount_paise=net, settled_datetime=dt, instrument=instr)


# ------------------------------------------------------------------- DSU
def test_dsu_partitions_a_graph_correctly():
    d = DSU()
    for a, b in [(1, 2), (2, 3), (5, 6)]:
        d.union(a, b)
    d.add(9)
    comps = {tuple(sorted(c, key=str)) for c in d.components()}
    assert comps == {(1, 2, 3), (5, 6), (9,)}


def test_dsu_union_reports_whether_it_merged():
    d = DSU()
    assert d.union("a", "b") is True
    assert d.union("a", "b") is False


def test_dsu_path_compression_keeps_finds_consistent():
    d = DSU()
    for i in range(1, 200):
        d.union(i, i + 1)
    assert len({d.find(i) for i in range(1, 201)}) == 1


# --------------------------------------------------------- amount index
def test_amount_index_window_is_inclusive():
    idx = AmountIndex([stl("A", 100), stl("B", 200), stl("C", 300)],
                      "net_amount_paise")
    assert [r["settlement_txn_id"] for r in idx.window(200, 300)] == ["B", "C"]
    assert idx.window(400, 500) == []


# ------------------------------------------------------------ hash join
def test_hash_join_resolves_matching_ids_and_reports_the_rest():
    orders = [order("O1", 100), order("O2", 200)]
    settlements = [stl("S1", 100, claimed="O1"), stl("S9", 900, claimed="NOPE")]
    pairs, lo, ls = hash_join(orders, settlements)
    assert [(o["order_id"], s["settlement_txn_id"]) for o, s in pairs] == \
           [("O1", "S1")]
    assert [o["order_id"] for o in lo] == ["O2"]
    assert [s["settlement_txn_id"] for s in ls] == ["S9"]


def test_hash_join_keeps_both_legs_of_a_split_settlement():
    orders = [order("O1", 100)]
    settlements = [stl("S1", 40, claimed="O1"), stl("S2", 60, claimed="O1")]
    pairs, lo, ls = hash_join(orders, settlements)
    assert len(pairs) == 2 and not lo and not ls


# ------------------------------------------------------- candidate pairs
def test_candidates_are_filtered_by_amount_and_date_window():
    o = order("O1", 100_000)
    near = stl("NEAR", 96_400)                       # plausible after fees
    far = stl("FAR", 5_000)                          # far below the window
    late = stl("LATE", 96_400, dt="2025-03-01T10:00:00")   # outside the lag window
    pairs = candidate_pairs([o], [near, far, late])
    assert [s["settlement_txn_id"] for _, s in pairs] == ["NEAR"]


def test_a_settlement_before_its_order_is_not_a_candidate():
    o = order("O1", 100_000, dt="2025-01-20T10:00:00")
    early = stl("EARLY", 96_400, dt="2025-01-06T10:00:00")
    assert candidate_pairs([o], [early]) == []


# ----------------------------------------------------------- components
def test_components_become_independent_subproblems():
    o1, o2, o3 = order("O1", 100), order("O2", 200), order("O3", 300)
    s1, s2 = stl("S1", 100), stl("S2", 200)
    pairs = [(o1, s1), (o2, s1), (o3, s2)]      # O1/O2 contend for S1
    comps = components(pairs)
    sizes = sorted(len(o) + len(s) for o, s in comps)
    assert sizes == [2, 3]
    big = [c for c in comps if len(c[0]) == 2][0]
    assert {o["order_id"] for o in big[0]} == {"O1", "O2"}


def test_isolated_records_still_appear_as_their_own_component():
    lonely = stl("S9", 999)
    comps = components([], extra_nodes=[("settlement", lonely,
                                         "settlement_txn_id")])
    assert comps == [([], [lonely])]


def test_size_distribution_is_a_histogram():
    o1, o2 = order("O1", 100), order("O2", 200)
    s1, s2 = stl("S1", 100), stl("S2", 200)
    dist = size_distribution(components([(o1, s1), (o2, s2)]))
    assert dist == {2: 2}
