"""
R1.12 -- adding a seed.

    fetch metadata -> dedup -> cascade (force=True bypasses, logged) ->
    upsert -> add_node(state=SEED, depth=0) -> append SEED_ADDED event

Two behaviours BUILD.md names explicitly:

  * adding the same seed twice raises `AlreadyPresent`
  * adding a filtered paper without `force` raises with the reason_code attached

**Seeds bypass filters, but the decision is still logged.** BUILD.md: "you want
to know you overrode it." A silent override is how a corpus fills with papers
nobody remembers admitting, and R2.14's drawer is where that becomes visible.

The event matters as much as the node. `graph_nodes.state` is a materialized
projection of the event log (R2.3 rebuilds it from events alone), so a seed
added without its SEED_ADDED event would vanish on the next rebuild.
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
from app.db import make_engine
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.services.seed import AlreadyPresent, SeedRejected, add_seed

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = ROOT / "backend" / "tests" / "fixtures" / "s2_cache.db"
SESSION = 1
AS_OF = 2026

BERT = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
RESNET = "Deep Residual Learning for Image Recognition"
WORD2VEC = "Efficient Estimation of Word Representations in Vector Space"

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
    db = tmp_path / "seed.db"
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


async def _s2_id(client: CachedOnlyS2Client, title: str) -> str:
    (stub,) = (await client.search_title(title))[:1]
    return stub.s2_paper_id


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_adding_a_seed_creates_a_seed_node(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    node = await add_seed(engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF)
    assert node.state == "SEED"
    assert node.depth == 0


async def test_the_seed_is_in_the_graph(env: tuple[Engine, CachedOnlyS2Client]) -> None:
    engine, client = env
    node = await add_seed(engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF)
    with engine.connect() as conn:
        assert node.paper_id in graph_repo.get_node_ids(conn, SESSION)


async def test_a_seed_added_event_is_appended(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    graph_nodes.state is a projection of the event log; R2.3 rebuilds it from
    events alone. A seed with no event would vanish on the next rebuild.
    """
    engine, client = env
    node = await add_seed(engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF)
    with engine.connect() as conn:
        assert events_repo.latest_state(conn, SESSION, node.paper_id) == "SEED_ADDED"


