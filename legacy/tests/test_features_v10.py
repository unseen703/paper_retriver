"""
tests/test_features_v10.py
----------------------------
TDD reproducer + fix for a real bug: many seed papers end up as isolated
nodes with zero edges in the graph.

Root cause: expand_network()'s outer per-dequeue cap check used `break`
(exiting the ENTIRE while-queue loop) whenever max_nodes or max_new was
hit. Since all seed_ids are enqueued first (FIFO), if an EARLY seed's own
uncapped references pushed the count past the ceiling, every seed still
waiting later in the queue was abandoned -- never dequeued, so
_fetch_neighbors was never called for it, so it never got any edges at
all, despite being labeled "seed" and supposed to have ALL its references
added unconditionally.

Fix: the outer check now skips (continue) a single non-bypass candidate
instead of aborting the whole loop, and is bypass-aware so a seed/liked
paper is never skipped. The inner mid-wave check is also updated so a
bypass paper's own references are exempt from BOTH max_new and max_nodes
(previously only max_new), consistent with "all references are always
added" being an unconditional guarantee for seed/liked papers.

User verbatim: "How is the reference are added to current implementation?
I see many seed paper linked without any links at all."
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import Paper, Store
from graph_expander import expand_network


def _s2_record(pid, citation_count=10, year=2020):
    return {
        "paperId": pid, "title": pid, "citationCount": citation_count,
        "externalIds": {}, "year": year, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": None, "tldr": None, "embedding": None,
    }


def _make_seed(store, pid):
    store.upsert_paper(
        Paper(paper_id=pid, title=pid, source_api="s2",
              field_of_study=["Computer Science"]),
        label="seed",
    )


class TestNoSeedLeftBehindByHardCeiling:

    def test_later_seed_still_gets_all_references_when_max_nodes_is_tiny(self):
        """The core bug: with a tiny max_nodes, s1's own references alone
        exceed the budget. s2, queued right after s1, must still get ALL
        of its own references fetched and added -- not zero."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _make_seed(store, "s1")
            _make_seed(store, "s2")

            s1_refs = [_s2_record(f"s1ref{i}") for i in range(5)]
            s2_refs = [_s2_record(f"s2ref{i}") for i in range(5)]

            s2_client = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return s1_refs
                if pid == "s2":
                    return s2_refs
                return []
            s2_client.get_references.side_effect = refs_side_effect
            s2_client.get_citations.return_value = []

            # Deliberately tiny ceiling: 2 seeds already exceed it before
            # either one's references are even considered.
            expand_network(s2_client, MagicMock(), store, ["s1", "s2"],
                           hops=1, max_new=1, max_nodes=2)

            all_ids = store.all_paper_ids()

        for i in range(5):
            assert f"s1ref{i}" in all_ids, f"s1ref{i} missing"
            assert f"s2ref{i}" in all_ids, (
                f"s2ref{i} missing -- s2 must not be abandoned just because "
                "max_nodes was already exceeded by s1's references"
            )

    def test_three_seeds_all_get_references_regardless_of_order(self):
        """Regression guard: no matter how many seeds precede it in the
        queue, every seed's own references must always come through."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for sid in ("s1", "s2", "s3"):
                _make_seed(store, sid)

            refs_by_seed = {
                "s1": [_s2_record("s1ref0")],
                "s2": [_s2_record("s2ref0")],
                "s3": [_s2_record("s3ref0")],
            }
            s2_client = MagicMock()
            s2_client.get_references.side_effect = lambda pid, limit=500: refs_by_seed.get(pid, [])
            s2_client.get_citations.return_value = []

            expand_network(s2_client, MagicMock(), store, ["s1", "s2", "s3"],
                           hops=1, max_new=0, max_nodes=1)

            all_ids = store.all_paper_ids()

        assert {"s1ref0", "s2ref0", "s3ref0"} <= all_ids

    def test_non_seed_candidates_are_skipped_not_loop_aborting(self):
        """Once the cap is hit, non-seed candidates must be individually
        skipped (loop keeps running) -- not cause the whole BFS to stop."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _make_seed(store, "s1")
            _make_seed(store, "s2")

            s2_client = MagicMock()
            def refs_side_effect(pid, limit=500):
                if pid == "s1":
                    return [_s2_record("mid1")]  # a non-seed candidate
                if pid == "s2":
                    return [_s2_record("s2ref0")]
                if pid == "mid1":
                    return [_s2_record("deep1")]
                return []
            s2_client.get_references.side_effect = refs_side_effect
            s2_client.get_citations.return_value = []

            expand_network(s2_client, MagicMock(), store, ["s1", "s2"],
                           hops=2, max_new=1, max_nodes=10000)

            all_ids = store.all_paper_ids()

        # s1 and s2 are seeds -> both their direct refs must be present.
        assert "mid1" in all_ids
        assert "s2ref0" in all_ids
        # max_new=1 caps mid1's OWN (non-seed) reference expansion.
        assert "deep1" not in all_ids

    def test_seed_citations_still_capped_after_fix(self):
        """Regression: the fix must not accidentally exempt a seed's
        CITATIONS from capping -- only its references are unconditional."""
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _make_seed(store, "s1")
            cites = [_s2_record(f"cite{i}") for i in range(5)]
            s2_client = MagicMock()
            s2_client.get_references.return_value = []
            s2_client.get_citations.return_value = cites

            added = expand_network(s2_client, MagicMock(), store, ["s1"],
                                   hops=1, max_new=1, max_nodes=10000)
        assert added == 1, f"seed citations must still respect max_new; added {added}"
