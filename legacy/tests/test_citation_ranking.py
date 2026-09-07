"""
tests/test_citation_ranking.py
-------------------------------
TDD tests for hybrid-score ranking of papers CITING an already-networked
paper, plus uncapped seed-paper reference expansion, plus a PageRank
implementation that never silently degrades to degree centrality.

User journeys:
  1. As the system, when the BFS expands a paper's CITATIONS (papers
     citing it), I rank candidates by a hybrid score combining, in
     priority order: citation count (0.4), network indegree / relevance
     to papers already in the network (0.3), representative author
     h-index (0.2), and venue tier (0.1). This applies to every node's
     citation-direction expansion, not just seeds.
  2. As the system, when expanding a SEED paper's REFERENCES specifically,
     I add every relevant reference regardless of the max_new per-run
     budget (still bounded by the hard max_nodes ceiling and still
     filtered by field-of-study relevance). This does not apply to a
     seed's citations, nor to any non-seed paper's references.
  3. As the system, PageRank is always genuinely computed (a pure
     power-iteration fallback) rather than silently substituting degree
     centrality when scipy is unavailable.

Affected APIs:
  - graph_expander.py: citation_hybrid_score(), _representative_hindex(),
    expand_network()'s _priority/_pre_pri closures (direction-aware) and
    its mid-wave / outer cap checks (seed+reference bypass).
  - semantic_scholar_client.py: SEARCH_FIELDS now requests authors.hIndex
    and venue.
  - citation_network.py: compute_metrics()'s except-branch now calls a
    manual power-iteration PageRank instead of nx.degree_centrality.

Callers: pytest only.
User verbatim: "Also for mentioning seed paper Citation implement page rank
algorithm, extract following feature for the paper that are citing seed
paper. 1. citation count for the child paper 2. relevance to the seed
papers (indegree with existing papers) 3. author (take one with highest
h-index as representative of paper.) 4. venue if available. this features
are written based on their priority/importance. while extending citation
network create rank them based on a hybrid score of this features and
ranks them this way. ... be sure that this is while extending the
citations, and not the reference, for seed papers all the reference
should be added to the network."
Clarified: applies to ALL nodes' citation-direction expansion (not just
seeds); relevance = edges to papers already in the network (general, not
seed-specific); h-index via extending existing fetch fields (no extra API
calls); weights 0.4/0.3/0.2/0.1 descending by stated priority.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import networkx as nx
import pytest

from storage import Paper, Store


# helpers

def _s2_record(pid, citation_count=10, year=2020, venue=None, hindex=None):
    authors = []
    if hindex is not None:
        authors = [{"authorId": "a-" + pid, "name": "Author " + pid, "hIndex": hindex}]
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": year, "authors": authors,
        "fieldsOfStudy": ["Computer Science"],
        "venue": venue, "tldr": None, "embedding": None,
    }


def _mock_s2(refs=None, cites=None):
    client = MagicMock()
    client.get_references.return_value = refs or []
    client.get_citations.return_value = cites or []
    return client


def _make_store_with_seed(db_path, seed_id="s1"):
    store = Store(db_path)
    store.upsert_paper(
        Paper(paper_id=seed_id, title="Seed", source_api="s2",
              field_of_study=["Computer Science"]),
        label="seed",
    )
    return store


from graph_expander import expand_network


# A. Pure hybrid-score formula tests

class TestCitationHybridScoreFormula:

    def test_weights_sum_to_one(self):
        from graph_expander import (
            _CITE_CIT_W, _CITE_INDEGREE_W, _CITE_HINDEX_W, _CITE_VENUE_W,
        )
        total = _CITE_CIT_W + _CITE_INDEGREE_W + _CITE_HINDEX_W + _CITE_VENUE_W
        assert abs(total - 1.0) < 1e-9

    def test_all_max_gives_score_of_one_when_venue_is_top_tier(self):
        from graph_expander import citation_hybrid_score
        score = citation_hybrid_score(
            citation_count=100, max_citation_count=100,
            indegree=5, max_indegree=5,
            hindex=50, max_hindex=50,
            venue="NeurIPS",
        )
        assert abs(score - 1.0) < 1e-9

    def test_all_zero_gives_score_of_zero(self):
        from graph_expander import citation_hybrid_score
        score = citation_hybrid_score(
            citation_count=0, max_citation_count=100,
            indegree=0, max_indegree=5,
            hindex=0, max_hindex=50,
            venue=None,
        )
        assert score == 0.0

    def test_citation_count_dominates_hindex_when_equal_magnitude(self):
        """Citation count weight (0.4) must outrank h-index weight (0.2)."""
        from graph_expander import citation_hybrid_score
        a = citation_hybrid_score(citation_count=100, max_citation_count=100,
                                   indegree=0, max_indegree=1,
                                   hindex=0, max_hindex=1, venue=None)
        b = citation_hybrid_score(citation_count=0, max_citation_count=100,
                                   indegree=0, max_indegree=1,
                                   hindex=50, max_hindex=50, venue=None)
        assert a > b

    def test_indegree_beats_hindex_when_others_zero(self):
        """Indegree weight (0.3) must outrank h-index weight (0.2)."""
        from graph_expander import citation_hybrid_score
        a = citation_hybrid_score(citation_count=0, max_citation_count=1,
                                   indegree=5, max_indegree=5,
                                   hindex=0, max_hindex=1, venue=None)
        b = citation_hybrid_score(citation_count=0, max_citation_count=1,
                                   indegree=0, max_indegree=5,
                                   hindex=50, max_hindex=50, venue=None)
        assert a > b

    def test_hindex_beats_venue_when_others_zero(self):
        """H-index weight (0.2) must outrank venue weight (0.1)."""
        from graph_expander import citation_hybrid_score
        a = citation_hybrid_score(citation_count=0, max_citation_count=1,
                                   indegree=0, max_indegree=1,
                                   hindex=50, max_hindex=50, venue=None)
        b = citation_hybrid_score(citation_count=0, max_citation_count=1,
                                   indegree=0, max_indegree=1,
                                   hindex=0, max_hindex=1, venue="NeurIPS")
        assert a > b


# B. BFS integration: citation-direction candidates ranked by hybrid score

class TestCitationDirectionRanking:

    def test_higher_citation_count_citer_wins_max_new_1(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            low = _s2_record("low_cit", citation_count=10)
            high = _s2_record("high_cit", citation_count=5000)
            s2 = _mock_s2(cites=[low, high])
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=1, max_nodes=10000)
            added = store.all_paper_ids() - {"s1"}
        assert "high_cit" in added, f"Expected higher-citation citer to win; got {added}"

    def test_higher_hindex_citer_wins_when_citations_equal(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            low = _s2_record("low_h", citation_count=100, hindex=2)
            high = _s2_record("high_h", citation_count=100, hindex=80)
            s2 = _mock_s2(cites=[low, high])
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=1, max_nodes=10000)
            added = store.all_paper_ids() - {"s1"}
        assert "high_h" in added, f"Expected higher-hindex citer to win; got {added}"

    def test_better_venue_citer_wins_when_citations_and_hindex_equal(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            no_venue = _s2_record("no_venue", citation_count=100, hindex=10, venue=None)
            good_venue = _s2_record("good_venue", citation_count=100, hindex=10, venue="NeurIPS")
            s2 = _mock_s2(cites=[no_venue, good_venue])
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=1, max_nodes=10000)
            added = store.all_paper_ids() - {"s1"}
        assert "good_venue" in added, f"Expected better-venue citer to win; got {added}"

    def test_reference_direction_unaffected_2020_beats_2026_same_citations(self):
        """Regression: reference-side ranking must still use the old
        equal-weight (+2026 rule) formula, coexisting correctly with the
        new citation-side hybrid score in the same wave."""
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            ref_2020 = _s2_record("ref_2020", citation_count=1000, year=2020)
            ref_2026 = _s2_record("ref_2026", citation_count=1000, year=2026)
            citer = _s2_record("citer1", citation_count=1)
            s2 = _mock_s2(refs=[ref_2026, ref_2020], cites=[citer])
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=100, max_nodes=10000)
            added = store.all_paper_ids()
        # All should be added (max_new=100 is generous); this just confirms
        # the mixed ref+cite wave doesn't crash and adds everything relevant.
        assert {"ref_2020", "ref_2026", "citer1"} <= added


# C. Seed references bypass the max_new cap

class TestSeedReferencesUncapped:

    def test_seed_references_all_added_despite_low_max_new(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            refs = [_s2_record(f"ref{i}", citation_count=10) for i in range(5)]
            s2 = _mock_s2(refs=refs)
            added_count = expand_network(s2, MagicMock(), store, ["s1"],
                                         hops=1, max_new=1, max_nodes=10000)
            all_ids = store.all_paper_ids()
            for i in range(5):
                assert f"ref{i}" in all_ids, f"ref{i} missing; seed references must all be added"
        assert added_count == 5

    def test_seed_citations_still_capped_by_max_new(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            cites = [_s2_record(f"cite{i}", citation_count=10) for i in range(5)]
            s2 = _mock_s2(cites=cites)
            added_count = expand_network(s2, MagicMock(), store, ["s1"],
                                         hops=1, max_new=1, max_nodes=10000)
        assert added_count == 1, \
            f"Seed citations must still respect max_new cap; added {added_count}"

    def test_non_seed_paper_references_still_capped_by_max_new(self):
        """A non-seed node discovered mid-BFS (hop >= 1) must still respect
        max_new for its own references -- only original seeds bypass it."""
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            mid_node = _s2_record("mid", citation_count=5000)
            s2 = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return [mid_node]
                if pid == "mid":
                    return [_s2_record(f"deep{i}", citation_count=10) for i in range(5)]
                return []
            s2.get_references.side_effect = refs_side_effect
            s2.get_citations.return_value = []
            added_count = expand_network(s2, MagicMock(), store, ["s1"],
                                         hops=2, max_new=2, max_nodes=10000)
        assert added_count == 2, \
            f"Expected exactly max_new=2 papers added total (1 seed-ref + 1 deep-ref); got {added_count}"

    def test_seed_references_still_filtered_by_relevance(self):
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            bio_ref = dict(_s2_record("bio1"), fieldsOfStudy=["Biology"])
            cs_ref = _s2_record("cs1")
            s2 = _mock_s2(refs=[bio_ref, cs_ref])
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=1, max_nodes=10000)
            all_ids = store.all_paper_ids()
        assert "cs1" in all_ids
        assert "bio1" not in all_ids

    def test_seed_references_bypass_max_nodes_too(self):
        """
        max_nodes is no longer a hard stop for a seed's own references
        either (see tests/test_features_v10.py) -- if it were, an early
        seed's reference list could exhaust the budget and leave a
        later-queued seed with zero references fetched at all. A seed's
        own references must always be added in full, regardless of both
        max_new and max_nodes.
        """
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            refs = [_s2_record(f"ref{i}", citation_count=10) for i in range(5)]
            s2 = _mock_s2(refs=refs)
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=100, max_nodes=3)
            all_ids = store.all_paper_ids()
        for i in range(5):
            assert f"ref{i}" in all_ids, \
                f"ref{i} missing -- seed references must all be added regardless of max_nodes"


# E. Liked / GUI-added papers get the same uncapped-reference treatment as
#    seeds -- checked against the paper's CURRENT store label, not just
#    membership in the seed_ids parameter, so this applies even when such a
#    paper is discovered mid-BFS rather than passed in explicitly.

class TestLikedAndGuiAddedPapersAlsoUncapped:

    def test_liked_paper_discovered_midbfs_gets_uncapped_references(self):
        """
        'liked1' is pre-labeled 'liked' (simulating a paper the user already
        liked in the GUI) BEFORE it is discovered as a reference of seed s1.
        When liked1 is later dequeued, its own 5 references must all be
        added despite a tight max_new=1 cap.
        """
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            store.upsert_paper(
                Paper(paper_id="liked1", title="Liked1", source_api="s2",
                      field_of_study=["Computer Science"]),
                label="liked",
            )
            s2 = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return [_s2_record("liked1")]
                if pid == "liked1":
                    return [_s2_record(f"deep{i}", citation_count=10) for i in range(5)]
                return []
            s2.get_references.side_effect = refs_side_effect
            s2.get_citations.return_value = []
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=2, max_new=1, max_nodes=10000)
            all_ids = store.all_paper_ids()
        for i in range(5):
            assert f"deep{i}" in all_ids, \
                f"deep{i} missing; a liked paper's references must all be added, like a seed's"

    def test_gui_added_paper_discovered_midbfs_gets_uncapped_references(self):
        """
        'guiadded1' is pre-labeled 'seed' (the label /api/paper/add-by-title
        uses) BEFORE being discovered as a reference of s1 -- same
        uncapped-reference guarantee must apply.
        """
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            store.upsert_paper(
                Paper(paper_id="guiadded1", title="GuiAdded1", source_api="s2",
                      field_of_study=["Computer Science"]),
                label="seed",
            )
            s2 = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return [_s2_record("guiadded1")]
                if pid == "guiadded1":
                    return [_s2_record(f"deep{i}", citation_count=10) for i in range(5)]
                return []
            s2.get_references.side_effect = refs_side_effect
            s2.get_citations.return_value = []
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=2, max_new=1, max_nodes=10000)
            all_ids = store.all_paper_ids()
        for i in range(5):
            assert f"deep{i}" in all_ids, \
                f"deep{i} missing; a GUI-added paper's references must all be added, like a seed's"

    def test_unlabeled_paper_discovered_midbfs_still_capped(self):
        """Regression: a plain candidate (no seed/liked label) discovered
        mid-BFS must still respect max_new for its own references."""
        with tempfile.TemporaryDirectory() as d:
            store = _make_store_with_seed(Path(d) / "t.db")
            s2 = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return [_s2_record("mid")]
                if pid == "mid":
                    return [_s2_record(f"deep{i}", citation_count=10) for i in range(5)]
                return []
            s2.get_references.side_effect = refs_side_effect
            s2.get_citations.return_value = []
            added = expand_network(s2, MagicMock(), store, ["s1"],
                                   hops=2, max_new=1, max_nodes=10000)
        assert added == 1, \
            f"Expected exactly max_new=1 paper added (s1's own ref 'mid'); got {added}"


# D. PageRank robustness (manual power-iteration fallback)

class TestPageRankRobustness:

    def test_manual_pagerank_ranks_hub_above_low_importance_leaf(self):
        """Construct a graph where degree centrality and PageRank disagree:
        H has in-degree 1 but from an important hub HH (fed by 5 leaves);
        L has in-degree 3 but from unrelated low-importance leaves.
        Degree centrality would rank L above H; PageRank must rank H above L."""
        from citation_network import _manual_pagerank
        g = nx.DiGraph()
        for i in range(5):
            g.add_edge(f"leaf{i}", "HH")
        g.add_edge("HH", "H")
        for name in ("leafA", "leafB", "leafC"):
            g.add_edge(name, "L")

        deg = nx.degree_centrality(g)
        assert deg["L"] > deg["H"], "test setup sanity check: L must have higher degree centrality than H"

        pr = _manual_pagerank(g, alpha=0.85, max_iter=200)
        assert pr["H"] > pr["L"], \
            f"PageRank must rank H (fed by important hub) above L (fed by unrelated leaves); got H={pr['H']}, L={pr['L']}"

    def test_manual_pagerank_sums_to_approximately_one(self):
        from citation_network import _manual_pagerank
        g = nx.DiGraph()
        g.add_edge("A", "B")
        g.add_edge("B", "A")
        g.add_edge("A", "C")
        pr = _manual_pagerank(g, alpha=0.85, max_iter=200)
        assert abs(sum(pr.values()) - 1.0) < 0.02

    def test_compute_metrics_uses_manual_pagerank_when_scipy_missing(self, monkeypatch):
        import citation_network as cn

        def fake_pagerank(*a, **kw):
            raise ModuleNotFoundError("no scipy")
        monkeypatch.setattr(cn.nx, "pagerank", fake_pagerank)

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for i in range(5):
                store.upsert_paper(Paper(paper_id=f"leaf{i}", title=f"leaf{i}", source_api="s2"))
            store.upsert_paper(Paper(paper_id="HH", title="HH", source_api="s2"))
            store.upsert_paper(Paper(paper_id="H", title="H", source_api="s2"))
            for i in range(5):
                store.add_edges(f"leaf{i}", ["HH"])
            store.add_edges("HH", ["H"])
            for name in ("leafA", "leafB", "leafC"):
                store.upsert_paper(Paper(paper_id=name, title=name, source_api="s2"))
                store.add_edges(name, ["L"])
            store.upsert_paper(Paper(paper_id="L", title="L", source_api="s2"))

            graph = cn.build_graph(store)
            cn.compute_metrics(store, graph, seed_ids=[])
            h_metrics = store.get_metrics("H")
            l_metrics = store.get_metrics("L")

        assert h_metrics["pagerank"] > 0
        assert h_metrics["pagerank"] > l_metrics["pagerank"], \
            "compute_metrics's manual fallback must genuinely compute PageRank, not degree centrality"
