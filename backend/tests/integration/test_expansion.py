"""
R1.11 -- the expansion orchestrator: the whole PLAN.md section E pipeline.

    (1) frontier  (2) edge fetch  (3) pool  (4) dedup+exclude  (5) filter
    (6) prescore  (7) enrich  (8) features  (9) rank  (10) budget  (11) commit

Runs against `CachedOnlyS2Client`, so this file cannot reach the network even by
accident -- a cache miss raises rather than fetching.

**"A partial expansion is a success."** BUILD.md is explicit: on hitting
`api_call_budget` or `max_nodes`, commit what you have and record the truncation
in `expansions.error`. The alternative -- rolling back -- throws away paid-for
API calls and leaves the user with nothing, which is strictly worse than a
smaller graph. Several tests below exist only to pin that behaviour.

The `expansions` row is the audit trail. Without `n_pool`, `n_filtered`,
`n_added`, `api_calls` and `cache_hits` written down, "why did I only get four
papers?" is unanswerable after the fact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client
from app.config import filters as filters_cfg
from app.config import ranking as ranking_cfg
from app.db import make_engine
from app.services.expansion import ExpandParams, expand

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = ROOT / "backend" / "tests" / "fixtures" / "s2_cache.db"
SESSION = 1
AS_OF = 2026

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(), reason="fixture cache absent; run scripts/build_fixture_cache.py"
)

# The arXiv categories these fixtures need. The production database carries all
# 3.16M rows from R0.10; a temp database has none, and without them the topic
# filter's primary-category rung cannot fire -- which is exactly the bug this
# module's wiring fixes, so the tests must supply the data rather than assume it.
ARXIV_META = [
    ("1810.04805", "cs.CL", ("cs.CL",)),  # BERT
    ("1706.03762", "cs.CL", ("cs.CL", "cs.LG")),  # Attention
    ("1512.03385", "cs.CV", ("cs.CV",)),  # ResNet
    ("1301.3781", "cs.CL", ("cs.CL",)),  # word2vec
    ("1502.03167", "cs.LG", ("cs.LG", "cs.CV")),  # Batch Normalization
    ("2005.14165", "cs.CL", ("cs.CL",)),  # GPT-3
]


def _load_arxiv_meta(engine: Engine) -> None:
    from app.clients.arxiv import ArxivRecord
    from app.repo import arxiv_meta

    arxiv_meta.load(
        engine,
        [ArxivRecord(i, primary, cats, "2020-01-01") for i, primary, cats in ARXIV_META],
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[Engine, CachedOnlyS2Client]]:
    db = tmp_path / "expansion.db"
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent))
    c.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(c, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    _load_arxiv_meta(engine)

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    client = CachedOnlyS2Client(cache=ResponseCache(fixture_engine))
    yield engine, client
    await client.aclose()
    fixture_engine.dispose()
    engine.dispose()


async def _seed_bert(engine: Engine, client: CachedOnlyS2Client) -> int:
    """Put BERT in the graph as a SEED, using only cached data."""
    from app.repo import graph as graph_repo
    from app.repo import papers as papers_repo

    (stub,) = (
        await client.search_title(
            "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
        )
    )[:1]
    (paper,) = await client.get_papers([stub.s2_paper_id])
    with engine.begin() as conn:
        paper_id = papers_repo.upsert_paper(conn, paper)
        graph_repo.add_node(conn, SESSION, paper_id, "SEED", depth=0)
    return paper_id


def _row(engine: Engine) -> tuple:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, n_pool, n_filtered, n_added, api_calls, cache_hits,"
                " error, params, config_version FROM expansions ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()


# --------------------------------------------------------------------------
# BUILD.md's named verification
# --------------------------------------------------------------------------


async def test_seeding_bert_and_expanding_adds_nodes(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """BUILD.md: seed BERT, expand with max_new=20, assert node count."""
    engine, client = env
    seed = await _seed_bert(engine, client)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert result.n_added > 0

    from app.repo import graph as graph_repo

    with engine.connect() as conn:
        node_ids = graph_repo.get_node_ids(conn, SESSION)
    assert seed in node_ids
    assert len(node_ids) == result.n_added + 1


async def test_no_rejected_paper_reaches_the_graph(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """BUILD.md: "assert no rejected paper present"."""
    engine, client = env
    await _seed_bert(engine, client)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    with engine.connect() as conn:
        leaked = conn.execute(
            text(
                "SELECT COUNT(*) FROM graph_nodes g JOIN filter_decisions f"
                " ON f.paper_id = g.paper_id"
                " WHERE g.session_id = :sid AND f.outcome = 'REJECT'"
            ),
            {"sid": SESSION},
        ).scalar()
    assert leaked == 0


async def test_the_api_call_budget_is_respected(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """BUILD.md: "assert api_calls <= 60"."""
    engine, client = env
    await _seed_bert(engine, client)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert result.api_calls <= 60


async def test_the_expansions_row_is_populated(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """BUILD.md: "assert the expansions row is populated"."""
    engine, client = env
    await _seed_bert(engine, client)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    status, n_pool, n_filtered, n_added, api_calls, cache_hits, error, params, cfgv = _row(engine)
    assert status == "DONE"
    assert n_pool > 0
    assert n_added > 0
    assert cache_hits > 0
    assert error is None
    assert "max_new" in params
    assert cfgv == filters_cfg.config_version


# --------------------------------------------------------------------------
# "A partial expansion is a success"
# --------------------------------------------------------------------------


async def test_hitting_max_nodes_commits_what_it_has(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    BUILD.md: "On hitting either, commit what you have and record the truncation
    in expansions.error. A partial expansion is a success."
    """
    engine, client = env
    await _seed_bert(engine, client)
    result = await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, max_nodes=4),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    from app.repo import graph as graph_repo

    with engine.connect() as conn:
        assert len(graph_repo.get_node_ids(conn, SESSION)) <= 4
    assert result.truncated is True


