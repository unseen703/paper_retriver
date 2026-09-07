"""
tests/test_review_findings_v2.py
----------------------------------
Reproducers for the 10 findings from the code review of the two-phase
expansion / strict-filter batch. Each test states the behaviour the code
SHOULD have; they fail against the current implementation.

  F1  field_filter.py:180  bare "law"/"crop" reject core AI papers
  F2  expansion.py:171     expansions never set distance/seed_connections
  F3  expansion.py:174     "added" counts papers that already existed
  F4  expansion.py:250     citation budget consumed by already-known papers
  F5  expansion.py:160     known papers re-filtered w/o abstract, edge dropped
  F6  semantic_scholar_client.py:179  batch default 500->5 slows hydrate_ids
  F7  graph_viz.py:233     reference expansion unbounded (no max_nodes)
  F8  graph_viz.py:209     non-numeric max_papers -> unhandled 500
  F9  templates/graph.html:501  setLabel disliked branch is dead code
  F10 templates/graph.html:723  dislike prompt hides cascade scope

Callers: pytest only.
User verbatim: "build tests for this bugs."
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from storage import Paper, Store


# ── helpers ──────────────────────────────────────────────────────────────────

def _rec(pid, citation_count=10, title=None, venue=None, abstract=None):
    return {
        "paperId": pid,
        "title": title or f"Neural Language Model Study {pid}",
        "citationCount": citation_count,
        "externalIds": {}, "year": 2020, "authors": [],
        "fieldsOfStudy": ["Computer Science"],
        "venue": venue, "tldr": None, "embedding": None,
        "abstract": abstract,
    }


def _seed(store, pid, distance=0, connections=None):
    store.upsert_paper(
        Paper(paper_id=pid, title=f"Neural Language Model Seed {pid}",
              source_api="s2", field_of_study=["Computer Science"]),
        label="seed",
    )
    store.update_distance(pid, distance, connections or [pid])


def _client(refs=None, cites=None):
    c = MagicMock()
    c.get_references.side_effect = lambda pid, limit=1000: (refs or {}).get(pid, [])
    c.get_citations.side_effect = lambda pid, limit=1000: (cites or {}).get(pid, [])
    c.batch_get_papers.side_effect = lambda ids, **kw: [
        {"paperId": i, "abstract": "A transformer language model benchmark."} for i in ids
    ]
    return c


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


# ── F1 — substring collisions between the two vocabularies ───────────────────

class TestF1SubstringCollisions:

    @pytest.mark.parametrize("title,venue", [
        ("Scaling Laws for Neural Language Models", "ArXiv"),
        ("A Law of Large Numbers for Gradient Descent", "ICML"),
        ("Deep Learning with Random Cropping Augmentation", "CVPR"),
    ])
    def test_core_papers_not_rejected_by_substring_of_applied_term(self, title, venue):
        """'law' inside 'Laws' and 'crop' inside 'Cropping' must not
        disqualify core AI work."""
        from field_filter import is_core_ai_paper
        assert is_core_ai_paper(title=title, venue=venue, abstract=None,
                                field_of_study=["Computer Science"]), \
            f"{title!r} wrongly rejected by an applied-term substring match"

    def test_no_applied_term_is_a_substring_of_a_core_term(self):
        """Structural guard: an applied term that occurs inside a core term
        can never be satisfied -- the applied gate always wins."""
        from field_filter import CORE_AI_SUBDOMAINS, APPLIED_DOMAIN_TERMS
        collisions = [
            (a, c) for a in APPLIED_DOMAIN_TERMS
            for c in CORE_AI_SUBDOMAINS
            if a in c
        ]
        assert not collisions, (
            "applied terms swallow core terms, making them unreachable: "
            f"{sorted(collisions)[:5]}"
        )

    def test_genuinely_applied_papers_still_rejected(self):
        """The fix must not weaken the applied gate itself."""
        from field_filter import is_core_ai_paper
        for title in ("Legal Document Retrieval for Court Case Prediction",
                      "Crop Yield Prediction with Deep Learning"):
            assert not is_core_ai_paper(title=title, venue="ArXiv", abstract=None,
                                        field_of_study=["Computer Science"]), \
                f"{title!r} is applied work and must stay rejected"


# ── F2 — distance / seed_connections propagation ─────────────────────────────

class TestF2DistancePropagation:

    def test_expand_references_sets_distance_and_connections(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1", distance=0, connections=["s1"])
            s2 = _client(refs={"s1": [_rec("r1")]})
            expand_references(s2, MagicMock(), store, ["s1"])
            added = store.get_paper("r1")
            assert added.distance_from_seed == 1, \
                "reference added at hop 1 must record distance_from_seed=1"
            assert "s1" in (added.seed_connections or []), \
                "reference must inherit its parent's seed_connections"

    def test_expand_citations_sets_distance_and_connections(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1", distance=0, connections=["s1"])
            s2 = _client(cites={"s1": [_rec("c1")]})
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=5)
            added = store.get_paper("c1")
            assert added.distance_from_seed == 1
            assert "s1" in (added.seed_connections or [])

    def test_node_score_not_penalised_for_expansion_added_papers(self):
        """Without seed_connections the conn term of _node_score is 0, so an
        expansion-added paper scores a third lower than the same paper added
        by the BFS."""
        import graph_viz
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            _seed(store, "s1", distance=0, connections=["s1"])
            s2 = _client(refs={"s1": [_rec("r1", citation_count=0)]})
            expand_references(s2, MagicMock(), store, ["s1"])
            paper = store.get_paper("r1")
            score = graph_viz._node_score(paper, 1)
            assert score > 0.0, \
                "expansion-added paper scores 0 because seed_connections is empty"


# ── F3 — "added" must count only genuinely new papers ────────────────────────

class TestF3AddedCountAccuracy:

    def test_shared_references_are_not_double_counted(self):
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            _seed(store, "s2")
            shared = [_rec("r1"), _rec("r2"), _rec("r3")]
            s2 = _client(refs={"s1": shared, "s2": list(shared)})
            before = len(store.all_paper_ids())
            result = expand_references(s2, MagicMock(), store, ["s1", "s2"])
            grew_by = len(store.all_paper_ids()) - before
            assert result["added"] == grew_by, (
                f"reported added={result['added']} but the network only grew by "
                f"{grew_by} papers"
            )

    def test_citation_added_count_matches_growth(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            _seed(store, "s2")
            shared = [_rec("c1"), _rec("c2")]
            s2 = _client(cites={"s1": shared, "s2": list(shared)})
            before = len(store.all_paper_ids())
            result = expand_citations(s2, MagicMock(), store, ["s1", "s2"], max_total=10)
            grew_by = len(store.all_paper_ids()) - before
            assert result["added"] == grew_by


# ── F4 — budget must buy NEW papers, not re-buy known ones ───────────────────

class TestF4BudgetNotBurnedOnKnownPapers:

    def test_quota_reaches_past_already_known_citations(self):
        """The 3 highest-ranked citing papers are already in the network.
        With a budget of 3 the expansion should still bring in 3 NEW papers
        rather than spending the whole budget re-touching known ones."""
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            for pid in ("known1", "known2", "known3"):
                store.upsert_paper(Paper(paper_id=pid, title=f"Neural Model {pid}",
                                         source_api="s2",
                                         field_of_study=["Computer Science"]))
            cites = [
                _rec("known1", citation_count=9000),
                _rec("known2", citation_count=8000),
                _rec("known3", citation_count=7000),
                _rec("new1", citation_count=600),
                _rec("new2", citation_count=500),
                _rec("new3", citation_count=400),
            ]
            s2 = _client(cites={"s1": cites})
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=3)
            ids = store.all_paper_ids()
            newly = [p for p in ("new1", "new2", "new3") if p in ids]
            assert len(newly) == 3, (
                "budget was consumed by papers already in the network; "
                f"only {newly} of the new citing papers were added"
            )

    def test_repeat_run_still_makes_progress(self):
        from expansion import expand_citations
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            cites = [_rec(f"c{i}", citation_count=1000 - i) for i in range(10)]
            s2 = _client(cites={"s1": cites})
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=3)
            after_first = len(store.all_paper_ids())
            expand_citations(s2, MagicMock(), store, ["s1"], max_total=3)
            after_second = len(store.all_paper_ids())
            assert after_second > after_first, (
                "a second run added nothing -- the budget was spent re-taking "
                "papers already in the network instead of reaching new ones"
            )


# ── F5 — edges to already-admitted papers must not be dropped ────────────────

class TestF5EdgesToKnownPapersPreserved:

    def test_edge_recorded_even_when_known_paper_fails_title_only_filter(self):
        """A paper already in the network was admitted once (its abstract
        carried the signal). Re-encountering it must not drop the edge just
        because we no longer hydrate its abstract."""
        from expansion import expand_references
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            _seed(store, "s1")
            store.upsert_paper(Paper(paper_id="x1", title="Scaling Up: A Study",
                                     venue="ArXiv", source_api="s2",
                                     field_of_study=["Computer Science"]))
            s2 = _client(refs={"s1": [_rec("x1", title="Scaling Up: A Study",
                                           venue="ArXiv")]})
            expand_references(s2, MagicMock(), store, ["s1"])
            edges = store.all_edges()
            assert ("s1", "x1") in edges, (
                "citation edge to an existing network member was dropped because "
                "the filter re-ran without the abstract"
            )


# ── F6 — hydrate_ids must not inherit the batch-of-5 default ─────────────────

class TestF6HydrateIdsBatchSize:

    def test_hydrate_ids_uses_large_batches(self):
        """hydrate_ids bulk-hydrates the whole network; at 5 ids per call a
        1500-paper network costs 300 rate-limited requests."""
        from graph_expander import hydrate_ids
        from semantic_scholar_client import SemanticScholarClient
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            client = SemanticScholarClient(api_key=None)
            ids = [f"p{i}" for i in range(100)]
            with patch.object(client, "_request", return_value=[]) as req:
                hydrate_ids(ids, client, store)
            assert req.call_count <= 5, (
                f"hydrate_ids issued {req.call_count} batch requests for 100 ids; "
                "it should use large batches, not the expansion path's batch of 5"
            )


# ── F7 — reference expansion needs a ceiling ─────────────────────────────────

class TestF7ReferenceExpansionHasCeiling:

    def test_endpoint_passes_a_max_nodes_ceiling(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            _seed(store, "s1")
            import graph_viz
            with patch("graph_viz.threading.Thread", _SyncThread), \
                 patch("graph_viz.expand_references",
                       return_value={"added": 0, "skipped": 0}) as mock_refs:
                with _app_client(db) as c:
                    c.post("/api/graph/expand-references", json={})
            ceiling = mock_refs.call_args.kwargs.get("max_nodes")
            assert ceiling is not None and ceiling > 0, (
                "expand-references runs with no max_nodes ceiling: one click can "
                "pull tens of thousands of candidates with no way to stop it"
            )


# ── F8 — malformed max_papers must not 500 ───────────────────────────────────

class TestF8MalformedMaxPapers:

    @pytest.mark.parametrize("bad", [None, "abc", {}])
    def test_non_numeric_max_papers_returns_400(self, bad):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            store = Store(db)
            _seed(store, "s1")
            with _app_client(db) as c:
                try:
                    resp = c.post("/api/graph/expand-citations",
                                  json={"max_papers": bad})
                except Exception as exc:
                    pytest.fail(
                        f"max_papers={bad!r} raised {type(exc).__name__} out of the "
                        "view instead of returning a 400"
                    )
                assert resp.status_code == 400, \
                    f"max_papers={bad!r} should be rejected with 400, got {resp.status_code}"


# ── F9 / F10 — front-end leftovers ───────────────────────────────────────────

class TestF9DeadDislikeLabelCode:

    def test_setlabel_no_longer_probes_the_dislike_button(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        start = html.index("async function setLabel")
        nxt = html.find("function ", start + len("async function setLabel"))
        body = html[start:nxt] if nxt != -1 else html[start:]
        assert "dislike-btn" not in body, (
            "setLabel still branches on .dislike-btn active state, but the "
            "dislike button now calls dislikeAndRemove and never carries it"
        )

    def test_no_dead_dislike_active_css_rule(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        assert ".dislike-btn.active" not in html, \
            "dead CSS rule for a state the dislike button can no longer enter"


class TestF10DislikePromptDisclosesCascade:

    def test_dislike_confirm_mentions_connected_papers(self):
        """Dislike triggers a reachability cascade that can delete many more
        nodes; the prompt must say so before the user accepts."""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            Store(db)
            html = _html(db)
        # Inspect only the confirm() prompt itself -- the after-the-fact toast
        # already mentions "orphaned", which would make this pass vacuously.
        rp = html.index("function removePaper")
        rp_end = html.find("function ", rp + len("function removePaper"))
        body = html[rp:rp_end] if rp_end != -1 else html[rp:]
        c_start = body.index("confirm(")
        c_end = body.index(")", c_start)
        prompt = body[c_start:c_end].lower()
        assert any(word in prompt for word in
                   ("orphan", "connected", "cascade", "other papers")), (
            f"the removal confirmation {prompt!r} does not warn that papers "
            "left disconnected will also be deleted"
        )
