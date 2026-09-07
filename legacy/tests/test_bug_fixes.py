"""
tests/test_bug_fixes.py
-----------------------
TDD reproducers for the 10 bugs surfaced by the independent code review.
Each test is written to FAIL against the unpatched code and PASS after the fix.

Bug list (matches ReportFindings order):
 #1  resolver / graph_expander : OA W-IDs passed to S2 API
 #2  storage : duplicate rows on DOI collision (S2 id vs OA W-id)
 #3  storage : seed_connections silently lost at equal hop distance
 #4  storage : papers_with_label([]) -> invalid SQL IN ()
 #5  recommend : unguarded get_paper() crashes seed resolution loop
 #6  openalex_client : lstrip() strips char-set not prefix
 #7  semantic_scholar_client : bare except Exception: pass hides API key errors
 #8  graph_expander : store.all_paper_ids() called every BFS iteration
 #9  semantic_scholar_client : title search results never cached
 #10 citation_network : co_cit first term duplicates in_deg formula
"""

import json
import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage import Paper, Store
from openalex_client import OpenAlexClient
from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs


# ── helpers ───────────────────────────────────────────────────────────────────

def _tmp_store() -> Store:
    td = tempfile.mkdtemp()
    return Store(Path(td) / "test.sqlite3")


def _make_paper(pid, title="T", year=2023, citation_count=10, doi=None):
    return Paper(paper_id=pid, title=title, year=year, citation_count=citation_count, doi=doi)


# ── Bug #4 — papers_with_label([]) SQL crash ──────────────────────────────────

class TestPapersWithLabelEmptyList:
    def test_empty_list_returns_empty(self):
        store = _tmp_store()
        store.upsert_paper(_make_paper("P1"), label="seed")
        result = store.papers_with_label([])
        assert result == []

    def test_single_label_still_works(self):
        store = _tmp_store()
        store.upsert_paper(_make_paper("P1"), label="seed")
        assert len(store.papers_with_label(["seed"])) == 1


# ── Bug #6 — lstrip() DOI stripping ──────────────────────────────────────────

class TestOpenAlexDoiNormalization:
    def _captured_url(self, raw_doi: str) -> str:
        client = OpenAlexClient()
        captured = {}
        def fake_request(url, params=None, cache_key=None):
            captured["url"] = url
            return None
        client._request = fake_request
        client.get_work_by_doi(raw_doi)
        return captured.get("url", "")

    def test_https_prefix_stripped(self):
        url = self._captured_url("https://doi.org/10.1234/abc")
        assert "10.1234/abc" in url

    def test_http_prefix_stripped(self):
        url = self._captured_url("http://doi.org/10.5555/xyz")
        assert "10.5555/xyz" in url

    def test_bare_doi_unchanged(self):
        url = self._captured_url("10.9999/bare")
        assert "10.9999/bare" in url

    def test_no_char_mangling(self):
        # lstrip would eat leading 'h' from "10.h.test" if path started with a stripped char.
        # We verify the clean variable never contains residual prefix chars before "10."
        url = self._captured_url("https://doi.org/10.1/test")
        # The URL must contain exactly "10.1/test", not "0.1/test" or other mangled form
        assert "10.1/test" in url
        assert url.count("10.1/test") >= 1


# ── Bug #3 — seed_connections lost at equal hop distance ─────────────────────

class TestUpdateDistanceMergesSeedConnections:
    def test_second_seed_merged_at_equal_distance(self):
        store = _tmp_store()
        store.upsert_paper(_make_paper("TARGET"))
        store.update_distance("TARGET", 2, ["SEED_A"])
        store.update_distance("TARGET", 2, ["SEED_B"])
        paper = store.get_paper("TARGET")
        conns = paper.seed_connections
        assert "SEED_A" in conns, f"SEED_A missing from {conns}"
        assert "SEED_B" in conns, f"SEED_B missing from {conns}"

    def test_closer_distance_replaces(self):
        store = _tmp_store()
        store.upsert_paper(_make_paper("TARGET"))
        store.update_distance("TARGET", 3, ["SEED_A"])
        store.update_distance("TARGET", 1, ["SEED_B"])
        paper = store.get_paper("TARGET")
        assert paper.distance_from_seed == 1

    def test_farther_distance_ignored(self):
        store = _tmp_store()
        store.upsert_paper(_make_paper("TARGET"))
        store.update_distance("TARGET", 1, ["SEED_A"])
        store.update_distance("TARGET", 3, ["SEED_B"])
        paper = store.get_paper("TARGET")
        assert paper.distance_from_seed == 1


