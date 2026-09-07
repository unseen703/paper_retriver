"""
tests/test_features_v4.py
-------------------------
TDD tests for v4 features:
  1. Graph layout spacing — larger repulsion/link-distance; labels only for good-score nodes
  2. Updated scoring — equal 1/3 weights for all three; 2026 papers get 20% less citation weight

Importers/callers: pytest only.
Affected APIs: graph_viz._node_score, /api/graph, graph_expander.expand_network (BFS _priority),
               GET / -> templates/graph.html (D3 force params + label logic).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import Paper, Store


# ── helpers ──────────────────────────────────────────────────────────────────

def _cs_ref(pid, citation_count=10, year=2020, venue=None):
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": year, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": venue, "tldr": None, "embedding": None,
    }

def _make_s2_client(ref_list=None, cite_list=None):
    client = MagicMock()
    client.get_references.return_value = ref_list or []
    client.get_citations.return_value = cite_list or []
    return client


# ── Feature 2a: Equal 1/3 scoring weights ────────────────────────────────────

class TestEqualScoringWeights:
    """
    New formula (papers before 2026):
        score = (cit_norm + venue_score + conn_norm) / 3

    2026 papers (very recent, citation count not yet mature):
        cit_weight  = (1/3) * 0.8  = 0.2667
        other_weight = (1 - cit_weight) / 2 = 0.3667
        score = 0.2667*cit + 0.3667*venue + 0.3667*conn
    """

    def _score(self, paper, n_seeds=1):
        import graph_viz
        return graph_viz._node_score(paper, n_seeds)

    def test_full_citation_equals_full_venue_for_2020_paper(self):
        """
        With equal 1/3 weights, max citations alone and max venue alone should
        produce approximately identical scores (all else zero).
        10000 citations -> cit_norm ≈ 1.0; NeurIPS venue_score = 1.0.
        Old formula: sa ≈ 0.5, sb = 0.25  (NOT equal — 2:1 bias).
        New formula: sa ≈ sb ≈ 0.333.
        """
        paper_a = Paper(paper_id="a", title="A", source_api="s2",
                        citation_count=10000, venue=None, year=2020)
        paper_b = Paper(paper_id="b", title="B", source_api="s2",
                        citation_count=0, venue="NeurIPS", year=2020)
        diff = abs(self._score(paper_a) - self._score(paper_b))
        assert diff < 0.10, (
            f"Citation-only score ({self._score(paper_a):.3f}) and venue-only score "
            f"({self._score(paper_b):.3f}) differ by {diff:.3f} — "
            "should be ≈equal with 1/3 weights (old 2:1 bias must be removed)"
        )

    def test_old_citation_bias_removed(self):
        """
        Old formula had citation weight 2× venue weight (0.5 vs 0.25).
        New formula is equal. The ratio of citation-only score to venue-only score
        must now be closer to 1.0 (not ~2.0).
        """
        p_cit = Paper(paper_id="c", title="C", source_api="s2",
                      citation_count=10000, venue=None, year=2020)
        p_ven = Paper(paper_id="v", title="V", source_api="s2",
                      citation_count=0, venue="NeurIPS", year=2020)
        ratio = self._score(p_cit) / max(self._score(p_ven), 1e-6)
        assert ratio < 1.4, (
            f"Citation-to-venue score ratio is {ratio:.2f}; "
            "old 2:1 bias not removed — expected ratio ≈ 1.0 with equal weights"
        )

    def test_2026_paper_scores_lower_when_high_citations(self):
        """
        A 2026 paper with max citations should score slightly lower than the same
        2020 paper because citation weight is reduced from 1/3 to 0.267 for 2026.
        """
        base = dict(paper_id="x", title="X", source_api="s2",
                    citation_count=10000, venue="NeurIPS")
        s2020 = self._score(Paper(**base, year=2020))
        s2026 = self._score(Paper(**base, year=2026))
        assert s2026 < s2020, (
            f"2026 paper score {s2026:.3f} should be < 2020 paper score {s2020:.3f} "
            "when citations are high (citation weight is reduced for very recent papers)"
        )

    def test_2026_paper_venue_weight_is_higher(self):
        """
        When citations = 0, the weight redistributed away from citations goes to venue.
        A 2026 NeurIPS paper with zero citations should score HIGHER than the 2020 equivalent.
        2020: 0/3 + 1.0/3 = 0.333
        2026: 0.267*0 + 0.367*1.0 = 0.367
        """
        base = dict(paper_id="y", title="Y", source_api="s2",
                    citation_count=0, venue="NeurIPS")
        s2020 = self._score(Paper(**base, year=2020))
        s2026 = self._score(Paper(**base, year=2026))
        assert s2026 > s2020, (
            f"2026 paper score {s2026:.3f} should be > 2020 paper score {s2020:.3f} "
            "when citations=0 (redistributed weight raises venue contribution for 2026)"
        )

    def test_2026_citation_weight_approximately_correct(self):
        """
        With only citations contributing (venue=None, conn=0, year=2026, 10000 cit):
        score ≈ 0.267 * cit_norm ≈ 0.267.
        """
        p = Paper(paper_id="z", title="Z", source_api="s2",
                  citation_count=10000, venue=None, year=2026)
        s = self._score(p)
        assert 0.20 < s < 0.32, (
            f"2026 citation-only score {s:.3f} not in [0.20, 0.32]; "
            "expected ≈0.267 × cit_norm for the reduced citation weight"
        )

    def test_scores_via_api_reflect_equal_weights(self):
        """
        Via /api/graph: a 2020 paper with max citations and no venue should score
        approximately the same as a 2020 paper with no citations and NeurIPS venue.
        """
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="hc", title="HC", source_api="s2",
                                    citation_count=10000, venue=None, year=2020))
            store.upsert_paper(Paper(paper_id="hv", title="HV", source_api="s2",
                                    citation_count=0, venue="NeurIPS", year=2020))
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                nodes = {n["id"]: n["score"]
                         for n in client.get("/api/graph").get_json()["nodes"]}
        diff = abs(nodes["hc"] - nodes["hv"])
        assert diff < 0.10, (
            f"API score: max-citation paper={nodes['hc']:.3f}, "
            f"max-venue paper={nodes['hv']:.3f}, diff={diff:.3f}; "
            "should be ≈equal with 1/3 weights"
        )


# ── Feature 2b: BFS priority uses updated formula ────────────────────────────

from graph_expander import expand_network


class TestBfsPriorityUpdatedFormula:
    """
    The BFS _priority closure inside expand_network must use the same
    equal-weight formula and 2026 citation reduction as _node_score.
    """

    def test_2020_paper_preferred_over_2026_with_same_citations(self):
        """
        Two refs with identical citation counts (normalized to 1.0 each, both same venue).
        ref_2020: year=2020 → priority ≈ (1.0 + 0.3 + 0) / 3 = 0.433
        ref_2026: year=2026 → priority ≈ 0.267*1.0 + 0.367*0.3 = 0.377
        With max_new=1, the 2020 paper must win.
        ref_2026 is placed first in the list to confirm the sort is by priority.
        """
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(
                Paper(paper_id="s1", title="Seed", source_api="s2",
                      field_of_study=["Computer Science"]),
                label="seed",
            )
            ref_2020 = _cs_ref("ref_2020", citation_count=1000, year=2020, venue=None)
            ref_2026 = _cs_ref("ref_2026", citation_count=1000, year=2026, venue=None)
            s2 = _make_s2_client(ref_list=[ref_2026, ref_2020])  # 2026 listed first
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=1, max_nodes=10000)
            added = store.all_paper_ids() - {"s1"}
        assert "ref_2020" in added, (
            f"Expected ref_2020 (higher BFS priority) to be added; got {added} — "
            "BFS _priority must apply the 2026 citation-reduced formula"
        )

    def test_equal_priority_refs_both_added_with_max_new_2(self):
        """
        Two 2020 refs with identical citation counts and no venue should
        produce equal priority — both get added when max_new=2.
        """
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(
                Paper(paper_id="s1", title="Seed", source_api="s2",
                      field_of_study=["Computer Science"]),
                label="seed",
            )
            refs = [
                _cs_ref("ra", citation_count=500, year=2020, venue=None),
                _cs_ref("rb", citation_count=500, year=2020, venue=None),
            ]
            s2 = _make_s2_client(ref_list=refs)
            expand_network(s2, MagicMock(), store, ["s1"],
                           hops=1, max_new=2, max_nodes=10000)
            added = store.all_paper_ids() - {"s1"}
        assert {"ra", "rb"} == added, \
            f"Both equally-scored refs should be added; got {added}"


# ── Feature 1: Graph layout spacing ──────────────────────────────────────────

class TestGraphLayoutSpacing:
    """
    D3 physics parameters must be updated so hub clusters spread apart.
    Labels must be conditional on node score, not always shown.
    """

    def _get_html(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        with graph_viz.app.test_client() as client:
            return client.get("/").data.decode()

    def test_charge_strength_is_more_repulsive(self):
        """
        forceManyBody().strength() must be <= -300 (more repulsive than old -200)
        so hub clusters spread apart and nodes do not overlap.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
        m = re.search(r'forceManyBody\(\)\.strength\(\s*-(\d+(?:\.\d+)?)\s*\)', html)
        assert m is not None, "forceManyBody().strength(<negative>) not found in graph.html"
        val = float(m.group(1))   # absolute value of the (negative) strength
        assert val >= 300, (
            f"forceManyBody strength is -{val}; "
            "must be >= 300 (i.e. strength <= -300) for better inter-hub spacing (old was -200)"
        )

    def test_link_distance_is_larger(self):
        """
        forceLink.distance() must be >= 120 (up from old 80) so connected nodes
        have breathing room between hubs.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
        m = re.search(r'\.distance\(\s*(\d+)\s*\)', html)
        assert m is not None, ".distance() not found in graph.html"
        val = int(m.group(1))
        assert val >= 120, (
            f"forceLink distance={val}; "
            "must be >= 120 for better hub spacing (old value was 80)"
        )

    def test_labels_conditional_on_score_threshold(self):
        """
        Node labels must only be shown when a node's score exceeds a threshold.
        HTML must contain a LABEL_SCORE_THRESHOLD constant.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
        assert "LABEL_SCORE_THRESHOLD" in html, (
            "LABEL_SCORE_THRESHOLD constant not found in graph.html — "
            "label visibility must be conditional on node score, not always shown"
        )

    def test_labeled_nodes_always_show_labels_regardless_of_score(self):
        """
        Even low-scoring labeled nodes (seed/liked/disliked/skipped) must always
        show their labels. The LABELED set and LABEL_SCORE_THRESHOLD must both
        appear in the label display logic.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
        assert "LABELED" in html and "LABEL_SCORE_THRESHOLD" in html, (
            "Both LABELED set and LABEL_SCORE_THRESHOLD must be in label display logic "
            "so seed/liked nodes always have visible labels regardless of score"
        )
