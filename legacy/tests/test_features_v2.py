"""
tests/test_features_v2.py
-------------------------
TDD tests for v2 features + confirmed bug fixes from code review.

Covers:
  - venue_config.venue_score()
  - field_filter.is_relevant_paper()
  - storage.upsert_paper() returns actual_id (Bug #1)
  - storage: liked/disliked label preserved when DOI dedup fires (Bug #3)
  - storage: source_api updates when better API ingests same paper (Bug #6)
  - citation_network: co_cit uses in_deg variable, not duplicate sum (Bug #7)
  - semantic_scholar_client: fallback /search exception returns None (Bug #4)
  - semantic_scholar_client: empty /match result not cached (Bug #5)
  - graph_expander: add_edges uses actual_id from upsert_paper (Bug #1)
  - graph_viz: Flask /api/graph returns valid JSON (Feature 1)
  - score_candidates: high-citation paper ranks above low-citation (Feature 2)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------
# venue_config
# ------------------------------------------------------------------

from venue_config import venue_score


class TestVenueScore:
    def test_neurips_is_tier1(self):
        assert venue_score("NeurIPS 2023") == 1.0

    def test_iclr_is_tier1(self):
        assert venue_score("ICLR 2024") == 1.0

    def test_acl_is_tier1(self):
        assert venue_score("ACL 2022") == 1.0

    def test_aistats_is_tier2(self):
        assert venue_score("AISTATS 2023") == 0.7

    def test_unknown_venue_is_default(self):
        score = venue_score("Some random workshop")
        assert 0.0 < score < 1.0

    def test_none_venue_returns_zero(self):
        assert venue_score(None) == 0.0

    def test_empty_venue_returns_zero(self):
        assert venue_score("") == 0.0

    def test_case_insensitive(self):
        assert venue_score("advances in neural information processing systems") == 1.0


# ------------------------------------------------------------------
# field_filter
# ------------------------------------------------------------------

from field_filter import is_relevant_paper


class TestIsRelevantPaper:
    def test_cs_paper_kept(self):
        assert is_relevant_paper(["Computer Science"]) is True

    def test_math_paper_kept(self):
        assert is_relevant_paper(["Mathematics"]) is True

    def test_biology_only_excluded(self):
        assert is_relevant_paper(["Biology"]) is False

    def test_medicine_only_excluded(self):
        assert is_relevant_paper(["Medicine"]) is False

    def test_economics_only_excluded(self):
        assert is_relevant_paper(["Economics"]) is False

    def test_cs_plus_biology_kept(self):
        assert is_relevant_paper(["Computer Science", "Biology"]) is True

    def test_empty_fields_kept(self):
        assert is_relevant_paper([]) is True

    def test_none_fields_kept(self):
        assert is_relevant_paper(None) is True

    def test_engineering_excluded(self):
        assert is_relevant_paper(["Engineering"]) is False


# ------------------------------------------------------------------
# storage: upsert_paper returns actual_id
# ------------------------------------------------------------------

from storage import Paper, Store


def _make_paper(pid: str, doi: str | None = None, label: str | None = None,
                citation_count: int = 0) -> Paper:
    return Paper(
        paper_id=pid,
        title=f"Title {pid}",
        doi=doi,
        label=label,
        citation_count=citation_count,
        source_api="semantic_scholar",
    )


class TestUpsertPaperReturnsActualId:
    def test_returns_own_id_when_no_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            p = _make_paper("abc123")
            result = store.upsert_paper(p)
            assert result == "abc123"

    def test_returns_existing_id_on_doi_collision(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            original = _make_paper("s2_abc", doi="10.1/x")
            store.upsert_paper(original)

            duplicate = _make_paper("oa_w999", doi="10.1/x")
            result = store.upsert_paper(duplicate)
            assert result == "s2_abc"


class TestLikedLabelPreservedOnDOIDedup:
    def test_liked_not_downgraded_to_seed(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            p = _make_paper("s2_abc", doi="10.1/x")
            store.upsert_paper(p)
            store.set_label("s2_abc", "liked")

            dup = _make_paper("oa_w999", doi="10.1/x")
            store.upsert_paper(dup, label="seed")

            kept = store.get_paper("s2_abc")
            assert kept.label == "liked", f"Expected 'liked', got {kept.label!r}"

    def test_disliked_not_overwritten_by_seed(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            p = _make_paper("s2_abc", doi="10.1/x")
            store.upsert_paper(p)
            store.set_label("s2_abc", "disliked")

            dup = _make_paper("oa_w999", doi="10.1/x")
            store.upsert_paper(dup, label="seed")

            kept = store.get_paper("s2_abc")
            assert kept.label == "disliked"


class TestSourceApiUpdated:
    def test_source_api_updates_when_better_api_ingests(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            oa_paper = Paper(paper_id="pid1", title="Test", source_api="openalex")
            store.upsert_paper(oa_paper)

            s2_paper = Paper(paper_id="pid1", title="Test", source_api="semantic_scholar")
            store.upsert_paper(s2_paper)

            stored = store.get_paper("pid1")
            assert stored.source_api == "semantic_scholar"


# ------------------------------------------------------------------
# citation_network: co_cit not a duplicate of in_deg
# ------------------------------------------------------------------

import networkx as nx
from citation_network import compute_metrics


class TestCoCitationCorrect:
    def test_co_cit_includes_candidate_to_seed_direction(self):
        """candidate cites seed → co_cit=1, in_degree=0"""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2")
            store.upsert_paper(seed, label="seed")
            cand = Paper(paper_id="cand1", title="Candidate", source_api="s2")
            store.upsert_paper(cand)

            g = nx.DiGraph()
            g.add_node("seed1")
            g.add_node("cand1")
            g.add_edge("cand1", "seed1")  # candidate → seed

            compute_metrics(store, g, ["seed1"])

            with store._conn() as conn:
                row = conn.execute(
                    "SELECT in_degree, co_citation FROM network_metrics WHERE paper_id='cand1'"
                ).fetchone()
            assert row is not None
            assert row["in_degree"] == 0
            assert row["co_citation"] == 1


# ------------------------------------------------------------------
# semantic_scholar_client: fallback exception handled
# ------------------------------------------------------------------

from semantic_scholar_client import SemanticScholarClient


class TestSearchFallbackExceptionReturnsNone:
    def test_fallback_exception_returns_none_not_raises(self):
        cache = MagicMock()
        cache.get.return_value = None
        client = SemanticScholarClient(api_key=None, cache=cache)

        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": []},           # /match: no candidates → fall through
                Exception("503 error"), # /search: network error
            ]
            result = client.search_paper_by_title("some title")
            assert result is None


class TestEmptyMatchNotCached:
    def test_empty_match_does_not_call_cache_set_with_empty_data(self):
        cache = MagicMock()
        cache.get.return_value = None
        client = SemanticScholarClient(api_key=None, cache=cache)

        with patch.object(client, "_request", return_value={"data": []}):
            client.search_paper_by_title("obscure title")
        # cache.set must never be called with {"data": []}
        for call in cache.set.call_args_list:
            stored_data = call[0][1] if len(call[0]) >= 2 else call[1].get("value")
            if isinstance(stored_data, dict):
                assert stored_data.get("data") != [], \
                    "Empty /match response must not be cached"


# ------------------------------------------------------------------
# graph_expander: edges reference real paper rows (no orphans)
# ------------------------------------------------------------------

from graph_expander import expand_network


class TestExpandNetworkNoOrphanEdges:
    def test_all_edge_endpoints_have_paper_rows(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            s2_client = MagicMock()
            s2_client.get_references.return_value = [
                {"paperId": "ref1", "title": "Ref1", "citationCount": 50,
                 "externalIds": {"DOI": "10.1/r1"}, "year": 2020,
                 "authors": [], "fieldsOfStudy": ["Computer Science"],
                 "venue": None, "tldr": None, "embedding": None},
            ]
            s2_client.get_citations.return_value = []
            oa_client = MagicMock()

            expand_network(s2_client, oa_client, store, ["seed1"], hops=1, max_nodes=100)

            all_pids = store.all_paper_ids()
            for citing, cited in store.all_edges():
                assert citing in all_pids, f"orphan citing_id: {citing!r}"
                assert cited in all_pids, f"orphan cited_id: {cited!r}"


# ------------------------------------------------------------------
# graph_viz: Flask API (Feature 1)
# ------------------------------------------------------------------

class TestGraphVizApi:
    def test_api_graph_returns_nodes_and_links(self):
        try:
            import graph_viz
        except ImportError:
            pytest.skip("graph_viz not yet implemented")

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            store = Store(db)
            p = Paper(paper_id="p1", title="Test Paper", source_api="s2", citation_count=10)
            store.upsert_paper(p)

            graph_viz.DB_PATH = db
            app = graph_viz.app
            app.config["TESTING"] = True
            with app.test_client() as client:
                resp = client.get("/api/graph")
                assert resp.status_code == 200
                data = resp.get_json()
                assert "nodes" in data
                assert "links" in data

    def test_api_paper_detail(self):
        try:
            import graph_viz
        except ImportError:
            pytest.skip("graph_viz not yet implemented")

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            store = Store(db)
            p = Paper(paper_id="xyz", title="Detail", source_api="s2",
                      abstract="Abstract text", citation_count=99)
            store.upsert_paper(p)

            graph_viz.DB_PATH = db
            app = graph_viz.app
            app.config["TESTING"] = True
            with app.test_client() as client:
                resp = client.get("/api/paper/xyz")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data["title"] == "Detail"
                assert data["citation_count"] == 99

    def test_api_graph_links_never_reference_missing_nodes(self):
        """Every link source/target must be present in nodes — even with orphan edges in DB."""
        try:
            import graph_viz
        except ImportError:
            pytest.skip("graph_viz not yet implemented")

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            store = Store(db)
            # Real paper
            store.upsert_paper(Paper(paper_id="real1", title="Real", source_api="s2"))
            # Orphan edge: "ghost_oa_id" is in edges table but has no paper row
            store.add_edges("ghost_oa_id", ["real1"])

            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                resp = client.get("/api/graph")
                assert resp.status_code == 200
                data = resp.get_json()
                node_ids = {n["id"] for n in data["nodes"]}
                for lnk in data["links"]:
                    assert lnk["source"] in node_ids, f"link source {lnk['source']!r} not in nodes"
                    assert lnk["target"] in node_ids, f"link target {lnk['target']!r} not in nodes"


# ------------------------------------------------------------------
# score_candidates: high-citation ranks above low-citation (Feature 2)
# ------------------------------------------------------------------

from citation_network import score_candidates


class TestCandidatesCitationOrdering:
    def test_higher_citation_paper_ranks_above_lower(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "test.db")
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         embedding=[1.0, 0.0])
            store.upsert_paper(seed, label="seed")

            low = Paper(paper_id="low1", title="Low", source_api="s2",
                        citation_count=5, year=2024)
            store.upsert_paper(low)
            high = Paper(paper_id="high1", title="High", source_api="s2",
                         citation_count=5000, year=2017)
            store.upsert_paper(high)

            g = nx.DiGraph()
            for pid in ["seed1", "low1", "high1"]:
                g.add_node(pid)
            g.add_edge("seed1", "high1")
            g.add_edge("seed1", "low1")

            ranked = score_candidates(store, g, [seed], [])
            ids = [sc.paper.paper_id for sc in ranked]
            if "high1" in ids and "low1" in ids:
                hi_pos = ids.index("high1")
                lo_pos = ids.index("low1")
                assert hi_pos < lo_pos, \
                    f"high1 (pos {hi_pos}) should rank above low1 (pos {lo_pos})"


# ------------------------------------------------------------------
# graph_viz: like/dislike API  (Feature GUI-1)
# ------------------------------------------------------------------

class TestLabelApi:
    def _make_app(self, store, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    def test_post_label_liked_updates_db(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="T", source_api="s2"))
            client = self._make_app(store, db)
            resp = client.post("/api/paper/p1/label",
                               json={"label": "liked"},
                               content_type="application/json")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["label"] == "liked"
            # Verify persisted
            store2 = Store(db)
            assert store2.get_paper("p1").label == "liked"

    def test_post_label_disliked_updates_db(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p2", title="T", source_api="s2"))
            client = self._make_app(store, db)
            resp = client.post("/api/paper/p2/label",
                               json={"label": "disliked"},
                               content_type="application/json")
            assert resp.status_code == 200
            assert Store(db).get_paper("p2").label == "disliked"

    def test_post_label_null_clears_label(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p3", title="T", source_api="s2"),
                               label="liked")
            client = self._make_app(store, db)
            resp = client.post("/api/paper/p3/label",
                               json={"label": None},
                               content_type="application/json")
            assert resp.status_code == 200
            assert Store(db).get_paper("p3").label is None

    def test_post_label_nonexistent_returns_404(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)  # init schema
            client = self._make_app(Store(db), db)
            resp = client.post("/api/paper/ghost/label",
                               json={"label": "liked"},
                               content_type="application/json")
            assert resp.status_code == 404

    def test_post_label_invalid_value_returns_400(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p4", title="T", source_api="s2"))
            client = self._make_app(store, db)
            resp = client.post("/api/paper/p4/label",
                               json={"label": "random_invalid"},
                               content_type="application/json")
            assert resp.status_code == 400


# ------------------------------------------------------------------
# graph_viz: node score field returned by /api/graph  (Feature GUI-2)
# ------------------------------------------------------------------

class TestNodeScoreInGraphApi:
    def test_each_node_has_score_field(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="s1", title="Seed",
                                    source_api="s2", citation_count=500,
                                    venue="NeurIPS"))
            store.upsert_paper(Paper(paper_id="c1", title="Cand",
                                    source_api="s2", citation_count=10))
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                resp = client.get("/api/graph")
                data = resp.get_json()
                for node in data["nodes"]:
                    assert "score" in node, f"node {node['id']} missing 'score'"
                    assert 0.0 <= node["score"] <= 1.0, \
                        f"score {node['score']} out of [0, 1]"

    def test_high_citation_node_scores_higher_than_low(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="hi", title="Hi",
                                    source_api="s2", citation_count=10000))
            store.upsert_paper(Paper(paper_id="lo", title="Lo",
                                    source_api="s2", citation_count=0))
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                resp = client.get("/api/graph")
                data = resp.get_json()
                scores = {n["id"]: n["score"] for n in data["nodes"]}
                assert scores["hi"] > scores["lo"], \
                    f"hi ({scores['hi']:.3f}) should score above lo ({scores['lo']:.3f})"


# ------------------------------------------------------------------
# storage: delete_papers_by_label (for Prune feature)
# ------------------------------------------------------------------

class TestDeletePapersByLabel:
    def test_deletes_disliked_papers_and_their_edges(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(Paper(paper_id="seed1", title="S", source_api="s2"), label="seed")
            store.upsert_paper(Paper(paper_id="bad1",  title="B", source_api="s2"), label="disliked")
            store.add_edges("seed1", ["bad1"])
            store.delete_papers_by_label("disliked")
            assert store.get_paper("bad1") is None
            edges = store.all_edges()
            assert ("seed1", "bad1") not in edges

    def test_returns_count_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(Paper(paper_id="d1", title="D1", source_api="s2"), label="disliked")
            store.upsert_paper(Paper(paper_id="d2", title="D2", source_api="s2"), label="disliked")
            count = store.delete_papers_by_label("disliked")
            assert count == 2

    def test_non_disliked_papers_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(Paper(paper_id="keep1", title="K", source_api="s2"), label="liked")
            store.upsert_paper(Paper(paper_id="del1",  title="D", source_api="s2"), label="disliked")
            store.delete_papers_by_label("disliked")
            assert store.get_paper("keep1") is not None

    def test_zero_when_no_matching_label(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            count = store.delete_papers_by_label("disliked")
            assert count == 0


# ------------------------------------------------------------------
# graph_viz: POST /api/graph/expand-prune and GET /api/expand/status
# ------------------------------------------------------------------

class TestExpandPruneApi:
    def _client(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    @staticmethod
    def _sync_thread_patch():
        """Patch threading.Thread to run target synchronously — avoids Windows file-lock races."""
        class _SyncThread:
            def __init__(self, target=None, daemon=None, **_kw):
                self._target = target
            def start(self):
                if self._target:
                    self._target()
        return patch("graph_viz.threading.Thread", _SyncThread)

    def test_prune_removes_disliked_from_db(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="s1", title="S", source_api="s2"), label="seed")
            store.upsert_paper(Paper(paper_id="b1", title="B", source_api="s2"), label="disliked")
            with self._sync_thread_patch(), \
                 patch("graph_viz.expand_network", return_value=0):
                with self._client(db) as client:
                    resp = client.post("/api/graph/expand-prune",
                                       json={"hops": 1},
                                       content_type="application/json")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["pruned"] == 1
            assert Store(db).get_paper("b1") is None

    def test_no_seeds_returns_done_immediately(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)  # init schema only
            # No seed file present either -- must not fall back to the real
            # project seed_papers.txt, which would attempt live S2 calls.
            with patch.object(graph_viz, "SEED_TITLES_FILE", Path(d) / "no_such_file.txt"):
                with self._client(db) as client:
                    resp = client.post("/api/graph/expand-prune",
                                       json={},
                                       content_type="application/json")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "done"
            assert data["added"] == 0

    def test_status_endpoint_returns_job_state(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="s1", title="S", source_api="s2"), label="seed")
            with self._sync_thread_patch(), \
                 patch("graph_viz.expand_network", return_value=5):
                with self._client(db) as client:
                    r1 = client.post("/api/graph/expand-prune",
                                     json={}, content_type="application/json")
                    job_id = r1.get_json().get("job_id")
                    if job_id:
                        r2 = client.get(f"/api/expand/status/{job_id}")
                        assert r2.status_code == 200
                        assert r2.get_json()["status"] in ("running", "done")

    def test_status_unknown_job_returns_404(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with self._client(db) as client:
                resp = client.get("/api/expand/status/nonexistent")
            assert resp.status_code == 404


# ------------------------------------------------------------------
# graph_viz: POST /api/paper/add-by-title  (Feature: manual seed add)
# ------------------------------------------------------------------

class TestAddPaperByTitle:
    def _client(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    _FAKE_RECORD = {
        "paperId": "abc123",
        "title": "Attention Is All You Need",
        "year": 2017,
        "citationCount": 50000,
        "venue": "NeurIPS",
        "authors": [{"name": "Vaswani"}],
        "fieldsOfStudy": ["Computer Science"],
        "externalIds": {},
        "abstract": "The dominant sequence transduction models...",
    }

    def test_missing_title_returns_400(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with self._client(db) as client:
                resp = client.post("/api/paper/add-by-title",
                                   json={},
                                   content_type="application/json")
            assert resp.status_code == 400

    def test_empty_title_returns_400(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with self._client(db) as client:
                resp = client.post("/api/paper/add-by-title",
                                   json={"title": "   "},
                                   content_type="application/json")
            assert resp.status_code == 400

    def test_no_s2_match_returns_404(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with patch("graph_viz.SemanticScholarClient") as MockS2:
                MockS2.return_value.search_paper_by_title.return_value = None
                with self._client(db) as client:
                    resp = client.post("/api/paper/add-by-title",
                                       json={"title": "Nonexistent Paper XYZ"},
                                       content_type="application/json")
            assert resp.status_code == 404

    def test_stores_paper_as_seed(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with patch("graph_viz.SemanticScholarClient") as MockS2:
                MockS2.return_value.search_paper_by_title.return_value = self._FAKE_RECORD
                with self._client(db) as client:
                    resp = client.post("/api/paper/add-by-title",
                                       json={"title": "Attention Is All You Need"},
                                       content_type="application/json")
                    assert resp.status_code == 200
            paper = Store(db).get_paper("abc123")
            assert paper is not None
            assert paper.label == "seed"

    def test_returns_paper_json_with_correct_fields(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with patch("graph_viz.SemanticScholarClient") as MockS2:
                MockS2.return_value.search_paper_by_title.return_value = self._FAKE_RECORD
                with self._client(db) as client:
                    resp = client.post("/api/paper/add-by-title",
                                       json={"title": "Attention Is All You Need"},
                                       content_type="application/json")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["paper_id"] == "abc123"
            assert data["title"] == "Attention Is All You Need"
            assert data["label"] == "seed"
            assert data["year"] == 2017

    def test_added_paper_appears_in_graph_api(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with patch("graph_viz.SemanticScholarClient") as MockS2:
                MockS2.return_value.search_paper_by_title.return_value = self._FAKE_RECORD
                with self._client(db) as client:
                    client.post("/api/paper/add-by-title",
                                json={"title": "Attention Is All You Need"},
                                content_type="application/json")
                    resp = client.get("/api/graph")
                    data = resp.get_json()
            node_ids = [n["id"] for n in data["nodes"]]
            assert "abc123" in node_ids

    def test_adding_duplicate_title_is_idempotent(self):
        """Adding the same paper twice does not create duplicates; label stays seed."""
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with patch("graph_viz.SemanticScholarClient") as MockS2:
                MockS2.return_value.search_paper_by_title.return_value = self._FAKE_RECORD
                with self._client(db) as client:
                    client.post("/api/paper/add-by-title",
                                json={"title": "Attention Is All You Need"},
                                content_type="application/json")
                    client.post("/api/paper/add-by-title",
                                json={"title": "Attention Is All You Need"},
                                content_type="application/json")
            seeds = Store(db).papers_with_label(["seed"])
            assert len([p for p in seeds if p.paper_id == "abc123"]) == 1


# ------------------------------------------------------------------
# graph_viz: node neighborhood highlight (pure frontend feature)
# Structural tests: verify the HTML served by Flask contains the
# required CSS classes and JS functions. Behavioral correctness
# (D3 DOM mutations) requires browser testing — noted as known gap.
# ------------------------------------------------------------------

class TestNodeHighlightFeature:
    def _get_html(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        with graph_viz.app.test_client() as client:
            resp = client.get("/")
            assert resp.status_code == 200
            return resp.data.decode()

    def test_dimmed_css_class_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
            assert ".node.dimmed" in html, \
                "CSS rule .node.dimmed not found — dimming of non-neighbor nodes not implemented"

    def test_neighbor_css_class_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
            assert ".node.neighbor" in html, \
                "CSS rule .node.neighbor not found — neighbor highlight not implemented"

    def test_link_dimmed_css_class_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
            assert ".link.dimmed" in html, \
                "CSS rule .link.dimmed not found — non-relevant edge dimming not implemented"

    def test_highlight_neighborhood_function_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
            assert "highlightNeighborhood" in html, \
                "highlightNeighborhood() JS function not found in template"

    def test_clear_highlight_function_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._get_html(db)
            assert "clearHighlight" in html, \
                "clearHighlight() function not found — background-click reset not wired up"


# ------------------------------------------------------------------
# graph_expander: max_new cap (Bug: expansion blocked when store
# already has >= max_nodes papers)
# ------------------------------------------------------------------

def _make_s2_client(ref_list=None, cite_list=None):
    """Return a MagicMock S2 client that yields ref_list as references."""
    client = MagicMock()
    client.get_references.return_value = ref_list or []
    client.get_citations.return_value  = cite_list or []
    return client


def _cs_ref(pid, citation_count=10):
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": 2020, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": None, "tldr": None, "embedding": None,
    }


class TestMaxNewExpansionCap:
    """
    The old max_nodes cap counted ALL papers in the store, so a store
    with >= max_nodes existing papers could never expand.  The fix adds
    max_new which caps only newly-added papers per expansion run.
    """

    def test_expansion_adds_papers_when_store_already_at_cap(self):
        """Store has 600 papers (> default max_nodes=500); expansion must still add new ones."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            # Pre-fill store with 600 papers (simulating a large existing network)
            for i in range(600):
                store.upsert_paper(Paper(paper_id=f"existing_{i}", title=f"E{i}", source_api="s2"))
            seed = Paper(paper_id="seed1", title="Seed", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            s2 = _make_s2_client(ref_list=[_cs_ref("new_paper_1"), _cs_ref("new_paper_2")])
            added = expand_network(s2, MagicMock(), store, ["seed1"],
                                   hops=1, max_nodes=10000, max_new=50)
            assert added > 0, \
                "expand_network returned 0 additions even though store is below max_nodes — max_new cap is not used"

    def test_max_new_limits_papers_added(self):
        """max_new=2 must stop after adding 2 papers even if more are available.
        Uses seed CITATIONS (not references) -- a seed paper's own references
        are always added in full regardless of max_new (see
        tests/test_citation_ranking.py::TestSeedReferencesUncapped); only
        citations remain subject to the max_new cap for seeds."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            seed = Paper(paper_id="s1", title="S", source_api="s2",
                         field_of_study=["Computer Science"])
            store.upsert_paper(seed, label="seed")

            cites = [_cs_ref(f"p{i}") for i in range(20)]
            s2 = _make_s2_client(cite_list=cites)
            added = expand_network(s2, MagicMock(), store, ["s1"],
                                   hops=1, max_nodes=10000, max_new=2)
            assert added <= 2, f"Expected at most 2 new papers but got {added}"

    def test_expand_prune_api_accepts_max_papers_param(self):
        """POST /api/graph/expand-prune should accept and use a max_papers body field."""
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="s1", title="S", source_api="s2"), label="seed")

            class _SyncThread:
                def __init__(self, target=None, daemon=None, **_kw):
                    self._target = target
                def start(self):
                    if self._target: self._target()

            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network", return_value=5) as mock_expand:
                graph_viz.DB_PATH = db
                graph_viz.app.config["TESTING"] = True
                with graph_viz.app.test_client() as client:
                    resp = client.post("/api/graph/expand-prune",
                                       json={"hops": 1, "max_papers": 100},
                                       content_type="application/json")
                assert resp.status_code == 200
                # Verify max_new was forwarded from max_papers=100
                kw = mock_expand.call_args[1] if mock_expand.call_args[1] else {}
                assert kw.get("max_new") == 100, \
                    f"expand_network not called with max_new=100; got kwargs={kw}"