# ── Bug #2 — duplicate rows on DOI collision ─────────────────────────────────

class TestUpsertPaperDoiDeduplication:
    def test_same_doi_different_ids_deduped(self):
        store = _tmp_store()
        p_s2 = Paper(paper_id="s2hex123", title="My Paper", doi="10.1234/test",
                     citation_count=5)
        p_oa = Paper(paper_id="W9999999", title="My Paper", doi="10.1234/test",
                     citation_count=5, source_api="openalex")
        store.upsert_paper(p_s2)
        store.upsert_paper(p_oa)
        doi_papers = [
            store.get_paper(pid) for pid in store.all_paper_ids()
            if (p := store.get_paper(pid)) and p.doi == "10.1234/test"
        ]
        doi_papers = [p for p in doi_papers if p is not None]
        assert len(doi_papers) == 1, (
            f"Expected 1 row for DOI, got {len(doi_papers)}: "
            f"{[p.paper_id for p in doi_papers]}"
        )


# ── Bug #10 — co_cit duplicates in_deg formula ───────────────────────────────

class TestComputeMetricsCoCitation:
    def test_co_citation_exceeds_in_degree_when_candidate_cites_seed(self):
        from citation_network import build_graph, compute_metrics

        store = _tmp_store()
        store.upsert_paper(_make_paper("SEED"), label="seed")
        store.upsert_paper(_make_paper("CAND"))
        # CAND cites SEED, but SEED does NOT cite CAND
        store.add_edges("CAND", ["SEED"])

        graph = build_graph(store)
        compute_metrics(store, graph, ["SEED"])

        m = store.get_metrics("CAND")
        assert m["in_degree"] == 0, f"Expected in_degree=0, got {m['in_degree']}"
        assert m["co_citation"] == 1, f"Expected co_citation=1, got {m['co_citation']}"

    def test_both_directions_count(self):
        from citation_network import build_graph, compute_metrics

        store = _tmp_store()
        store.upsert_paper(_make_paper("SEED"), label="seed")
        store.upsert_paper(_make_paper("CAND"))
        store.add_edges("SEED", ["CAND"])  # SEED cites CAND
        store.add_edges("CAND", ["SEED"])  # CAND cites SEED

        graph = build_graph(store)
        compute_metrics(store, graph, ["SEED"])

        m = store.get_metrics("CAND")
        assert m["in_degree"] == 1
        assert m["co_citation"] == 2  # in_deg(1) + cites_seeds(1)


# ── Bug #7 — bare except: pass hides API key errors ──────────────────────────

class TestS2SearchLogsOnFailure:
    def test_match_failure_is_logged(self, caplog):
        client = SemanticScholarClient(api_key=None)
        bad_resp = MagicMock()
        bad_resp.status_code = 403
        bad_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"data": []}
        ok_resp.raise_for_status.return_value = None

        with patch.object(client.session, "request", side_effect=[bad_resp, ok_resp]):
            with patch("time.sleep"):
                with caplog.at_level(logging.WARNING):
                    client.search_paper_by_title("some title")

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_msgs, (
            f"Expected at least one WARNING log for /match failure, got nothing. "
            f"All records: {caplog.records}"
        )


# ── Bug #5 — recommend.py unguarded get_paper() ──────────────────────────────

