"""
tests/test_features_v9.py
--------------------------
TDD tests for v9 feature:
  "Clear network" is simplified to a PURE wipe -- papers, edges, metrics,
  AND seed_titles -- with NOTHING restored automatically. The auto-bootstrap
  responsibility (resolving seeds from SEED_TITLES_FILE when the network is
  empty) now lives entirely in the Expand button (added last turn), making
  Clear's own seed-restoration redundant and its "(keep seeds)" label wrong.

User journey: As a user, when I click "Clear network", it removes
absolutely everything -- no residual seed papers, no lingering DB rows --
so the button's label and behavior are unambiguous. I then use the
existing "Expand" button (which already auto-bootstraps from the seed
file when the network is empty) to rebuild.

Affected API: graph_viz.py: POST /api/graph/clear-and-rebuild (now a pure,
synchronous wipe -- no background job, no S2 calls, no seed restoration).

Callers: pytest only.
User verbatim: "why clear button still shows keep seeds? it should remove
entire network." followed by "on GUI part remove part saying keep seeds."
and "just clear network is fine" (button label).
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


class TestClearIsPureWipe:

    def test_clear_removes_all_papers_edges_and_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("p2")[0])
            store.add_edges("p1", ["p2"])
            with _app_client(db) as client:
                resp = client.post("/api/graph/clear-and-rebuild", json={})
            assert resp.status_code == 200
            assert store.all_paper_ids() == set()
            assert store.all_edges() == []

    def test_clear_also_wipes_seed_titles(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.record_seed_title("Some Title", "p1", 1.0)
            with _app_client(db) as client:
                client.post("/api/graph/clear-and-rebuild", json={})
            assert store.get_seed_titles() == []

    def test_clear_does_not_restore_any_seed_paper(self):
        """The old behavior re-hydrated and re-added seed papers during
        clear -- this must no longer happen; the network stays empty."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            with _app_client(db) as client:
                client.post("/api/graph/clear-and-rebuild", json={})
            assert store.all_paper_ids() == set(), \
                "clear must not restore/re-add any seed paper -- that's the Expand button's job now"

    def test_clear_succeeds_even_when_network_already_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with _app_client(db) as client:
                resp = client.post("/api/graph/clear-and-rebuild", json={})
            assert resp.status_code == 200

    def test_clear_never_calls_semantic_scholar(self):
        """A pure wipe must not make any S2 API calls (no resolving, no
        hydrating) -- it should be instant, not rate-limited."""
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            with patch.object(graph_viz.SemanticScholarClient, "search_paper_by_title") as mock_search, \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper") as mock_get:
                with _app_client(db) as client:
                    client.post("/api/graph/clear-and-rebuild", json={})
            mock_search.assert_not_called()
            mock_get.assert_not_called()

    def test_clear_is_synchronous_no_job_id(self):
        """No background job is needed for a pure DB wipe -- the response
        should just confirm success directly, not hand back a job_id to poll."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            with _app_client(db) as client:
                resp = client.post("/api/graph/clear-and-rebuild", json={})
                data = resp.get_json()
            assert "job_id" not in data
            assert data.get("ok") is True


class TestClearButtonUiWording:

    def _html(self, db):
        with _app_client(db) as client:
            return client.get("/").data.decode()

    def test_button_and_confirm_text_do_not_mention_keep_seeds(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "keep seeds" not in html.lower(), \
            "Clear button/confirm text must not claim seeds are kept -- it's a full wipe now"

    def test_clear_button_still_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "clear-rebuild-btn" in html or "clearAndRebuild" in html
