"""
R0.9 -- CachedOnlyS2Client against the committed fixture cache.

BUILD.md calls the fixture DB the single highest-leverage two hours in the
project, and the reason is this file: everything here runs with **no network
transport at all**. Not a mock that could accidentally reach out -- there is
literally no way for these tests to make a request, because CachedOnlyS2Client
raises CacheMiss instead.

That property is what makes every later filter, dedup and ranking test both
fast and deterministic. If a test in this file starts failing after someone
edits the fixture DB, the fixture is wrong, not the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client, CacheMiss
from app.db import make_engine

FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def client() -> CachedOnlyS2Client:
    engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    return CachedOnlyS2Client(cache=ResponseCache(engine))


# --------------------------------------------------------------------------
# The offline guarantee itself
# --------------------------------------------------------------------------


async def test_a_miss_raises_rather_than_reaching_the_network(
    client: CachedOnlyS2Client,
) -> None:
    with pytest.raises(CacheMiss):
        await client.search_title("a title nobody has ever published xyzzy")


async def test_the_client_has_no_usable_transport(client: CachedOnlyS2Client) -> None:
    """Belt and braces: even the escape hatch is closed."""
    with pytest.raises(CacheMiss):
        await client.get_papers(["an-id-that-is-not-cached"])


async def test_cached_reads_never_increment_api_calls(client: CachedOnlyS2Client) -> None:
    await client.search_title("Attention Is All You Need")
    assert client.api_calls == 0
    assert client.cache_hits > 0


# --------------------------------------------------------------------------
# Fixture coverage -- one test per documented case in BUILD.md R0.9
# --------------------------------------------------------------------------


async def test_hub_paper_is_available(client: CachedOnlyS2Client) -> None:
    """Forward-expand guard: a 190k-citation hub must be in the fixtures."""
    (stub, *_) = await client.search_title("Attention Is All You Need")
    assert stub.citation_count is not None
    assert stub.citation_count > 100_000


async def test_boundary_paper_predates_the_year_floor(client: CachedOnlyS2Client) -> None:
    """PRE_ERA: stored as a row + edges, never given a graph_nodes row."""
    (stub, *_) = await client.search_title(
        "Efficient Estimation of Word Representations in Vector Space"
    )
    assert stub.year is not None
    assert stub.year < 2015


async def test_a_primary_cs_cv_paper_is_available(client: CachedOnlyS2Client) -> None:
    """CAT_PRIMARY_APPLIED. Needed to prove the deny list actually fires."""
    (stub, *_) = await client.search_title("Deep Residual Learning for Image Recognition")
    assert stub.title.lower().startswith("deep residual learning")


async def test_a_dataset_paper_is_available(client: CachedOnlyS2Client) -> None:
    """IS_DATASET -> REJECT per filters.yaml paper_type_policy."""
    stubs = await client.search_title("ImageNet: A Large-Scale Hierarchical Image Database")
    assert stubs


async def test_a_high_citation_survey_is_available(client: CachedOnlyS2Client) -> None:
    """The survey-accept path: citation_count_min is 200."""
    (stub, *_) = await client.search_title("A Comprehensive Survey on Graph Neural Networks")
    assert stub.citation_count is not None
    assert stub.citation_count >= 200


async def test_a_medical_ai_paper_is_available(client: CachedOnlyS2Client) -> None:
    """FIELD_NON_CS -- an applied paper that uses AI as a tool."""
    stubs = await client.search_title(
        "Dermatologist-level classification of skin cancer with deep neural networks"
    )
    assert stubs


async def test_a_venue_only_paper_is_available(client: CachedOnlyS2Client) -> None:
    """VENUE_CORE fallback: a core-venue paper carrying no arXiv id."""
    (stub, *_) = await client.search_title("Adam: A Method for Stochastic Optimization")
    assert stub.venue


async def test_seed_corpus_papers_are_available(client: CachedOnlyS2Client) -> None:
    """The user's own seed list, so R1.12's seeding path is testable offline."""
    for title in (
        "ReAct: Synergizing Reasoning and Acting in Language Models",
        "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
    ):
        assert await client.search_title(title), f"missing fixture: {title}"


# --------------------------------------------------------------------------
# Edges, offline
# --------------------------------------------------------------------------


async def test_references_are_cached_for_the_hub(client: CachedOnlyS2Client) -> None:
    (stub, *_) = await client.search_title("Attention Is All You Need")
    refs = await client.get_references(stub.s2_paper_id)
    assert len(refs) > 5
    assert all(r.citing_s2_id == stub.s2_paper_id for r in refs)


async def test_no_reference_is_a_self_citation(client: CachedOnlyS2Client) -> None:
    (stub, *_) = await client.search_title("Attention Is All You Need")
    refs = await client.get_references(stub.s2_paper_id)
    assert all(r.citing_s2_id != r.cited_s2_id for r in refs)


async def test_metadata_round_trips_from_the_fixture(client: CachedOnlyS2Client) -> None:
    (stub, *_) = await client.search_title("Attention Is All You Need")
    (paper,) = await client.get_papers([stub.s2_paper_id])
    assert paper.year == 2017
    assert paper.arxiv_id == "1706.03762"
