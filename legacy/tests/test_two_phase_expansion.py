"""
tests/test_two_phase_expansion.py
-----------------------------------
TDD tests for the split expansion pipeline.

  #2  Two distinct expansions:
        expand_references(...)  -- references of seed/liked papers, no cap
        expand_citations(...)   -- citations, hybrid-score ranked, capped
                                   by the GUI's max_papers value
  #4  The citation cap is split EQUALLY across the seed papers
      (cap 3000 / 10 seeds -> 300 each). When a seed has fewer citations
      than its quota, the unused remainder rolls on to the next seed.
  #5  Papers already processed this run are not re-fetched, and papers are
      processed fewest-neighbours-first.
  #1  A randomly-chosen seed (different every run) expands successfully
      under a 50-node threshold.
  #3  Admission goes through is_core_ai_paper, with abstracts hydrated in
      batches so the applied-paper gate has text to work with.

Affected API: expansion.py -- expand_references(), expand_citations(),
order_by_fewest_neighbors().

Callers: pytest only.
User verbatim: "there should be two different expansion first one expanding
the reference of liked/seed papers with no capping. second will be
expanding citations discussed above with page rank and capping provided in
GUI should apply here only." / "while adding citations distribute the
max_capping equaly to all the seed papers ... in case there are not enough
citations available then move onto next seed paper." / "while expanding
keep check of already exsiting papers and avoid cheking them againand
again. and first start with nodes which has least neighbhors." / "add a
test case where you are selecting one random seed paper each time(not
fixed paper) and trying to expand it with 50 node threshold."
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from storage import Paper, Store


# ── helpers ──────────────────────────────────────────────────────────────────

def _rec(pid, citation_count=10, year=2020, venue=None, title=None):
    """A core-AI-looking S2 record that passes the strict filter."""
    return {
        "paperId": pid,
        "title": title or f"Neural Language Model Study {pid}",
        "citationCount": citation_count,
        "externalIds": {}, "year": year, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": venue, "tldr": None, "embedding": None,
        "abstract": "A transformer language model benchmark.",
    }


def _seed(store, pid, label="seed"):
    store.upsert_paper(
        Paper(paper_id=pid, title=f"Neural Language Model Seed {pid}",
              source_api="s2", field_of_study=["Computer Science"]),
        label=label,
    )


def _client(refs_by_pid=None, cites_by_pid=None):
    c = MagicMock()
    c.get_references.side_effect = lambda pid, limit=1000: (refs_by_pid or {}).get(pid, [])
    c.get_citations.side_effect = lambda pid, limit=1000: (cites_by_pid or {}).get(pid, [])
    c.batch_get_papers.side_effect = lambda ids, **kw: [
        {"paperId": i, "abstract": "A transformer language model benchmark."} for i in ids
    ]
    return c


# ── #5 — fewest-neighbours-first ordering ────────────────────────────────────

class TestOrderByFewestNeighbors:

    def test_orders_ascending_by_neighbor_count(self):
        from expansion import order_by_fewest_neighbors
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for pid in ("a", "b", "c", "x", "y", "z"):
                _seed(store, pid)
            # a: 2 neighbours, b: 0, c: 1
            store.add_edges("a", ["x", "y"])
            store.add_edges("c", ["z"])
            ordered = order_by_fewest_neighbors(store, ["a", "b", "c"])
        assert ordered == ["b", "c", "a"]

    def test_unknown_papers_count_as_zero_neighbors(self):
        from expansion import order_by_fewest_neighbors
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "a")
            _seed(store, "x")
            store.add_edges("a", ["x"])
            ordered = order_by_fewest_neighbors(store, ["a", "ghost"])
        assert ordered[0] == "ghost"


# ── #2 — reference expansion is uncapped ─────────────────────────────────────

class TestExpandReferences:

    def test_adds_every_reference_with_no_cap(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            refs = [_rec(f"r{i}") for i in range(25)]
            s2 = _client(refs_by_pid={"s1": refs})
            result = expand_references(s2, MagicMock(), store, ["s1"])
            ids = store.all_paper_ids()
        assert result["added"] == 25
        for i in range(25):
            assert f"r{i}" in ids

    def test_creates_edge_from_paper_to_reference(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            s2 = _client(refs_by_pid={"s1": [_rec("r1")]})
            expand_references(s2, MagicMock(), store, ["s1"])
            assert ("s1", "r1") in store.all_edges()

    def test_rejects_applied_papers(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            good = _rec("keep", title="Transformer Language Model Scaling")
            applied = _rec("drop", title="Deep Learning for Cancer Diagnosis")
            applied["abstract"] = "A clinical oncology study of patients."
            s2 = _client(refs_by_pid={"s1": [good, applied]})
            s2.batch_get_papers.side_effect = lambda ids, **kw: [
                {"paperId": i,
                 "abstract": "A clinical oncology study of patients."
                             if i == "drop" else "A transformer benchmark."}
                for i in ids
            ]
            result = expand_references(s2, MagicMock(), store, ["s1"])
            ids = store.all_paper_ids()
        assert "keep" in ids
        assert "drop" not in ids
        assert result["skipped"] >= 1

    def test_does_not_reprocess_the_same_paper_twice(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            s2 = _client(refs_by_pid={"s1": [_rec("r1")]})
            expand_references(s2, MagicMock(), store, ["s1", "s1", "s1"])
        assert s2.get_references.call_count == 1, \
            "a paper listed more than once must only be fetched once"

    def test_one_failing_paper_does_not_abort_the_run(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            _seed(store, "s2")
            s2 = MagicMock()
            def refs(pid, limit=1000):
                if pid == "s1":
                    raise Exception("500 from S2")
                return [_rec("r_ok")]
            s2.get_references.side_effect = refs
            s2.get_citations.return_value = []
            s2.batch_get_papers.side_effect = lambda ids, **kw: [
                {"paperId": i, "abstract": "A transformer benchmark."} for i in ids
            ]
            expand_references(s2, MagicMock(), store, ["s1", "s2"])
            ids = store.all_paper_ids()
        assert "r_ok" in ids, "s2 must still be expanded after s1 failed"

    def test_hydrates_abstracts_in_batches(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            refs = [_rec(f"r{i}") for i in range(12)]
            s2 = _client(refs_by_pid={"s1": refs})
            expand_references(s2, MagicMock(), store, ["s1"], batch_size=5)
        assert s2.batch_get_papers.called, "abstracts must be hydrated for the filter"


# ── #2/#4 — citation expansion is capped and quota-split ─────────────────────

class TestExpandCitations:

    def test_cap_is_split_equally_across_seeds(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            _seed(store, "s2")
            cites = {
                "s1": [_rec(f"a{i}") for i in range(20)],
                "s2": [_rec(f"b{i}") for i in range(20)],
            }
            s2 = _client(cites_by_pid=cites)
            result = expand_citations(s2, MagicMock(), store, ["s1", "s2"], max_total=10)
            ids = store.all_paper_ids()
        assert result["added"] == 10
        assert len([i for i in ids if i.startswith("a")]) == 5
        assert len([i for i in ids if i.startswith("b")]) == 5

    def test_unused_quota_rolls_over_to_next_seed(self):
        """s1 only has 2 citations though its quota is 5 -- s2 should be
        allowed to use the remaining 8 rather than stopping at 5."""
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            _seed(store, "s2")
            cites = {
                "s1": [_rec(f"a{i}") for i in range(2)],
                "s2": [_rec(f"b{i}") for i in range(20)],
            }
            s2 = _client(cites_by_pid=cites)
            result = expand_citations(s2, MagicMock(), store, ["s1", "s2"], max_total=10)
            ids = store.all_paper_ids()
        assert result["added"] == 10, "leftover quota must roll over, not be lost"
        assert len([i for i in ids if i.startswith("a")]) == 2
        assert len([i for i in ids if i.startswith("b")]) == 8

    def test_never_exceeds_the_total_cap(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for pid in ("s1", "s2", "s3"):
                _seed(store, pid)
            cites = {p: [_rec(f"{p}c{i}") for i in range(50)] for p in ("s1", "s2", "s3")}
            s2 = _client(cites_by_pid=cites)
            result = expand_citations(s2, MagicMock(), store, ["s1", "s2", "s3"], max_total=9)
        assert result["added"] == 9

    def test_higher_ranked_citations_are_taken_first(self):
        """Within a seed's quota, the hybrid score (citation count dominant)
        decides which citing papers make the cut."""
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            cites = [_rec("low", citation_count=1), _rec("high", citation_count=9000)]
            s2 = _client(cites_by_pid={"s1": cites})
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=1)
            ids = store.all_paper_ids()
        assert "high" in ids and "low" not in ids

    def test_creates_edge_from_citer_to_paper(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            s2 = _client(cites_by_pid={"s1": [_rec("c1")]})
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=5)
            assert ("c1", "s1") in store.all_edges()

    def test_zero_cap_adds_nothing(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            s2 = _client(cites_by_pid={"s1": [_rec("c1")]})
            result = expand_citations(s2, MagicMock(), store, ["s1"], max_total=0)
        assert result["added"] == 0


# ── #1 — a randomly chosen seed expands under a 50-node threshold ────────────

class TestRandomSeedExpansion:

    def test_random_seed_gets_its_references_added(self):
        """Picks a DIFFERENT seed each run (not a fixed one) and expands it
        with a 50-node threshold -- references must actually land."""
        from expansion import expand_references
        seed_pool = ["react", "reflexion", "toolace", "deepseek", "attention"]
        chosen = random.choice(seed_pool)

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for pid in seed_pool:
                _seed(store, pid)
            refs = [_rec(f"{chosen}_ref{i}") for i in range(60)]
            s2 = _client(refs_by_pid={chosen: refs})

            result = expand_references(s2, MagicMock(), store, [chosen], max_nodes=50)
            ids = store.all_paper_ids()

        assert result["added"] > 0, f"randomly chosen seed {chosen!r} added no references"
        assert f"{chosen}_ref0" in ids
        assert len(ids) <= 50 + len(seed_pool), \
            "the 50-node threshold should bound how many new papers land"

    def test_random_seed_expansion_is_repeatable_across_seeds(self):
        """Run the same expansion for every seed in the pool -- each one
        must independently succeed, so no seed is silently a dead end."""
        from expansion import expand_references
        seed_pool = ["react", "reflexion", "toolace"]
        for chosen in seed_pool:
            with tempfile.TemporaryDirectory() as d:
                store = Store(Path(d) / "t.db")
                for pid in seed_pool:
                    _seed(store, pid)
                s2 = _client(refs_by_pid={chosen: [_rec(f"{chosen}_r1")]})
                result = expand_references(s2, MagicMock(), store, [chosen], max_nodes=50)
                assert result["added"] == 1, f"seed {chosen!r} failed to expand"
