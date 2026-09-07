"""
tests/test_features_v8.py
--------------------------
TDD tests for v8 feature:
  The "Clear network" button (and the expand button's auto-bootstrap-when-
  empty path) now resolve seeds from seed_titles.txt IN THE PROJECT FOLDER
  -- the file is the live source of truth -- instead of the (possibly
  stale) previously-resolved rows in the seed_titles DB table.

User journey: As a user, when I edit seed_titles.txt to add/remove/change
seed papers and then click "Clear network", the new network is built from
the file's CURRENT contents, not whatever was resolved in some earlier run.

Affected APIs:
  - storage.py: Store.clear_seed_titles()
  - graph_viz.py: SEED_TITLES_FILE, _load_seed_titles_file(),
    _resolve_seeds_from_file(); api_clear_and_rebuild and
    api_expand_prune's auto-bootstrap path both use these now.

Callers: pytest only.
User verbatim: "remove entire network with clear button and start from
seed paper txt file in the folder to build new network."
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


_ABC_MATCH = {
    "paperId": "abc123", "title": "Attention Is All You Need",
    "year": 2017, "citationCount": 100000,
    "authors": [], "fieldsOfStudy": ["Computer Science"],
    "externalIds": {}, "venue": None, "abstract": None,
    "tldr": None, "embedding": None,
}


# Storage: clear_seed_titles

class TestClearSeedTitles:
    def test_clear_seed_titles_removes_all_rows(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.record_seed_title("Title A", "idA", 1.0)
            store.record_seed_title("Title B", None, 0.0)
            store.clear_seed_titles()
            assert store.get_seed_titles() == []


# graph_viz: _load_seed_titles_file

class TestLoadSeedTitlesFile:

    def test_reads_non_blank_non_comment_lines(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "seeds.txt"
            f.write_text("Title One\n\n# a comment\nTitle Two\n   \n", encoding="utf-8")
            with patch.object(graph_viz, "SEED_TITLES_FILE", f):
                titles = graph_viz._load_seed_titles_file()
        assert titles == ["Title One", "Title Two"]

    def test_returns_empty_list_when_file_missing(self):
        import graph_viz
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "does_not_exist.txt"
            with patch.object(graph_viz, "SEED_TITLES_FILE", f):
                titles = graph_viz._load_seed_titles_file()
        assert titles == []


# API: clear-and-rebuild now sources from the file, clears seed_titles too

# NOTE: /api/graph/clear-and-rebuild is now a pure, synchronous DB wipe
# (no seed resolution at all) -- see tests/test_features_v9.py for its
# current contract. File-based seed resolution now lives solely in
# _resolve_seeds_from_file(), exercised below via the expand button's
# auto-bootstrap path.

# API: expand-prune's auto-bootstrap-when-empty also sources from the file

class TestExpandPruneBootstrapUsesFile:

    def test_auto_bootstrap_resolves_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            f = Path(d) / "seeds.txt"
            f.write_text("Attention Is All You Need\n", encoding="utf-8")
            store = Store(db)
            store.record_seed_title("Some Stale Old Title", "stale999", 1.0)

            import graph_viz
            with patch.object(graph_viz, "SEED_TITLES_FILE", f), \
                 patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_network", return_value=3) as mock_expand, \
                 patch.object(graph_viz.SemanticScholarClient, "search_paper_by_title",
                             return_value=_ABC_MATCH), \
                 patch.object(graph_viz.SemanticScholarClient, "get_paper",
                             return_value=_ABC_MATCH):
                graph_viz.DB_PATH = db
                graph_viz.app.config["TESTING"] = True
                with graph_viz.app.test_client() as c:
                    resp = c.post("/api/graph/expand-prune", json={"hops": 1, "max_papers": 50})
                    data = resp.get_json()
                    job = c.get(f"/api/expand/status/{data['job_id']}").get_json()

        assert job["status"] == "done"
        mock_expand.assert_called_once()
        called_seed_ids = mock_expand.call_args.args[3]
        assert "abc123" in called_seed_ids
