"""
tests/test_features_v7.py
--------------------------
TDD tests for v7 feature:
  The existing "Expand" button (/api/graph/expand-prune) auto-bootstraps
  from stored seed titles when the network has zero papers, instead of
  just reporting "no seeds to expand from" and doing nothing.

User journey: As a user, when I click "Expand" on a completely empty
network, the system automatically restores my seed papers from the
stored seed titles and then expands from them in the same click.

Affected API: graph_viz.py: POST /api/graph/expand-prune

Callers: pytest only.
User verbatim: "Add rebuild functiona;ity to expand button, if no nodes
present it shold invoke rebuild feature."
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from storage import Paper, Store


def _p(pid, label=None):
    return Paper(paper_id=pid, title=pid, source_api="s2",
                field_of_study=["Computer Science"]), label


def _app_client(db: Path):
    import graph_viz
    graph_viz.DB_PATH = db
    graph_viz.app.config["TESTING"] = True
    return graph_viz.app.test_client()


class _SyncThread:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target
    def start(self):
        if self._target:
            self._target()


_ABC_RECORD = {
    "paperId": "abc123", "title": "Attention Is All You Need",
    "year": 2017, "citationCount": 100000,
    "authors": [], "fieldsOfStudy": ["Computer Science"],
    "externalIds": {}, "venue": None, "abstract": None,
    "tldr": None, "embedding": None,
}


class TestExpandPruneAutoRebuildsWhenEmpty:

    def test_auto_rebuilds_and_expands_when_network_is_empty(self):
        """Seeds now resolve from SEED_TITLES_FILE (see tests/test_features_v8.py),
        not the DB seed_titles table -- this test points that file at a temp
        file with known content."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            seed_file = Path(d) / "seeds.txt"
            seed_file.write_text("Attention Is All You Need\n", encoding="utf-8")
            store = Store(db)

            import graph_viz
            with patch.object(graph_viz, "SEED_TITLES_FILE", seed_file), \
                 patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network", return_value=5) as mock_expand, \
                 patch.object(graph_viz.SemanticScholarClient, "search_paper_by_title",
                             return_value=_ABC_RECORD), \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper",
                             return_value=_ABC_RECORD):
                client = _app_client(db)
                with client as c:
                    resp = c.post("/api/graph/expand-prune", json={"hops": 1, "max_papers": 50})
                    data = resp.get_json()
                    job = c.get(f"/api/expand/status/{data['job_id']}").get_json()
                    restored = store.get_paper("abc123")

        assert resp.status_code == 200
        assert job["status"] == "done"
        assert restored is not None
        assert restored.label == "seed"
        mock_expand.assert_called_once()
        called_seed_ids = mock_expand.call_args.args[3]
        assert "abc123" in called_seed_ids

    def test_does_not_rebuild_when_candidates_exist_but_no_seed_liked(self):
        """Network has papers (just none labeled seed/liked) -- 'no nodes
        present' is false, so the old 'no seeds to expand from' message
        still applies; auto-rebuild must not trigger."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.record_seed_title("Attention Is All You Need", "abc123", 1.0)
            store.upsert_paper(_p("candidate1")[0])  # unlabeled, present

            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network") as mock_expand, \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper",
                             return_value=_ABC_RECORD):
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-prune", json={})
                    data = resp.get_json()
                    assert store.get_paper("abc123") is None

        assert data.get("message") == "No seed/liked papers to expand from"
        mock_expand.assert_not_called()

    def test_returns_message_when_empty_and_no_seed_titles_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.record_seed_title("Unresolved Title", None, 0.0)

            import graph_viz
            with patch.object(graph_viz, "SEED_TITLES_FILE", Path(d) / "no_such_file.txt"), \
                 patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network") as mock_expand:
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-prune", json={})
                    data = resp.get_json()

        assert resp.status_code == 200
        assert data["status"] == "done"
        assert data["added"] == 0
        mock_expand.assert_not_called()

    def test_normal_flow_unaffected_when_seeds_already_exist(self):
        """Regression: when seed/liked papers already exist, behavior is
        identical to before -- no auto-rebuild, no extra S2 calls."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("s1", "seed")[0], label="seed")

            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network", return_value=2) as mock_expand, \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper") as mock_get_paper:
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-prune", json={"hops": 1, "max_papers": 50})
                    data = resp.get_json()
                    job = c.get(f"/api/expand/status/{data['job_id']}").get_json()

        assert job["status"] == "done"
        mock_expand.assert_called_once()
        called_seed_ids = mock_expand.call_args.args[3]
        assert called_seed_ids == ["s1"]
        mock_get_paper.assert_not_called()

    def test_response_indicates_rebuilding_when_bootstrapping(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            seed_file = Path(d) / "seeds.txt"
            seed_file.write_text("Attention Is All You Need\n", encoding="utf-8")
            store = Store(db)

            import graph_viz
            with patch.object(graph_viz, "SEED_TITLES_FILE", seed_file), \
                 patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network", return_value=1), \
                 patch.object(graph_viz.SemanticScholarClient, "search_paper_by_title",
                             return_value=_ABC_RECORD), \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper",
                             return_value=_ABC_RECORD):
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-prune", json={})
                    data = resp.get_json()

        assert data.get("rebuilding") is True
