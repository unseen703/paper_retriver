"""
tests/test_exploration.py
--------------------------
TDD tests for separating "exploration" from "recommendation".

User journeys:
  1. As a user, when I like a paper, the system explores its FULL
     citation neighborhood: every reference and every citing paper
     (up to Semantic Scholar's per-request page cap), filters out
     out-of-domain fields, and adds the rest to the network.
  2. As a developer, exploration mechanics (fetch + filter + add one
     paper's direct neighborhood) live in their own module
     (exploration.py), separate from:
       - graph_expander.py's priority-capped multi-hop BFS traversal
       - citation_network.py / features.py's scoring & ranking logic

Affected APIs:
  - exploration.explore_paper(paper_id, s2_client, oa_client, store) -> dict
  - graph_viz.py: POST /api/paper/<id>/label with label="liked" calls
    graph_viz.explore_paper(...) (module-level reference, monkeypatchable)

Callers: pytest only.
User verbatim: "Seperate the code of exploration and recommendation. i.e for
reocomonded node explore all its reference and add it to network and also add
the papers that cites the recomandaed papers after initial filtering of sub types."
Clarified: trigger = liked papers; ref scope = no cap (S2 page max);
filter = existing is_relevant_paper (field_filter.py).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import Paper, Store


# helpers

def _s2_paper(pid, citation_count=10, year=2020, fields=None):
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": year, "authors": [],
        "fieldsOfStudy": fields if fields is not None else ["Computer Science"],
        "venue": None, "tldr": None, "embedding": None,
    }


def _mock_s2(refs=None, cites=None):
    client = MagicMock()
    client.get_references.return_value = refs or []
    client.get_citations.return_value = cites or []
    return client


def _seed_store(db_path, root_id="root"):
    store = Store(db_path)
    store.upsert_paper(
        Paper(paper_id=root_id, title="Root", source_api="s2",
              field_of_study=["Computer Science"]),
        label="liked",
    )
    return store


# exploration.explore_paper: fetch scope (no cap)

class TestExplorePaperFetchScope:

    def test_fetches_references_with_max_page_limit(self):
        from exploration import explore_paper, MAX_NEIGHBORS_PER_CALL
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            s2 = _mock_s2()
            explore_paper("root", s2, MagicMock(), store)
        s2.get_references.assert_called_once_with("root", limit=MAX_NEIGHBORS_PER_CALL)

    def test_fetches_citations_with_max_page_limit(self):
        from exploration import explore_paper, MAX_NEIGHBORS_PER_CALL
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            s2 = _mock_s2()
            explore_paper("root", s2, MagicMock(), store)
        s2.get_citations.assert_called_once_with("root", limit=MAX_NEIGHBORS_PER_CALL)

    def test_max_page_limit_is_at_least_1000(self):
        """No artificial cap below S2's own per-request page maximum (1000)."""
        from exploration import MAX_NEIGHBORS_PER_CALL
        assert MAX_NEIGHBORS_PER_CALL >= 1000


# exploration.explore_paper: adds relevant refs + citing papers

