from app.services.communities import detect_communities


def _clique(ids: list[int]) -> list[tuple[int, int]]:
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1 :]]


TWO_CLUSTERS = _clique([1, 2, 3, 4]) + _clique([11, 12, 13]) + [(4, 11)]


def test_two_cliques_joined_by_a_bridge_split_in_two() -> None:
    got = detect_communities([1, 2, 3, 4, 11, 12, 13], TWO_CLUSTERS)
    assert len({got[i] for i in (1, 2, 3, 4)}) == 1
    assert len({got[i] for i in (11, 12, 13)}) == 1
    assert got[1] != got[11]


def test_ids_are_ordered_by_size_so_zero_is_the_biggest() -> None:
    got = detect_communities([1, 2, 3, 4, 11, 12, 13], TWO_CLUSTERS)
    assert got[1] == 0
    assert got[11] == 1


def test_deterministic_regardless_of_input_order() -> None:
    nodes = [1, 2, 3, 4, 11, 12, 13]
    a = detect_communities(nodes, TWO_CLUSTERS)
    b = detect_communities(reversed(nodes), reversed(TWO_CLUSTERS))
    assert a == b


def test_direction_is_irrelevant() -> None:
    nodes = [1, 2, 3, 4, 11, 12, 13]
    flipped = [(b, a) for a, b in TWO_CLUSTERS]
    assert detect_communities(nodes, TWO_CLUSTERS) == detect_communities(nodes, flipped)


def test_isolated_node_gets_its_own_community() -> None:
    got = detect_communities([1, 2, 3, 99], _clique([1, 2, 3]))
    assert 99 in got
    assert got[99] not in {got[1], got[2], got[3]}


def test_edges_outside_the_node_set_and_self_loops_are_ignored() -> None:
    got = detect_communities([1, 2], [(1, 2), (1, 1), (2, 500)])
    assert set(got) == {1, 2}
    assert got[1] == got[2]


def test_empty_graph() -> None:
    assert detect_communities([], []) == {}
