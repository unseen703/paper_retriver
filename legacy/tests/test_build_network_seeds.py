"""
tests/test_build_network_seeds.py
-----------------------------------
TDD test for build_network.py picking up existing liked/GUI-added papers
as additional BFS starting points on a re-run, so they get the same
uncapped-reference exploration as freshly-resolved --seeds.

User journey: As a user, when I re-run build_network.py after liking papers
or adding papers via the GUI in between runs, those papers should also be
treated as expansion starting points (not just the literal --seeds/--seeds-file
titles), so their references get added in full like any other seed.

Affected API: build_network.merge_seed_ids_with_labeled_papers(seed_ids, store)

Callers: pytest only.
User verbatim: "papers liked and papers added via GUI should also get the
similar treatment as seed paper."
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from storage import Paper, Store


def _p(pid, label=None):
    return Paper(paper_id=pid, title=pid, source_api="s2",
                field_of_study=["Computer Science"]), label


class TestMergeSeedIdsWithLabeledPapers:

    def test_merges_existing_liked_papers_not_in_original_seed_ids(self):
        from build_network import merge_seed_ids_with_labeled_papers
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            paper, label = _p("liked1", "liked")
            store.upsert_paper(paper, label=label)
            merged = merge_seed_ids_with_labeled_papers(["s1"], store)
        assert "liked1" in merged
        assert "s1" in merged

    def test_merges_existing_gui_added_seed_labeled_papers(self):
        from build_network import merge_seed_ids_with_labeled_papers
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            paper, label = _p("guiadded1", "seed")
            store.upsert_paper(paper, label=label)
            merged = merge_seed_ids_with_labeled_papers(["s1"], store)
        assert "guiadded1" in merged

    def test_does_not_duplicate_a_paper_already_in_seed_ids(self):
        from build_network import merge_seed_ids_with_labeled_papers
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            paper, label = _p("s1", "seed")
            store.upsert_paper(paper, label=label)
            merged = merge_seed_ids_with_labeled_papers(["s1"], store)
        assert merged.count("s1") == 1

    def test_ignores_disliked_and_skipped_and_unlabeled(self):
        from build_network import merge_seed_ids_with_labeled_papers
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for pid, label in [("d1", "disliked"), ("sk1", "skipped"), ("u1", None)]:
                paper, lbl = _p(pid, label)
                store.upsert_paper(paper, label=lbl)
            merged = merge_seed_ids_with_labeled_papers(["s1"], store)
        assert "d1" not in merged
        assert "sk1" not in merged
        assert "u1" not in merged

    def test_empty_original_seed_ids_still_picks_up_labeled_papers(self):
        from build_network import merge_seed_ids_with_labeled_papers
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            paper, label = _p("liked1", "liked")
            store.upsert_paper(paper, label=label)
            merged = merge_seed_ids_with_labeled_papers([], store)
        assert merged == ["liked1"]