class TestExplorePaperAddsRelevant:

    def test_relevant_reference_added_and_edge_created(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            ref = _s2_paper("ref1")
            s2 = _mock_s2(refs=[ref])
            explore_paper("root", s2, MagicMock(), store)
            assert store.get_paper("ref1") is not None
            assert ("root", "ref1") in store.all_edges()

    def test_relevant_citing_paper_added_with_reversed_edge(self):
        """A paper that cites 'root' must get edge citer -> root (not root -> citer)."""
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            citer = _s2_paper("citer1")
            s2 = _mock_s2(cites=[citer])
            explore_paper("root", s2, MagicMock(), store)
            assert store.get_paper("citer1") is not None
            assert ("citer1", "root") in store.all_edges()

    def test_multiple_refs_and_cites_all_added(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            refs = [_s2_paper(f"ref{i}") for i in range(5)]
            cites = [_s2_paper(f"cite{i}") for i in range(3)]
            s2 = _mock_s2(refs=refs, cites=cites)
            result = explore_paper("root", s2, MagicMock(), store)
            assert result["refs_added"] == 5
            assert result["cites_added"] == 3
            all_ids = store.all_paper_ids()
            for i in range(5):
                assert f"ref{i}" in all_ids
            for i in range(3):
                assert f"cite{i}" in all_ids


# exploration.explore_paper: field-of-study filtering

class TestExplorePaperFiltersIrrelevant:

    def test_biology_only_reference_is_skipped(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            bio_ref = _s2_paper("bio1", fields=["Biology"])
            s2 = _mock_s2(refs=[bio_ref])
            result = explore_paper("root", s2, MagicMock(), store)
            assert store.get_paper("bio1") is None
            assert result["refs_skipped"] == 1
            assert result["refs_added"] == 0

    def test_medicine_only_citing_paper_is_skipped(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            med_citer = _s2_paper("med1", fields=["Medicine"])
            s2 = _mock_s2(cites=[med_citer])
            result = explore_paper("root", s2, MagicMock(), store)
            assert store.get_paper("med1") is None
            assert result["cites_skipped"] == 1
            assert result["cites_added"] == 0

    def test_mixed_relevant_and_irrelevant_refs(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            refs = [
                _s2_paper("cs1", fields=["Computer Science"]),
                _s2_paper("bio1", fields=["Biology"]),
                _s2_paper("cs2", fields=["Computer Science"]),
            ]
            s2 = _mock_s2(refs=refs)
            result = explore_paper("root", s2, MagicMock(), store)
            assert result["refs_added"] == 2
            assert result["refs_skipped"] == 1
            assert store.get_paper("cs1") is not None
            assert store.get_paper("cs2") is not None
            assert store.get_paper("bio1") is None


# exploration.explore_paper: return shape + resilience

class TestExplorePaperReturnShapeAndResilience:

    def test_returns_all_four_count_keys(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            s2 = _mock_s2()
            result = explore_paper("root", s2, MagicMock(), store)
        for key in ("refs_added", "refs_skipped", "cites_added", "cites_skipped"):
            assert key in result

    def test_references_fetch_exception_does_not_raise(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            s2 = MagicMock()
            s2.get_references.side_effect = Exception("network down")
            s2.get_citations.return_value = []
            result = explore_paper("root", s2, MagicMock(), store)
        assert result["refs_added"] == 0

    def test_citations_fetch_exception_does_not_raise(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            s2 = MagicMock()
            s2.get_references.return_value = []
            s2.get_citations.side_effect = Exception("network down")
            result = explore_paper("root", s2, MagicMock(), store)
        assert result["cites_added"] == 0


# graph_viz.py wiring: liking a paper triggers exploration

class TestLikeTriggersExploration:

    def _client(self, db, monkeypatch, fake_explore):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        monkeypatch.setattr(graph_viz, "explore_paper", fake_explore)
        return graph_viz.app.test_client()

    def test_liking_a_paper_calls_explore_paper(self, monkeypatch):
        calls = []
        def fake_explore(paper_id, s2, oa, store):
            calls.append(paper_id)
            return {"refs_added": 0, "refs_skipped": 0, "cites_added": 0, "cites_skipped": 0}

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="seed")
            client = self._client(db, monkeypatch, fake_explore)
            resp = client.post("/api/paper/p1/label", json={"label": "liked"})
        assert resp.status_code == 200
        assert calls == ["p1"]

    def test_disliking_does_not_call_explore_paper(self, monkeypatch):
        calls = []
        def fake_explore(paper_id, s2, oa, store):
            calls.append(paper_id)
            return {}

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="seed")
            client = self._client(db, monkeypatch, fake_explore)
            client.post("/api/paper/p1/label", json={"label": "disliked"})
        assert calls == []

    def test_skipping_does_not_call_explore_paper(self, monkeypatch):
        calls = []
        def fake_explore(paper_id, s2, oa, store):
            calls.append(paper_id)
            return {}

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="seed")
            client = self._client(db, monkeypatch, fake_explore)
            client.post("/api/paper/p1/label", json={"label": "skipped"})
        assert calls == []

    def test_clearing_label_does_not_call_explore_paper(self, monkeypatch):
        calls = []
        def fake_explore(paper_id, s2, oa, store):
            calls.append(paper_id)
            return {}

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="liked")
            client = self._client(db, monkeypatch, fake_explore)
            client.post("/api/paper/p1/label", json={"label": None})
        assert calls == []

    def test_response_includes_explored_summary_when_liked(self, monkeypatch):
        def fake_explore(paper_id, s2, oa, store):
            return {"refs_added": 4, "refs_skipped": 1, "cites_added": 2, "cites_skipped": 0}

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="seed")
            client = self._client(db, monkeypatch, fake_explore)
            resp = client.post("/api/paper/p1/label", json={"label": "liked"})
        data = resp.get_json()
        assert data["explored"] == {"refs_added": 4, "refs_skipped": 1, "cites_added": 2, "cites_skipped": 0}

    def test_exploration_exception_does_not_break_label_response(self, monkeypatch):
        """If exploration blows up, the label must still be set and API must still return 200."""
        def fake_explore(paper_id, s2, oa, store):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(Paper(paper_id="p1", title="P1", source_api="s2"), label="seed")
            client = self._client(db, monkeypatch, fake_explore)
            resp = client.post("/api/paper/p1/label", json={"label": "liked"})
            updated = store.get_paper("p1")
        assert resp.status_code == 200
        assert updated.label == "liked"
