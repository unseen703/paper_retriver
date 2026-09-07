"""
tests/test_gui_two_buttons.py
-------------------------------
TDD tests for the GUI half of this batch.

  #2   Two buttons -- "Expand references" and "Expand citations" -- backed
       by two endpoints. The max-papers budget applies to the CITATIONS
       button only; references are uncapped.
  #8   Dislike behaves like Remove: it deletes the node (and cascades to
       anything left orphaned) instead of just re-labelling it.
  #9   The Seeds & Liked list lives in the side panel and does NOT vanish
       when a node is removed -- it refreshes in place and stays open
       until the user presses the Seeds & Liked button again.
  #10  Selecting a paper from the Seeds & Liked list or from a neighbour
       list makes it the currently selected node.

Affected APIs:
  - graph_viz.py: POST /api/graph/expand-references,
                  POST /api/graph/expand-citations
  - templates/graph.html: expand-refs-btn / expand-cites-btn,
                          refreshSeedsLiked(), selectNodeById()

Callers: pytest only.
User verbatim: "there should be two different expansion ... capping
provided in GUI should apply here only." / "dislike should also behave
like remove button and should deleted nodes in cascade fashion." / "the
seeds and label button should be in side panel which is easily accessible
and it should not vanish after each time we remove a node, list will
remain till user press the seed& liked button again." / "when we select
paper in list either in seed&likes or other papers neighbhour list, it
should change to current selected node."
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from storage import Paper, Store


def _p(pid, label=None):
    return Paper(paper_id=pid, title=f"Neural Language Model {pid}",
                 source_api="s2", field_of_study=["Computer Science"]), label


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


def _html(db):
    with _app_client(db) as c:
        return c.get("/").data.decode()


# ── #2 — two endpoints ───────────────────────────────────────────────────────

class TestTwoExpansionEndpoints:

    def test_expand_references_endpoint_runs_reference_expansion_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_references",
                       return_value={"added": 7, "skipped": 0}) as mock_refs, \
                 patch("graph_viz.expand_citations") as mock_cites:
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-references", json={})
                    data = resp.get_json()
                    job = c.get(f"/api/expand/status/{data['job_id']}").get_json()
        assert resp.status_code == 200
        assert job["status"] == "done"
        mock_refs.assert_called_once()
        mock_cites.assert_not_called(), "the references button must not expand citations"

    def test_expand_references_ignores_max_papers_cap(self):
        """The cap belongs to the citations phase only."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_references",
                       return_value={"added": 1, "skipped": 0}) as mock_refs:
                with _app_client(db) as c:
                    c.post("/api/graph/expand-references", json={"max_papers": 13})
        kwargs = mock_refs.call_args.kwargs
        assert "max_total" not in kwargs
        assert kwargs.get("max_nodes") != 13, \
            "max_papers must not become a reference-phase cap"

    def test_expand_citations_endpoint_passes_gui_cap_as_max_total(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_citations",
                       return_value={"added": 4, "skipped": 0}) as mock_cites, \
                 patch("graph_viz.expand_references") as mock_refs:
                with _app_client(db) as c:
                    resp = c.post("/api/graph/expand-citations", json={"max_papers": 300})
                    data = resp.get_json()
                    job = c.get(f"/api/expand/status/{data['job_id']}").get_json()
        assert job["status"] == "done"
        assert mock_cites.call_args.kwargs["max_total"] == 300
        mock_refs.assert_not_called(), "the citations button must not expand references"

    def test_both_endpoints_report_no_seeds_when_none_exist(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            import graph_viz
            with patch.object(graph_viz, "SEED_TITLES_FILE", Path(d) / "none.txt"):
                with _app_client(db) as c:
                    r1 = c.post("/api/graph/expand-references", json={}).get_json()
                    r2 = c.post("/api/graph/expand-citations", json={}).get_json()
        assert r1["status"] == "done" and r1["added"] == 0
        assert r2["status"] == "done" and r2["added"] == 0


# ── #2 — two buttons in the UI ───────────────────────────────────────────────

class TestTwoExpansionButtons:

    def test_both_buttons_present_with_requested_labels(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "expand-refs-btn" in html
        assert "expand-cites-btn" in html
        assert "Expand references" in html
        assert "Expand citations" in html

    def test_button_labels_avoid_capped_uncapped_wording(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "uncapped" not in html.lower()

    def test_js_functions_for_both_expansions_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "expandReferences" in html
        assert "expandCitations" in html


# ── #8 — dislike removes (with cascade) ──────────────────────────────────────

class TestDislikeRemovesNode:

    def test_dislike_handler_deletes_instead_of_labelling(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("function dislikeAndRemove")
        end = html.index("function ", start + len("function dislikeAndRemove"))
        body = html[start:end]
        assert "removePaper" in body or "method:\"DELETE\"" in body, \
            "dislike must go through the cascading delete path"

    def test_dislike_button_wired_to_remove_handler(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "dislikeAndRemove" in html

    def test_delete_endpoint_still_cascades(self):
        """Regression: the cascade behaviour dislike now relies on."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan")[0])
            store.add_edges("s1", ["orphan"])
            with _app_client(db) as c:
                data = c.delete("/api/paper/s1").get_json()
        assert set(data["deleted"]) == {"s1", "orphan"}


# ── #9 — the Seeds & Liked list persists across removals ─────────────────────

class TestSeedsLikedPanelPersists:

    def test_refresh_function_exists_separate_from_toggle(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "refreshSeedsLiked" in html, \
            "a non-toggling refresh is needed so the list survives a removal"

    def test_remove_refreshes_rather_than_toggles_the_panel(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("function removePaper")
        end = html.index("function ", start + len("function removePaper"))
        body = html[start:end]
        assert "refreshSeedsLiked" in body, \
            "removePaper must refresh the list in place"
        assert "loadSeedsLiked(" not in body, \
            "removePaper must not call the toggle, which would close the panel"

    def test_toggle_button_still_exists_for_manual_open_close(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert "loadSeedsLiked" in html
        assert "seeds-liked-panel" in html


# ── #10 — selecting from a list changes the current node ─────────────────────

class TestListSelectionChangesCurrentNode:

    def test_select_node_by_id_updates_selection_and_detail(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("function selectNodeById")
        nxt = html.find("function ", start + len("function selectNodeById"))
        body = html[start:nxt] if nxt != -1 else html[start:]
        assert "loadDetail" in body, "selection must populate the detail panel"
        assert "highlightNeighborhood" in body or "_selectedId" in body, \
            "selection must update the graph's current node, not just the sidebar"

    def test_seeds_liked_rows_call_select_node_by_id(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("function renderSeedsLiked")
        end = html.index("function ", start + len("function renderSeedsLiked"))
        body = html[start:end]
        assert "selectNodeById" in body

    def test_neighbor_rows_call_select_node_by_id(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("function loadNeighborSidebar")
        end = html.index("function ", start + len("function loadNeighborSidebar"))
        body = html[start:end]
        assert "selectNodeById" in body, \
            "clicking a neighbour must switch the current node too"