class TestRecommendResolveSeedTitlesHandlesNetworkError:
    def test_network_error_on_get_paper_skips_seed(self, capsys):
        from recommend import resolve_seed_titles

        store = _tmp_store()
        client = MagicMock()
        client.search_paper_by_title.return_value = {
            "paperId": "P1", "title": "Paper One", "year": 2020,
            "authors": [], "citationCount": 5, "externalIds": {},
        }
        client.get_paper.side_effect = Exception("Connection timeout")

        # Before fix: exception propagates; after fix: skipped gracefully
        result = resolve_seed_titles(client, store, ["Paper One"])
        assert isinstance(result, list)

    def test_second_seed_resolved_after_first_fails(self, capsys):
        from recommend import resolve_seed_titles

        store = _tmp_store()
        client = MagicMock()

        def fake_search(title):
            return {"paperId": title.replace(" ", "_"), "title": title,
                    "year": 2020, "authors": [], "citationCount": 0, "externalIds": {}}

        def fake_get(pid):
            if pid == "Paper_One":
                raise Exception("timeout")
            return {"paperId": pid, "title": pid, "year": 2020, "abstract": "x",
                    "authors": [], "citationCount": 0, "externalIds": {},
                    "venue": None, "tldr": None, "embedding": None, "fieldsOfStudy": []}

        client.search_paper_by_title.side_effect = fake_search
        client.get_paper.side_effect = fake_get

        result = resolve_seed_titles(client, store, ["Paper One", "Paper Two"])
        resolved_ids = {p.paper_id for p in result}
        assert "Paper_Two" in resolved_ids, (
            f"Paper Two must be resolved even after Paper One fails. Got: {resolved_ids}"
        )


# ── Bug #1 — OA W-IDs passed to S2 API ──────────────────────────────────────

class TestGraphExpanderHandlesOAIds:
    def test_oa_wid_does_not_raise(self):
        from graph_expander import _fetch_neighbors
        import requests as req_lib

        store = _tmp_store()
        oa_paper = Paper(paper_id="W2741809807", title="OA Paper",
                         doi="10.9999/oa", source_api="openalex")
        store.upsert_paper(oa_paper)

        s2 = MagicMock()
        s2.get_references.side_effect = req_lib.exceptions.HTTPError("404 Not Found")
        s2.get_citations.side_effect = req_lib.exceptions.HTTPError("404 Not Found")

        oa = MagicMock()
        oa.get_work_by_doi.return_value = None

        # Before fix: HTTPError propagates; after fix: caught and returns ([], [])
        refs, cites, hindex_by_id = _fetch_neighbors("W2741809807", s2, oa, store, limit=10)
        assert refs == []
        assert cites == []


# ── Bug #9 — title search results never cached ───────────────────────────────

class TestS2TitleSearchCached:
    def test_cache_key_passed_for_title_search(self):
        from api_cache import ApiCache

        td = tempfile.mkdtemp()
        cache = ApiCache(cache_dir=td)
        client = SemanticScholarClient(api_key=None, cache=cache)

        captured_keys = []

        def spy_request(method, url, cache_key=None, **kwargs):
            captured_keys.append(cache_key)
            return {"data": []}

        client._request = spy_request
        with patch("time.sleep"):
            client.search_paper_by_title("Attention is All You Need")

        assert any(k is not None for k in captured_keys), (
            f"Expected non-None cache_key for title search, got all None: {captured_keys}"
        )


# ── Bug #8 — all_paper_ids() called per BFS iteration ────────────────────────

class TestGraphExpanderNodeCapUsesCounter:
    def test_all_paper_ids_called_at_most_twice(self):
        from graph_expander import expand_network

        store = _tmp_store()
        seed = _make_paper("SEED1")
        store.upsert_paper(seed, label="seed")
        store.update_distance("SEED1", 0, ["SEED1"])

        s2 = MagicMock()
        s2.get_references.return_value = []
        s2.get_citations.return_value = []
        oa = MagicMock()
        oa.get_work_by_doi.return_value = None

        call_count = {"n": 0}
        original = store.all_paper_ids

        def counting():
            call_count["n"] += 1
            return original()

        store.all_paper_ids = counting
        expand_network(s2, oa, store, ["SEED1"], hops=1, max_nodes=100, per_paper_limit=5)

        assert call_count["n"] <= 2, (
            f"store.all_paper_ids() called {call_count['n']} times; expected ≤2 after fix"
        )