async def test_a_truncation_is_recorded_rather_than_raised(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await _seed_bert(engine, client)
    await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, max_nodes=4),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    status, *_rest, error, _params, _cfgv = _row(engine)
    assert status == "DONE"
    assert error is not None
    assert "max_nodes" in error


async def test_a_truncated_run_still_commits_its_nodes(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """Rolling back would discard paid-for API calls and leave nothing."""
    engine, client = env
    await _seed_bert(engine, client)
    await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, max_nodes=3),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    from app.repo import graph as graph_repo

    with engine.connect() as conn:
        assert len(graph_repo.get_node_ids(conn, SESSION)) > 1


async def test_an_api_budget_of_zero_still_completes(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """Degenerate, but must record a truncation rather than crash."""
    engine, client = env
    await _seed_bert(engine, client)
    result = await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, api_call_budget=0),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    assert result.truncated is True


# --------------------------------------------------------------------------
# Boundary papers survive the pipeline
# --------------------------------------------------------------------------


async def test_pre_era_references_are_stored_without_nodes(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    R1.9's rule, end to end. BERT's references include word2vec and GloVe; both
    must land in `papers` with edges and no `graph_nodes` row.
    """
    engine, client = env
    await _seed_bert(engine, client)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    with engine.connect() as conn:
        orphans = conn.execute(
            text(
                "SELECT COUNT(*) FROM papers p"
                " LEFT JOIN graph_nodes g ON g.paper_id = p.id AND g.session_id = :sid"
                " WHERE g.paper_id IS NULL"
            ),
            {"sid": SESSION},
        ).scalar()
    assert orphans > 0, "corpus and graph are not separated; boundary papers vanished"


async def test_edges_are_written_for_boundary_papers(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await _seed_bert(engine, client)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    with engine.connect() as conn:
        assert (conn.execute(text("SELECT COUNT(*) FROM edges")).scalar() or 0) > 20


# --------------------------------------------------------------------------
# Determinism and idempotence
# --------------------------------------------------------------------------


async def test_a_second_expansion_picks_up_what_the_budget_could_not_fit(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    More of BERT's references pass the filter than `max_new` admits, and the
    surplus is not discarded -- it stays in the pool for the next expansion.
    Pressing Expand again is how you get the rest, which is the behaviour the
    UI depends on.
    """
    engine, client = env
    await _seed_bert(engine, client)
    first = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    second = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert first.n_added == 20
    assert 0 < second.n_added < 20, "the budget surplus was lost rather than deferred"


async def test_expansion_converges_once_the_pool_is_drained(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """It must terminate: a third pass over one seed has nothing left to add."""
    engine, client = env
    await _seed_bert(engine, client)
    for _ in range(3):
        await expand(
            engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
        )
    final = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert final.n_added == 0
    assert final.n_pool == 0


async def test_the_second_run_makes_no_api_calls(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await _seed_bert(engine, client)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    second = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert second.api_calls == 0


async def test_an_empty_frontier_is_a_no_op(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """No seeds means nothing to expand from -- not an error."""
    engine, client = env
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert result.n_added == 0
    assert _row(engine)[0] == "DONE"


async def test_every_added_node_records_its_provenance(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """`added_by` points at the expansion that introduced the node."""
    engine, client = env
    await _seed_bert(engine, client)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    with engine.connect() as conn:
        unattributed = conn.execute(
            text(
                "SELECT COUNT(*) FROM graph_nodes"
                " WHERE session_id = :sid AND state = 'CANDIDATE' AND added_by IS NULL"
            ),
            {"sid": SESSION},
        ).scalar()
    assert result.expansion_id is not None
    assert unattributed == 0
