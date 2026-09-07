"""
tests/test_features_v3.py
-------------------------
TDD tests for v3 features:
  1. Search papers by title (in-graph search endpoint + UI)
  2. Neighbor sidebar — list a node's direct neighbors on click
  3. Physics slowdown — velocityDecay high enough to prevent drag thrash
  4. Fetch all refs sorted by citation count so high-impact papers are found
"""

from __future__ import annotations

import inspect
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from storage import Paper, Store


# ── helpers reused from v2 ───────────────────────────────────────────────────

def _cs_ref(pid, citation_count=10):
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": 2020, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": None, "tldr": None, "embedding": None,
    }


def _make_s2_client(ref_list=None, cite_list=None):
    client = MagicMock()
    client.get_references.return_value = ref_list or []
    client.get_citations.return_value = cite_list or []
    return client


# ── Feature 1: GET /api/paper/search ────────────────────────────────────────

class TestSearchPapersApi:
    def _client(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    def test_search_returns_matching_paper(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Attention Is All You Need", source_api="s2"))
            store.upsert_paper(Paper(paper_id="p2", title="BERT: Bidirectional Transformers", source_api="s2"))
            with self._client(db) as client:
                resp = client.get("/api/paper/search?q=attention")
            assert resp.status_code == 200
            data = resp.get_json()
            ids = [p["paper_id"] for p in data["papers"]]
            assert "p1" in ids
            assert "p2" not in ids

    def test_search_empty_query_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Some Paper", source_api="s2"))
            with self._client(db) as client:
                resp = client.get("/api/paper/search?q=")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["papers"] == []

    def test_search_no_match_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Attention", source_api="s2"))
            with self._client(db) as client:
                resp = client.get("/api/paper/search?q=xyz_zzz_nonexistent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["papers"] == []

    def test_search_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Attention Is All You Need", source_api="s2"))
            with self._client(db) as client:
                resp = client.get("/api/paper/search?q=ATTENTION")
            data = resp.get_json()
            ids = [p["paper_id"] for p in data["papers"]]
            assert "p1" in ids

    def test_search_response_includes_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Attention Is All You Need",
                                    source_api="s2", year=2017, citation_count=50000))
            with self._client(db) as client:
                resp = client.get("/api/paper/search?q=attention")
            data = resp.get_json()
            p = data["papers"][0]
            for field in ("paper_id", "title", "year", "citation_count"):
                assert field in p, f"Field '{field}' missing from search result"

    def test_search_html_contains_search_input(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                html = client.get("/").data.decode()
        assert "graph-search-input" in html, \
            "#graph-search-input element not found in graph.html"

    def test_search_html_contains_search_function(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                html = client.get("/").data.decode()
        assert "searchGraph" in html or "doSearch" in html, \
            "Search JS function (searchGraph/doSearch) not found in graph.html"


# ── Feature 2: GET /api/paper/<id>/neighbors ─────────────────────────────────

class TestNeighborSidebarApi:
    def _client(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    def test_neighbors_unknown_paper_returns_404(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with self._client(db) as client:
                resp = client.get("/api/paper/ghost_xyz/neighbors")
            assert resp.status_code == 404

    def test_neighbors_isolated_paper_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Isolated", source_api="s2"))
            with self._client(db) as client:
                resp = client.get("/api/paper/p1/neighbors")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["neighbors"] == []

    def test_neighbors_returns_outgoing_refs_with_direction_out(self):
        """p1 cites p2 → p2 appears with direction='out' in p1's neighbors."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Paper 1", source_api="s2"))
            store.upsert_paper(Paper(paper_id="p2", title="Paper 2", source_api="s2"))
            store.add_edges("p1", ["p2"])
            with self._client(db) as client:
                resp = client.get("/api/paper/p1/neighbors")
            data = resp.get_json()
            neighbor_ids = [n["paper_id"] for n in data["neighbors"]]
            assert "p2" in neighbor_ids
            p2_entry = next(n for n in data["neighbors"] if n["paper_id"] == "p2")
            assert p2_entry["direction"] == "out"

    def test_neighbors_returns_incoming_cites_with_direction_in(self):
        """p3 cites p1 → p3 appears with direction='in' in p1's neighbors."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Paper 1", source_api="s2"))
            store.upsert_paper(Paper(paper_id="p3", title="Paper 3", source_api="s2"))
            store.add_edges("p3", ["p1"])
            with self._client(db) as client:
                resp = client.get("/api/paper/p1/neighbors")
            data = resp.get_json()
            neighbor_ids = [n["paper_id"] for n in data["neighbors"]]
            assert "p3" in neighbor_ids
            p3_entry = next(n for n in data["neighbors"] if n["paper_id"] == "p3")
            assert p3_entry["direction"] == "in"

    def test_neighbor_response_includes_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="Paper 1", source_api="s2"))
            store.upsert_paper(Paper(paper_id="p2", title="Paper 2", source_api="s2",
                                    year=2022, citation_count=500))
            store.add_edges("p1", ["p2"])
            with self._client(db) as client:
                resp = client.get("/api/paper/p1/neighbors")
            data = resp.get_json()
            n = data["neighbors"][0]
            for fld in ("paper_id", "title", "year", "citation_count", "direction"):
                assert fld in n, f"Field '{fld}' missing from neighbor entry"

    def test_neighbor_sidebar_html_element_present(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                html = client.get("/").data.decode()
        assert "neighbor-sidebar" in html, \
            "#neighbor-sidebar element not found in graph.html"

    def test_neighbor_sidebar_load_function_present(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                html = client.get("/").data.decode()
        assert "loadNeighborSidebar" in html or "showNeighborSidebar" in html, \
            "Neighbor sidebar JS function not found in graph.html"


# ── Feature 3: Physics damping (structural) ──────────────────────────────────

class TestPhysicsSettings:
    def _get_html(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        with graph_viz.app.test_client() as client:
            return client.get("/").data.decode()

    def test_velocity_decay_is_high_enough(self):
        """
        velocityDecay >= 0.6 → high damping → nodes settle fast without oscillation
        when dragged. The old default (0.3-0.4) caused the network to thrash.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
        m = re.search(r'velocityDecay\s*\(\s*([\d.]+)\s*\)', html)
        assert m is not None, \
            "velocityDecay() call not found in graph.html simulation setup"
        val = float(m.group(1))
        assert val >= 0.6, \
            f"velocityDecay={val} is too low — graph thrashes when dragging; set >= 0.6"


# ── Feature 4: Sorted references by citation count ───────────────────────────

from graph_expander import expand_network


class TestSortedReferencesByScore:

    def test_per_paper_limit_default_is_at_least_500(self):
        """expand_network default per_paper_limit must be >= 500 (was 50)."""
        sig = inspect.signature(expand_network)
        default = sig.parameters["per_paper_limit"].default
        assert default >= 500, \
            f"per_paper_limit default={default}, expected >= 500 so full reference lists are fetched"

    def test_get_references_called_with_limit_at_least_500(self):
        """S2 client get_references must be called with limit >= 500."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            s2 = MagicMock()
            s2.get_references.return_value = []
            s2.get_citations.return_value = []
            expand_network(s2, MagicMock(), store, ["seed1"], hops=1)

            for call in s2.get_references.call_args_list:
                args, kwargs = call
                limit_arg = args[1] if len(args) > 1 else kwargs.get("limit", 0)
                assert limit_arg >= 500, \
                    f"get_references called with limit={limit_arg}, expected >= 500"

    def test_highest_citation_ref_added_when_max_new_is_1(self):
        """
        With max_new=1, the single paper added must be the highest-citation one,
        regardless of the order S2 returned refs (tests sorting before processing).
        """
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            low_ref = _cs_ref("low_cit_paper", citation_count=10)
            high_ref = _cs_ref("high_cit_paper", citation_count=50000)
            # S2 returns low first — but after sorting, high should be processed first
            s2 = _make_s2_client(ref_list=[low_ref, high_ref])

            expand_network(s2, MagicMock(), store, ["seed1"],
                           hops=1, max_new=1, max_nodes=10000)

            all_ids = store.all_paper_ids()
            assert "high_cit_paper" in all_ids, \
                ("high_cit_paper not added — refs are not sorted by citation_count DESC; "
                 "low_cit_paper was added first because it was returned first by S2")

    def test_top_3_by_citation_added_when_max_new_3(self):
        """
        10 CITATIONS with known citation counts; with max_new=3 the 3 added
        papers must be the 3 highest-citation ones (the hybrid score is
        dominated by citation count when venue/hindex/indegree are equal).
        Uses citations, not references -- a seed's own references are now
        always added in full regardless of max_new (see
        tests/test_citation_ranking.py::TestSeedReferencesUncapped).
        """
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            counts = [5, 200, 80, 1000, 15, 400, 3, 750, 60, 25]
            cites = [_cs_ref(f"ref_{i}", citation_count=c) for i, c in enumerate(counts)]
            # top 3: ref_3 (1000), ref_7 (750), ref_5 (400)
            top3_pids = {"ref_3", "ref_7", "ref_5"}
            s2 = _make_s2_client(cite_list=cites)

            expand_network(s2, MagicMock(), store, ["seed1"],
                           hops=1, max_new=3, max_nodes=10000)

            added = store.all_paper_ids() - {"seed1"}
            assert added == top3_pids, \
                (f"Expected top-3 by citation count {top3_pids} but got {added} — "
                 "citations not ranked by hybrid score (citation count dominant) before processing")
