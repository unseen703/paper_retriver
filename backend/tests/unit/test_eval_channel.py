"""
R6.15 -- how many recoveries came only from the embedding channel?

Journey:

    As the person deciding whether R6 earned its place, I want the count of
    ground-truth papers only the embedding channel recovered, with the temporal
    guard applied to that channel too, so the number is not inflated by the future.
"""

from __future__ import annotations

from channel import ChannelRecovery, recovery, summarize


def test_embedding_only_excludes_what_the_graph_pool_holds() -> None:
    r = recovery(truth={1, 2, 3, 4}, graph_pool={1, 2, 9}, embedding_ids={2, 3, 8})
    assert r == ChannelRecovery(truth=4, graph=2, embedding=2, embedding_only=1, both=1)


def test_papers_outside_the_truth_never_count() -> None:
    r = recovery(truth={1}, graph_pool={5, 6}, embedding_ids={7, 8})
    assert (r.graph, r.embedding, r.embedding_only) == (0, 0, 0)


def test_summary_sums_cases_and_reports_the_share() -> None:
    a = ChannelRecovery(truth=4, graph=2, embedding=2, embedding_only=1, both=1)
    b = ChannelRecovery(truth=2, graph=1, embedding=0, embedding_only=0, both=0)
    s = summarize([a, b])
    assert (s["truth"], s["graph"], s["embedding_only"]) == (6, 3, 1)
    assert s["embedding_only_share_of_found"] == 0.25  # 1 of (3 graph + 1 only-embedding)


def test_zero_recoveries_is_a_zero_share_not_a_division_error() -> None:
    assert summarize([])["embedding_only_share_of_found"] == 0.0
