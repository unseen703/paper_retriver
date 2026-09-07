"""
tests/test_features_v5.py
-------------------------
TDD tests for v5 feature:
  "See liked and seed papers and option to remove/prune them from the network."

User journeys:
  1. As a user, I can open a panel listing all seed and liked papers.
  2. As a user, I can click Remove on any seed/liked paper to delete it and its edges.

Affected APIs:
  - GET  /api/papers/seeds-liked  -> list [{paper_id, title, year, citation_count, label}]
  - DELETE /api/paper/<paper_id>  -> {ok: true, paper_id}  (404 if not found)
  - GET  /  -> graph.html must contain seeds-liked panel + JS functions

Callers: pytest only.
User verbatim: "add a feature to see liked and seed papers and option to remove/prune them from network."
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from storage import Paper, Store


# helpers

def _make_paper(pid, title="T", year=2020, citation_count=10,
                venue=None) -> Paper:
    return Paper(paper_id=pid, title=title, source_api="s2",
                 year=year, citation_count=citation_count,
                 venue=venue, field_of_study=["Computer Science"])


def _app_client(db: Path):
    import graph_viz
    graph_viz.DB_PATH = db
    graph_viz.app.config["TESTING"] = True
    return graph_viz.app.test_client()


# Storage layer: delete_paper

class TestStorageDeletePaper:
    """store.delete_paper(paper_id) removes the paper row and all its edges."""

    def test_delete_removes_paper_row(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_make_paper("p1"), label="seed")
            store.delete_paper("p1")
            assert store.get_paper("p1") is None

    def test_delete_removes_outgoing_edges(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_make_paper("p1"), label="seed")
            store.upsert_paper(_make_paper("p2"))
            store.add_edges("p1", ["p2"])
            store.delete_paper("p1")
            assert ("p1", "p2") not in store.all_edges()

    def test_delete_removes_incoming_edges(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_make_paper("p1"), label="seed")
            store.upsert_paper(_make_paper("p2"))
            store.add_edges("p2", ["p1"])
            store.delete_paper("p1")
            assert ("p2", "p1") not in store.all_edges()

    def test_delete_nonexistent_paper_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.delete_paper("ghost")  # must not raise

    def test_delete_leaves_other_papers_intact(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_make_paper("p1"), label="seed")
            store.upsert_paper(_make_paper("p2"), label="liked")
            store.delete_paper("p1")
            assert store.get_paper("p2") is not None


# API: GET /api/papers/seeds-liked

class TestSeedsLikedApi:

    def test_returns_seed_and_liked_papers(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("s1", title="Seed One"), label="seed")
            store.upsert_paper(_make_paper("l1", title="Liked One"), label="liked")
            store.upsert_paper(_make_paper("c1", title="Candidate"))
            with _app_client(db) as client:
                data = client.get("/api/papers/seeds-liked").get_json()
        ids = {p["paper_id"] for p in data["papers"]}
        assert ids == {"s1", "l1"}, f"Expected only seed+liked, got {ids}"

    def test_each_paper_has_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(
                _make_paper("s1", title="Attention", year=2017, citation_count=9000),
                label="seed",
            )
            with _app_client(db) as client:
                data = client.get("/api/papers/seeds-liked").get_json()
        p = data["papers"][0]
        for field in ("paper_id", "title", "year", "citation_count", "label"):
            assert field in p, f"Missing field {field!r}"
        assert p["label"] == "seed"
        assert p["year"] == 2017
        assert p["citation_count"] == 9000

    def test_empty_when_no_seeds_or_liked(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with _app_client(db) as client:
                data = client.get("/api/papers/seeds-liked").get_json()
        assert data["papers"] == []

    def test_does_not_include_disliked_or_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("d1"), label="disliked")
            store.upsert_paper(_make_paper("sk1"), label="skipped")
            store.upsert_paper(_make_paper("s1"), label="seed")
            with _app_client(db) as client:
                data = client.get("/api/papers/seeds-liked").get_json()
        ids = {p["paper_id"] for p in data["papers"]}
        assert "d1" not in ids and "sk1" not in ids


# API: DELETE /api/paper/<paper_id>

class TestDeletePaperApi:

    def test_delete_existing_paper_returns_ok(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("p1"), label="seed")
            with _app_client(db) as client:
                resp = client.delete("/api/paper/p1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["paper_id"] == "p1"

    def test_delete_removes_paper_from_graph_api(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("p1"), label="seed")
            with _app_client(db) as client:
                client.delete("/api/paper/p1")
                nodes = {n["id"] for n in client.get("/api/graph").get_json()["nodes"]}
        assert "p1" not in nodes

    def test_delete_nonexistent_paper_returns_404(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with _app_client(db) as client:
                resp = client.delete("/api/paper/ghost")
        assert resp.status_code == 404

    def test_delete_removes_edges_associated_with_paper(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("p1"), label="seed")
            store.upsert_paper(_make_paper("p2"))
            store.add_edges("p1", ["p2"])
            with _app_client(db) as client:
                client.delete("/api/paper/p1")
            edges = store.all_edges()
        assert ("p1", "p2") not in edges


# Frontend: HTML structure

class TestSeedsLikedUi:

    def _html(self, db):
        with _app_client(db) as client:
            return client.get("/").data.decode()

    def test_seeds_liked_panel_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "seeds-liked-panel" in html, \
            "HTML must contain element with id 'seeds-liked-panel'"

    def test_load_seeds_liked_js_function_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "loadSeedsLiked" in html, \
            "HTML must contain JS function loadSeedsLiked()"

    def test_remove_paper_js_function_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "removePaper" in html, \
            "HTML must contain JS function removePaper() for pruning papers"
