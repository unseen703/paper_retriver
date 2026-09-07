"""
tests/test_api_fields_and_batch.py
------------------------------------
TDD tests for:
  #7/#1  Semantic Scholar rejects `authors.hIndex` on the /references and
         /citations endpoints with
         {"error":"Unrecognized or unsupported fields: [authors.hIndex]"}.
         Because _fetch_neighbors swallows the resulting 400, EVERY
         reference/citation fetch silently returned an empty list -- which
         is exactly why "with only two papers I'm not seeing their
         references being added to the network".
  #6     Candidate metadata is hydrated through S2's POST /paper/batch in
         small batches (5 ids), and one failing batch must not abort the
         whole hydration run.

User journeys:
  1. As the system, I request only fields the endpoint actually supports,
     so references and citations really come back.
  2. As the system, I hydrate candidate metadata in batches, and a single
     failing batch degrades gracefully instead of losing everything.

Affected APIs:
  - semantic_scholar_client.py: NEIGHBOR_FIELDS (new), get_references,
    get_citations, batch_get_papers(batch_size=..)
  - graph_expander.py / exploration.py consume the fixed fetches.

Callers: pytest only.
User verbatim: "for many API are not in correct format for calling semantic
scholar API. they are giving this error, {"error":"Unrecognized or
unsupported fields: [authors.hIndex]"}." and "Also fetch items in a batch
of 5 and add batch robustness as well in case one of the batch item gives
error it should not give error."
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from semantic_scholar_client import SemanticScholarClient


# ── #7 — reference/citation endpoints must not request authors.hIndex ────────

class TestNeighborFieldsExcludeUnsupported:

    def test_neighbor_fields_constant_exists(self):
        from semantic_scholar_client import NEIGHBOR_FIELDS
        assert isinstance(NEIGHBOR_FIELDS, str) and NEIGHBOR_FIELDS

    def test_neighbor_fields_omits_authors_hindex(self):
        """/references and /citations reject authors.hIndex outright."""
        from semantic_scholar_client import NEIGHBOR_FIELDS
        assert "authors.hIndex" not in NEIGHBOR_FIELDS

    def test_neighbor_fields_still_has_what_ranking_needs(self):
        from semantic_scholar_client import NEIGHBOR_FIELDS
        for required in ("paperId", "title", "year", "authors",
                         "citationCount", "fieldsOfStudy", "venue"):
            assert required in NEIGHBOR_FIELDS, f"{required} missing from NEIGHBOR_FIELDS"

    def test_get_references_requests_neighbor_fields_not_search_fields(self):
        from semantic_scholar_client import NEIGHBOR_FIELDS
        client = SemanticScholarClient(api_key=None)
        with patch.object(client, "_request", return_value={"data": []}) as req:
            client.get_references("abc123", limit=10)
        sent_fields = req.call_args.kwargs["params"]["fields"]
        assert "authors.hIndex" not in sent_fields
        assert sent_fields == NEIGHBOR_FIELDS

    def test_get_citations_requests_neighbor_fields_not_search_fields(self):
        from semantic_scholar_client import NEIGHBOR_FIELDS
        client = SemanticScholarClient(api_key=None)
        with patch.object(client, "_request", return_value={"data": []}) as req:
            client.get_citations("abc123", limit=10)
        sent_fields = req.call_args.kwargs["params"]["fields"]
        assert "authors.hIndex" not in sent_fields
        assert sent_fields == NEIGHBOR_FIELDS

    def test_search_fields_may_still_use_hindex(self):
        """/paper/search and /paper/search/match DO support authors.hIndex,
        so title resolution can keep asking for it."""
        from semantic_scholar_client import SEARCH_FIELDS
        assert "authors.hIndex" in SEARCH_FIELDS


# ── #6 — batched, fault-tolerant metadata hydration ──────────────────────────

class TestBatchHydrationRobustness:

    def test_batch_get_papers_uses_batch_size_of_five_by_default(self):
        client = SemanticScholarClient(api_key=None)
        ids = [f"p{i}" for i in range(12)]
        with patch.object(client, "_request", return_value=[]) as req:
            client.batch_get_papers(ids)
        # 12 ids at 5 per call -> 3 calls of 5, 5, 2
        chunk_sizes = [len(c.kwargs["json"]["ids"]) for c in req.call_args_list]
        assert chunk_sizes == [5, 5, 2], f"expected batches of 5, got {chunk_sizes}"

    def test_one_failing_batch_does_not_abort_the_rest(self):
        """A 400/500 on one chunk must not lose the other chunks' results."""
        client = SemanticScholarClient(api_key=None)
        ids = [f"p{i}" for i in range(10)]

        calls = {"n": 0}
        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("400 Bad Request on first chunk")
            return [{"paperId": "p5"}, {"paperId": "p6"}]

        with patch.object(client, "_request", side_effect=flaky):
            results = client.batch_get_papers(ids)

        assert calls["n"] == 2, "both chunks must still be attempted"
        assert [r["paperId"] for r in results] == ["p5", "p6"], \
            "surviving chunk's results must be returned despite the earlier failure"

    def test_all_batches_failing_returns_empty_not_raises(self):
        client = SemanticScholarClient(api_key=None)
        with patch.object(client, "_request", side_effect=Exception("network down")):
            results = client.batch_get_papers(["p1", "p2", "p3"])
        assert results == []

    def test_empty_id_list_makes_no_calls(self):
        client = SemanticScholarClient(api_key=None)
        with patch.object(client, "_request") as req:
            assert client.batch_get_papers([]) == []
        req.assert_not_called()
