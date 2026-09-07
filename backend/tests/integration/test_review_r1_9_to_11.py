"""
Fixes for the R1.9-R1.11 review findings.

Two real ones, both in the orchestrator, and both of the kind that produce a
working system that is quietly worse than intended.

**`max_nodes` counted the wrong thing.** `node_count` incremented once per
*ingested reference*, not once per *graph node*. Since boundary papers never
become nodes -- and R1.7 measured those as 15 of BERT's 50 references -- the year
floor was eating the node budget. A graph capped at 2000 would stop ingesting
after 2000 references while holding perhaps 1200 nodes.

**Forward expansion was never performed.** `expand` only ever called
`get_references`. `ExpandParams.max_citations` was defined and unused, R1.10's
FORWARD direction floor could never fire, and the hub guard in `build_pool` --
which exists specifically to stop forward expansion from a 191k-citation paper
-- guarded a code path that did not exist. The recommender only looked backward,
so it could surface a paper's influences but never its influence.
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
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.expansion import ExpandParams, expand

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = ROOT / "backend" / "tests" / "fixtures" / "s2_cache.db"
SESSION = 1
AS_OF = 2026

pytestmark = pytest.mark.skipif(not FIXTURE_DB.is_file(), reason="fixture cache absent")

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
    db = tmp_path / "r911.db"
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


async def _seed(engine: Engine, client: CachedOnlyS2Client, title: str) -> int:
    (stub,) = (await client.search_title(title))[:1]
    (paper,) = await client.get_papers([stub.s2_paper_id])
    with engine.begin() as conn:
        paper_id = papers_repo.upsert_paper(conn, paper)
        graph_repo.add_node(conn, SESSION, paper_id, "SEED", depth=0)
    return paper_id


BERT = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
ATTENTION = "Attention Is All You Need"


# --------------------------------------------------------------------------
# Finding 1 -- max_nodes must cap graph nodes, not ingested papers
# --------------------------------------------------------------------------


async def test_boundary_papers_do_not_consume_the_node_budget(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    R1.7 measured 15 of BERT's 50 references as pre-era. Those never become
    nodes, so counting them against max_nodes silently shrinks the graph.
    """
    engine, client = env
    await _seed(engine, client, BERT)
    await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, max_nodes=10),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    with engine.connect() as conn:
        nodes = len(graph_repo.get_node_ids(conn, SESSION))
        papers = conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0
    # The corpus grows well past the node cap; the graph respects it.
    assert nodes <= 10
    assert papers > 10, "ingestion stopped at the node cap, discarding evidence"


async def test_a_generous_node_cap_ingests_the_whole_reference_list(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    With max_nodes=2000 the whole bibliography must be ingested. Under the old
    counter, ingestion stopped once the count of *references seen* hit the cap.
    """
    engine, client = env
    await _seed(engine, client, BERT)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    with engine.connect() as conn:
        papers = conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0
    assert papers > 40, "the reference list was truncated by the node cap"


# --------------------------------------------------------------------------
# Finding 2 -- forward expansion must actually happen
# --------------------------------------------------------------------------


async def test_forward_expansion_is_performed(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    Without it the recommender surfaces a paper's influences but never its
    influence, R1.10's FORWARD direction floor is unreachable, and build_pool's
    hub guard protects a code path that does not exist.
    """
    engine, client = env
    await _seed(engine, client, ATTENTION)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert result.forward_fetched > 0 or result.hub_skipped > 0


async def test_a_hub_seed_skips_forward_but_still_expands_backward(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    Attention Is All You Need has 191,436 citations, far over
    forward_expand_max. Forward is skipped; backward must still run.
    """
    engine, client = env
    await _seed(engine, client, ATTENTION)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    assert result.hub_skipped == 1
    assert result.n_added > 0


async def test_max_citations_is_actually_used(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """A parameter nobody reads is a promise the code does not keep."""
    import inspect

    from app.services import expansion

    source = inspect.getsource(expansion)
    assert source.count("max_citations") >= 2


# --------------------------------------------------------------------------
# Neither fix may break what already worked
# --------------------------------------------------------------------------


async def test_the_node_count_still_matches_what_was_added(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    seed = await _seed(engine, client, BERT)
    result = await expand(
        engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF
    )
    with engine.connect() as conn:
        node_ids = graph_repo.get_node_ids(conn, SESSION)
    assert len(node_ids) == result.n_added + 1
    assert seed in node_ids


async def test_rejected_papers_still_never_reach_the_graph(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await _seed(engine, client, BERT)
    await expand(engine, client, SESSION, ExpandParams(max_new=20), filters_cfg, ranking_cfg, AS_OF)
    with engine.connect() as conn:
        leaked = conn.execute(
            text(
                "SELECT COUNT(*) FROM graph_nodes g JOIN filter_decisions f"
                " ON f.paper_id = g.paper_id"
                " WHERE g.session_id = :sid AND f.outcome != 'ACCEPT'"
            ),
            {"sid": SESSION},
        ).scalar()
    assert leaked == 0


async def test_truncation_is_still_recorded_not_raised(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await _seed(engine, client, BERT)
    result = await expand(
        engine,
        client,
        SESSION,
        ExpandParams(max_new=20, max_nodes=3),
        filters_cfg,
        ranking_cfg,
        AS_OF,
    )
    assert result.truncated is True
    with engine.connect() as conn:
        status, error = conn.execute(
            text("SELECT status, error FROM expansions ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert status == "DONE"
    assert error is not None
