"""
tests/test_features_v6.py
--------------------------
TDD tests for v6 features:
  1. Removing a node via the GUI also cascade-deletes any of its direct
     neighbors that become isolated (no remaining edges to any other paper
     in the network) as a result -- but never a labeled paper (seed/liked/
     disliked/skipped), which represents an explicit user decision.
  2. "Clear & rebuild" -- wipe papers/edges/metrics (keep seed_titles),
     then re-resolve and re-expand from those stored seed titles.

User journeys:
  1. As a user, when I remove a paper that turns out to be the only thing
     connecting some other candidate to the rest of the network, that
     candidate is removed too, so I don't have to manually clean up
     dangling orphan nodes.
  2. As a user, I can wipe the current network and rebuild it from scratch
     using my original seed titles, without having to re-type them.

Affected APIs:
  - storage.py: Store.delete_paper_cascade(paper_id) -> list[str]
  - storage.py: Store.clear_network() -> None
  - storage.py: Store.get_seed_titles() -> list[dict]
  - graph_viz.py: DELETE /api/paper/<paper_id> now cascades and returns
    {"ok", "paper_id", "deleted": [...]}
  - graph_viz.py: POST /api/graph/clear-and-rebuild (new, async job like
    /api/graph/expand-prune)
  - templates/graph.html: "Clear & Rebuild" button + JS

Callers: pytest only.
User verbatim: "When removing nodes via GUI remove all the nodes which are
only neighbhor of this node and not connected to any other nodes in the
network. also provide option to clear entire network and start build from
scratch with seed papers"
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# Storage layer: cascade delete of orphaned neighbors

class TestDeletePaperCascade:

    def test_cascade_deletes_neighbor_with_no_other_connections(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan1")[0])
            store.add_edges("p1", ["orphan1"])
            store.delete_paper_cascade("p1")
            assert store.get_paper("orphan1") is None

    def test_cascade_keeps_neighbor_connected_to_another_paper(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("p2", "seed")[0], label="seed")
            store.upsert_paper(_p("shared")[0])
            store.add_edges("p1", ["shared"])
            store.add_edges("p2", ["shared"])
            store.delete_paper_cascade("p1")
            assert store.get_paper("shared") is not None, \
                "shared paper is still connected to p2 and must not be deleted"

    def test_cascade_does_not_delete_labeled_orphan(self):
        """A neighbor that would become isolated but carries a user label
        (seed/liked/disliked/skipped) must be preserved."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("liked_orphan")[0], label="liked")
            store.add_edges("p1", ["liked_orphan"])
            store.delete_paper_cascade("p1")
            assert store.get_paper("liked_orphan") is not None

    def test_cascade_returns_all_deleted_ids(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan1")[0])
            store.upsert_paper(_p("orphan2")[0])
            store.add_edges("p1", ["orphan1", "orphan2"])
            deleted = store.delete_paper_cascade("p1")
        assert set(deleted) == {"p1", "orphan1", "orphan2"}

    def test_cascade_handles_multiple_orphans_and_mixed_connectivity(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("p2", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan1")[0])
            store.upsert_paper(_p("shared")[0])
            store.add_edges("p1", ["orphan1", "shared"])
            store.add_edges("p2", ["shared"])
            deleted = store.delete_paper_cascade("p1")
            assert "orphan1" in deleted
            assert "shared" not in deleted
            assert store.get_paper("shared") is not None

    def test_cascade_nonexistent_paper_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            deleted = store.delete_paper_cascade("ghost")
        assert deleted == ["ghost"] or deleted == []

    def test_cascade_removes_entire_disconnected_chain(self):
        """
        Recursive/transitive cascade: S -> A -> B -> C, all unlabeled
        except S. Deleting S must sweep the WHOLE orphaned chain (A, B,
        and C), not just the direct neighbor A -- since none of them are
        reachable from any remaining seed/liked paper.
        """
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            for pid in ("a", "b", "c"):
                store.upsert_paper(_p(pid)[0])
            store.add_edges("s1", ["a"])
            store.add_edges("a", ["b"])
            store.add_edges("b", ["c"])
            deleted = store.delete_paper_cascade("s1")
        assert set(deleted) == {"s1", "a", "b", "c"}

    def test_cascade_keeps_chain_connected_via_another_seed(self):
        """Same chain, but 'c' is also directly cited by a second seed --
        the whole chain must survive since it's still reachable from s2."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            store.upsert_paper(_p("s2", "seed")[0], label="seed")
            for pid in ("a", "b", "c"):
                store.upsert_paper(_p(pid)[0])
            store.add_edges("s1", ["a"])
            store.add_edges("a", ["b"])
            store.add_edges("b", ["c"])
            store.add_edges("s2", ["c"])
            deleted = store.delete_paper_cascade("s1")
            assert deleted == ["s1"]
            for pid in ("a", "b", "c"):
                assert store.get_paper(pid) is not None

    def test_cascade_disliked_paper_preserved_but_does_not_anchor_others(self):
        """A disliked paper is never deleted by the cascade sweep, but it
        does not count as 'still connected to the network' for anything
        that hangs only off it -- only seed/liked anchor reachability."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("s1", "seed")[0], label="seed")
            store.upsert_paper(_p("d1")[0], label="disliked")
            store.upsert_paper(_p("hanger")[0])
            store.add_edges("s1", ["d1"])
            store.add_edges("d1", ["hanger"])
            deleted = store.delete_paper_cascade("s1")
            assert "d1" not in deleted, "disliked paper must be preserved"
            assert "hanger" in deleted, \
                "hanger is only reachable via a disliked paper, which doesn't anchor reachability"
            assert store.get_paper("d1") is not None


class TestDeleteApiCascades:

    def test_delete_api_returns_deleted_list_including_orphans(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan1")[0])
            store.add_edges("p1", ["orphan1"])
            with _app_client(db) as client:
                resp = client.delete("/api/paper/p1")
        data = resp.get_json()
        assert set(data["deleted"]) == {"p1", "orphan1"}

    def test_delete_api_cascade_removes_orphan_from_graph_api(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            store.upsert_paper(_p("p1", "seed")[0], label="seed")
            store.upsert_paper(_p("orphan1")[0])
            store.add_edges("p1", ["orphan1"])
            with _app_client(db) as client:
                client.delete("/api/paper/p1")
                nodes = {n["id"] for n in client.get("/api/graph").get_json()["nodes"]}
        assert "orphan1" not in nodes


# Storage layer: clear network + read seed titles

class TestClearNetworkAndSeedTitles:

    def test_clear_network_removes_all_papers_and_edges(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.upsert_paper(_p("p1")[0])
            store.upsert_paper(_p("p2")[0])
            store.add_edges("p1", ["p2"])
            store.clear_network()
            assert store.all_paper_ids() == set()
            assert store.all_edges() == []

    def test_clear_network_preserves_seed_titles(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.record_seed_title("Attention Is All You Need", "abc123", 1.0)
            store.upsert_paper(_p("p1")[0])
            store.clear_network()
            titles = store.get_seed_titles()
        assert len(titles) == 1
        assert titles[0]["title"] == "Attention Is All You Need"
        assert titles[0]["matched_paper_id"] == "abc123"

    def test_get_seed_titles_returns_stored_rows(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.record_seed_title("Title A", "idA", 1.0)
            store.record_seed_title("Title B", None, 0.0)
            titles = store.get_seed_titles()
        by_title = {t["title"]: t for t in titles}
        assert by_title["Title A"]["matched_paper_id"] == "idA"
        assert by_title["Title B"]["matched_paper_id"] is None


# API: POST /api/graph/clear-and-rebuild
# NOTE: this endpoint is now a pure, synchronous DB wipe (papers, edges,
# metrics, seed_titles) with no seed restoration and no expansion --
# see tests/test_features_v9.py::TestClearIsPureWipe for the current
# contract. Restoring/expanding is the "Prune disliked & expand from
# liked/seed" button's job (it auto-bootstraps from SEED_TITLES_FILE when
# the network is empty -- see tests/test_features_v7.py and
# tests/test_features_v8.py::TestExpandPruneBootstrapUsesFile).

# Frontend: HTML structure

class TestClearRebuildUi:

    def _html(self, db):
        with _app_client(db) as client:
            return client.get("/").data.decode()

    def test_clear_rebuild_button_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "clear-rebuild-btn" in html or "clearAndRebuild" in html, \
            "HTML must contain a clear-and-rebuild trigger"

    def test_clear_and_rebuild_js_function_present(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = self._html(db)
        assert "clearAndRebuild" in html, \
            "HTML must contain JS function clearAndRebuild()"