async def test_the_full_metadata_is_stored_not_a_stub(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """A seed is worth a metadata call; boundary papers are not."""
    from app.models import CrawlState
    from app.repo import papers as papers_repo

    engine, client = env
    node = await add_seed(engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF)
    with engine.connect() as conn:
        (paper,) = papers_repo.get_papers_by_ids(conn, [node.paper_id])
    assert paper.crawl_state is CrawlState.METADATA
    assert paper.year == 2019


async def test_the_seed_records_an_accept_decision(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    await add_seed(engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF)
    with engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM filter_decisions ORDER BY id DESC LIMIT 1")
        ).scalar()
    assert outcome == "ACCEPT"


# --------------------------------------------------------------------------
# BUILD.md's two named cases
# --------------------------------------------------------------------------


async def test_adding_the_same_seed_twice_raises(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """BUILD.md: "adding the same seed twice raises AlreadyPresent"."""
    engine, client = env
    s2_id = await _s2_id(client, BERT)
    await add_seed(engine, client, SESSION, s2_id, filters_cfg, AS_OF)
    with pytest.raises(AlreadyPresent):
        await add_seed(engine, client, SESSION, s2_id, filters_cfg, AS_OF)


async def test_a_filtered_paper_raises_with_its_reason_code(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    BUILD.md: "adding a filtered paper without force raises with the
    reason_code attached." "Rejected" alone is not an actionable error.
    """
    engine, client = env
    with pytest.raises(SeedRejected) as exc:
        await add_seed(engine, client, SESSION, await _s2_id(client, RESNET), filters_cfg, AS_OF)
    assert exc.value.reason_code == "CAT_PRIMARY_APPLIED"
    assert "CAT_PRIMARY_APPLIED" in str(exc.value)


async def test_a_pre_era_paper_is_rejected_without_force(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    with pytest.raises(SeedRejected) as exc:
        await add_seed(engine, client, SESSION, await _s2_id(client, WORD2VEC), filters_cfg, AS_OF)
    assert exc.value.reason_code == "PRE_ERA"


async def test_a_rejected_seed_leaves_no_node(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    with pytest.raises(SeedRejected):
        await add_seed(engine, client, SESSION, await _s2_id(client, RESNET), filters_cfg, AS_OF)
    with engine.connect() as conn:
        assert graph_repo.get_node_ids(conn, SESSION) == set()


# --------------------------------------------------------------------------
# force=True -- bypass, but never silently
# --------------------------------------------------------------------------


async def test_force_admits_a_filtered_paper(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    engine, client = env
    node = await add_seed(
        engine, client, SESSION, await _s2_id(client, RESNET), filters_cfg, AS_OF, force=True
    )
    assert node.state == "SEED"


async def test_a_forced_override_is_still_logged(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """
    BUILD.md: "the decision is still logged -- you want to know you overrode
    it." A silent override is how a corpus fills with papers nobody remembers
    admitting.
    """
    engine, client = env
    await add_seed(
        engine, client, SESSION, await _s2_id(client, RESNET), filters_cfg, AS_OF, force=True
    )
    with engine.connect() as conn:
        outcome, reason = conn.execute(
            text("SELECT outcome, reason_code FROM filter_decisions ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert outcome == "REJECT"
    assert reason == "CAT_PRIMARY_APPLIED"


async def test_the_override_is_recorded_on_the_event(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """The event payload is where "why is this here?" gets answered."""
    engine, client = env
    node = await add_seed(
        engine, client, SESSION, await _s2_id(client, RESNET), filters_cfg, AS_OF, force=True
    )
    with engine.connect() as conn:
        (event,) = events_repo.get_events(conn, SESSION, node.paper_id)
    assert event.payload.get("forced") is True
    assert event.payload.get("reason_code") == "CAT_PRIMARY_APPLIED"


async def test_force_on_an_acceptable_paper_is_not_marked_as_an_override(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """force is permission to override, not a claim that one happened."""
    engine, client = env
    node = await add_seed(
        engine, client, SESSION, await _s2_id(client, BERT), filters_cfg, AS_OF, force=True
    )
    with engine.connect() as conn:
        (event,) = events_repo.get_events(conn, SESSION, node.paper_id)
    assert event.payload.get("forced") is not True


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


async def test_an_unknown_paper_id_raises(env: tuple[Engine, CachedOnlyS2Client]) -> None:
    from app.clients.s2 import CacheMiss

    engine, client = env
    with pytest.raises((SeedRejected, CacheMiss, LookupError)):
        await add_seed(engine, client, SESSION, "not-a-real-id", filters_cfg, AS_OF)


async def test_seeds_are_session_scoped(env: tuple[Engine, CachedOnlyS2Client]) -> None:
    """The same paper can seed two workspaces independently."""
    engine, client = env
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    s2_id = await _s2_id(client, BERT)
    await add_seed(engine, client, SESSION, s2_id, filters_cfg, AS_OF)
    await add_seed(engine, client, 2, s2_id, filters_cfg, AS_OF)
    with engine.connect() as conn:
        assert len(graph_repo.get_node_ids(conn, SESSION)) == 1
        assert len(graph_repo.get_node_ids(conn, 2)) == 1


async def test_a_second_seed_reuses_the_cached_paper(
    env: tuple[Engine, CachedOnlyS2Client],
) -> None:
    """Seeding session B with a paper session A already crawled costs nothing."""
    engine, client = env
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
    s2_id = await _s2_id(client, BERT)
    await add_seed(engine, client, SESSION, s2_id, filters_cfg, AS_OF)
    before = client.api_calls
    await add_seed(engine, client, 2, s2_id, filters_cfg, AS_OF)
    assert client.api_calls == before
