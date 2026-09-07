"""
tests/test_review_fixes.py
---------------------------
TDD reproducers for issues #2-#10 surfaced by the 2026-09-06 high-effort
code review (issue #1, graph_viz.py's clear-and-rebuild dropping liked
papers, is intentionally out of scope for this round).

Bug list (matches the ReportFindings order, #1 excluded):
 #2  recommend.py            : expand_network call omits max_new, silently
                                falling back to 300 regardless of max_nodes
 #3  exploration.py          : explore_paper has no max_nodes ceiling
 #4  graph_expander.hydrate_ids : unguarded batch_get_papers call
 #5  storage.py              : migration swallows ANY OperationalError
 #6  graph_viz.py/graph.html : liking a paper doesn't refresh the graph view
 #7  exploration.py          : oa_client accepted but never used (no OA fallback)
 #8  exploration.py          : never calls store.update_distance
 #9  graph_viz.py            : node-score n_seeds excludes "liked" papers
 #10 semantic_scholar_client.py : raise_for_status() outside retry logic
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from storage import Paper, Store
from openalex_client import OpenAlexClient
from semantic_scholar_client import SemanticScholarClient


# ── helpers ───────────────────────────────────────────────────────────────────

def _tmp_store() -> Store:
    td = tempfile.mkdtemp()
    return Store(Path(td) / "test.sqlite3")


def _make_paper(pid, title="T", year=2023, citation_count=10, doi=None, venue=None):
    return Paper(paper_id=pid, title=title, year=year, citation_count=citation_count,
                 doi=doi, venue=venue)


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


def _seed_store(db_path, root_id="root", label="liked", doi=None):
    store = Store(db_path)
    store.upsert_paper(
        Paper(paper_id=root_id, title="Root", source_api="s2", doi=doi,
              field_of_study=["Computer Science"]),
        label=label,
    )
    return store


# ── Bug #3 — explore_paper has no max_nodes ceiling ───────────────────────────

class TestExplorePaperRespectsMaxNodes:
    def test_stops_adding_once_max_nodes_reached(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")  # 1 paper already in store
            refs = [_s2_paper(f"ref{i}") for i in range(5)]
            s2 = _mock_s2(refs=refs)
            result = explore_paper("root", s2, MagicMock(), store, max_nodes=3)
            # total starts at 1 (root); only 2 more may be added before hitting max_nodes=3
            assert result["refs_added"] == 2
            assert len(store.all_paper_ids()) == 3

    def test_default_max_nodes_does_not_break_existing_behavior(self):
        """No max_nodes passed -> the generous default must not artificially cap
        a small, ordinary exploration (regression guard for existing callers)."""
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db")
            refs = [_s2_paper(f"ref{i}") for i in range(5)]
            s2 = _mock_s2(refs=refs)
            result = explore_paper("root", s2, MagicMock(), store)
        assert result["refs_added"] == 5


# ── Bug #7 — oa_client accepted but never used ────────────────────────────────

class TestExplorePaperOpenAlexFallback:
    def test_falls_back_to_openalex_when_s2_has_nothing(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db", doi="10.1/xyz")
            s2 = _mock_s2()  # empty refs and cites -> S2 has no coverage

            oa = OpenAlexClient()
            oa.get_work_by_doi = MagicMock(return_value={
                "id": "https://openalex.org/W1",
                "referenced_works": ["https://openalex.org/W2"],
            })
            oa.get_references = MagicMock(return_value=[{
                "id": "https://openalex.org/W2",
                "display_title": "OA Ref",
                "publication_year": 2019,
                "cited_by_count": 5,
                "concepts": [{"display_name": "Computer Science", "level": 0}],
            }])
            oa.get_citations = MagicMock(return_value=[])

            result = explore_paper("root", s2, oa, store)
            assert result["refs_added"] == 1
            assert store.get_paper("W2") is not None

    def test_no_fallback_attempted_when_s2_has_data(self):
        """OA must not be consulted when S2 already returned something."""
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db", doi="10.1/xyz")
            ref = _s2_paper("ref1")
            s2 = _mock_s2(refs=[ref])
            oa = MagicMock()
            explore_paper("root", s2, oa, store)
        oa.get_work_by_doi.assert_not_called()


# ── Bug #8 — explore_paper never calls store.update_distance ─────────────────

class TestExplorePaperUpdatesDistance:
    def test_new_ref_gets_distance_one_past_a_seed(self):
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db", label="seed")
            store.update_distance("root", 0, ["root"])
            ref = _s2_paper("ref1")
            s2 = _mock_s2(refs=[ref])
            explore_paper("root", s2, MagicMock(), store)
            added = store.get_paper("ref1")
            assert added.distance_from_seed == 1
            assert "root" in added.seed_connections

    def test_new_ref_inherits_distance_from_a_non_seed_liked_paper(self):
        """The liked paper itself may not be a seed (distance already >0);
        children must build on ITS distance, not assume 0."""
        from exploration import explore_paper
        with tempfile.TemporaryDirectory() as d:
            store = _seed_store(Path(d) / "t.db", label="liked")
            store.update_distance("root", 3, ["some_seed"])
            ref = _s2_paper("ref1")
            s2 = _mock_s2(refs=[ref])
            explore_paper("root", s2, MagicMock(), store)
            added = store.get_paper("ref1")
            assert added.distance_from_seed == 4
            assert "some_seed" in added.seed_connections


# ── Bug #5 — storage.py migration swallows ANY OperationalError ──────────────

class TestMigrationErrorHandling:
    def test_swallows_duplicate_column_error(self):
        store = _tmp_store()
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("duplicate column name: doi")
        store._migrate(conn)  # must not raise

    def test_reraises_other_operational_errors(self):
        store = _tmp_store()
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        with pytest.raises(sqlite3.OperationalError):
            store._migrate(conn)


# ── Bug #4 — hydrate_ids unguarded batch_get_papers call ──────────────────────

class TestHydrateIdsHandlesBatchFailure:
    def test_batch_failure_does_not_raise(self):
        from graph_expander import hydrate_ids
        store = _tmp_store()
        s2 = MagicMock()
        s2.batch_get_papers.side_effect = Exception("network down")
        result = hydrate_ids(["p1", "p2"], s2, store)
        assert result == 0

    def test_successful_batch_still_hydrates(self):
        from graph_expander import hydrate_ids
        store = _tmp_store()
        s2 = MagicMock()
        s2.batch_get_papers.return_value = [_s2_paper("p1")]
        result = hydrate_ids(["p1"], s2, store)
        assert result == 1


# ── Bug #10 — raise_for_status() outside retry logic ──────────────────────────

class TestSemanticScholarRetriesTransientServerErrors:
    def test_transient_500_is_retried_and_succeeds(self):
        client = SemanticScholarClient(api_key=None)
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"paperId": "p1"}
        ok_resp.raise_for_status.return_value = None

        with patch.object(client.session, "request", side_effect=[bad_resp, ok_resp]):
            with patch("time.sleep"):
                data = client._request("GET", "http://example.com")
        assert data == {"paperId": "p1"}

    def test_persistent_500_raises_after_retries_exhausted(self):
        client = SemanticScholarClient(api_key=None, max_retries=2)
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")

        with patch.object(client.session, "request", return_value=bad_resp):
            with patch("time.sleep"):
                with pytest.raises(requests.exceptions.HTTPError):
                    client._request("GET", "http://example.com")

    def test_404_is_not_retried(self):
        """Client errors (other than 429) must still fail fast -- retrying a
        permanent 404 wastes time and hides the real problem."""
        client = SemanticScholarClient(api_key=None, max_retries=4)
        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")

        calls = {"n": 0}
        def _req(*a, **kw):
            calls["n"] += 1
            return bad_resp

        with patch.object(client.session, "request", side_effect=_req):
            with patch("time.sleep"):
                with pytest.raises(requests.exceptions.HTTPError):
                    client._request("GET", "http://example.com")
        assert calls["n"] == 1


# ── Bug #2 — recommend.py expand_network call omits max_new ──────────────────

class TestRecommendExpandKeepsMaxNewInSyncWithMaxNodes:
    def test_max_new_matches_max_nodes(self):
        import recommend
        store = _tmp_store()
        client = MagicMock()
        oa = MagicMock()
        seed = _make_paper("S1")
        with patch("recommend.expand_network") as mock_expand:
            recommend.expand_seed_network(client, oa, store, [seed], per_paper_limit=30)
        _, kwargs = mock_expand.call_args
        assert kwargs.get("max_new") is not None
        assert kwargs.get("max_new") == kwargs.get("max_nodes"), (
            f"max_new ({kwargs.get('max_new')}) must not silently fall back below "
            f"the configured max_nodes budget ({kwargs.get('max_nodes')})"
        )

    def test_per_paper_limit_still_forwarded(self):
        import recommend
        store = _tmp_store()
        client = MagicMock()
        oa = MagicMock()
        seed = _make_paper("S1")
        with patch("recommend.expand_network") as mock_expand:
            recommend.expand_seed_network(client, oa, store, [seed], per_paper_limit=42)
        _, kwargs = mock_expand.call_args
        assert kwargs.get("per_paper_limit") == 42


# ── Bug #9 — graph_viz node-score n_seeds excludes "liked" papers ─────────────

class TestNodeScoreCountsLikedAsSeed:
    def _client(self, db):
        import graph_viz
        graph_viz.DB_PATH = db
        graph_viz.app.config["TESTING"] = True
        return graph_viz.app.test_client()

    def test_conn_ratio_clamped_to_one_when_liked_paper_exists(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_make_paper("S", citation_count=0), label="seed")
            store.upsert_paper(_make_paper("L", citation_count=0), label="liked")
            store.upsert_paper(_make_paper("C", citation_count=0, year=2020))
            store.update_distance("C", 1, ["S", "L"])

            client = self._client(db)
            data = client.get("/api/graph").get_json()
            score_by_id = {n["id"]: n["score"] for n in data["nodes"]}

        # cit=0, venue_score(None)=0 -> only the conn term contributes.
        # With the bug (n_seeds counts only "seed"), conn = 2/1 = 2.0 and the
        # score would be ~0.667. Fixed: n_seeds counts seed+liked -> conn=2/2=1.0
        # and the score is exactly one equal-weight share (~0.333).
        assert score_by_id["C"] == pytest.approx(1.0 / 3.0, abs=1e-3)


# ── Bug #6 — liking a paper doesn't refresh the graph view ────────────────────

class TestLikeRefreshesGraphView:
    def _html(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            import graph_viz
            graph_viz.DB_PATH = db
            graph_viz.app.config["TESTING"] = True
            with graph_viz.app.test_client() as client:
                return client.get("/").data.decode()

    def _set_label_function_body(self, html: str) -> str:
        start = html.index("async function setLabel")
        # next top-level function definition after setLabel marks the end
        end = html.index("function ", start + len("async function setLabel"))
        return html[start:end]

    def test_setlabel_refreshes_graph_when_exploration_added_nodes(self):
        body = self._set_label_function_body(self._html())
        assert "data.explored" in body, (
            "setLabel() must inspect the 'explored' field returned when a paper is liked"
        )
        assert "refreshGraph" in body, (
            "setLabel() must call refreshGraph() so nodes added by exploration become visible"
        )
