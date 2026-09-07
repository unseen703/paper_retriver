"""
R0.11 CHECKPOINT -- arXiv categories resolve.

BUILD.md wants two specific answers, and says both must be right:

    fetch "BERT"                     -> cs.CL   (proves the join works)
    fetch "Deep Residual Learning"   -> cs.CV   (proves you will actually
                                                 deny cs.CV)

The second is the one with teeth. `CAT_PRIMARY_APPLIED` is the rule that rejects
computer-vision papers, and it keys off `primary_arxiv_category`. If the join
silently returns NULL, that rule never fires, the paper falls through to the
weaker venue/keyword fallback, and nothing looks broken -- the corpus just
quietly fills with cs.CV.

The third case here is not in BUILD.md but matters just as much: Batch
Normalization is `cs.LG` cross-listed `cs.CV`. It must come back cs.LG, because
denial keys off the *primary* category, not membership. Get that wrong and the
deny list eats a third of core ML.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from app.clients.arxiv import ArxivRecord
from app.db import make_engine
from app.repo import arxiv_meta
from app.services.categories import CategoryResolver

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"

# Real arXiv ids, so these stay meaningful against the loaded snapshot.
BERT = "1810.04805"
RESNET = "1512.03385"
BATCHNORM = "1502.03167"
TRANSFORMER = "1706.03762"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "cat.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    arxiv_meta.load(
        eng,
        [
            ArxivRecord(BERT, "cs.CL", ("cs.CL",), "2019-05-24"),
            ArxivRecord(RESNET, "cs.CV", ("cs.CV",), "2015-12-10"),
            ArxivRecord(BATCHNORM, "cs.LG", ("cs.LG", "cs.CV"), "2015-03-02"),
            ArxivRecord(TRANSFORMER, "cs.CL", ("cs.CL", "cs.LG"), "2017-12-06"),
        ],
    )
    yield eng
    eng.dispose()


@pytest.fixture
def resolver(engine: Engine) -> CategoryResolver:
    return CategoryResolver(engine)


# --------------------------------------------------------------------------
# The checkpoint itself
# --------------------------------------------------------------------------


def test_bert_resolves_to_cs_cl(resolver: CategoryResolver) -> None:
    """Confirms the join is wired at all."""
    assert resolver.primary_category(BERT) == "cs.CL"


def test_resnet_resolves_to_cs_cv(resolver: CategoryResolver) -> None:
    """Confirms cs.CV will actually be denied rather than silently admitted."""
    assert resolver.primary_category(RESNET) == "cs.CV"


def test_a_cross_listed_paper_keeps_its_primary(resolver: CategoryResolver) -> None:
    """Batch Normalization: cs.LG primary, cs.CV secondary. Must read cs.LG."""
    assert resolver.primary_category(BATCHNORM) == "cs.LG"
    assert resolver.categories(BATCHNORM) == ("cs.LG", "cs.CV")


# --------------------------------------------------------------------------
# Enrichment of whole Paper objects
# --------------------------------------------------------------------------


def test_enrich_populates_the_category_fields(resolver: CategoryResolver) -> None:
    from app.models import Paper

    paper = Paper(s2_paper_id="x", title="BERT", first_seen_at="2026-01-01", arxiv_id=BERT)
    assert paper.primary_arxiv_category is None
    (enriched,) = resolver.enrich([paper])
    assert enriched.primary_arxiv_category == "cs.CL"
    assert enriched.arxiv_categories == ("cs.CL",)


def test_enrich_leaves_a_paper_without_an_arxiv_id_alone(resolver: CategoryResolver) -> None:
    """Journal-only papers never get a category; that is the VENUE_CORE case."""
    from app.models import Paper

    paper = Paper(s2_paper_id="x", title="Some Journal Paper", first_seen_at="2026-01-01")
    (enriched,) = resolver.enrich([paper])
    assert enriched.primary_arxiv_category is None


def test_enrich_leaves_an_unknown_arxiv_id_alone(resolver: CategoryResolver) -> None:
    """Newer than the snapshot -> NULL until the OAI delta runs."""
    from app.models import Paper

    paper = Paper(
        s2_paper_id="x", title="Brand New", first_seen_at="2026-01-01", arxiv_id="2609.99999"
    )
    (enriched,) = resolver.enrich([paper])
    assert enriched.primary_arxiv_category is None


def test_enrich_is_one_query_for_many_papers(resolver: CategoryResolver) -> None:
    """N+1 here would mean one SELECT per candidate in every expansion."""
    from app.models import Paper

    papers = [
        Paper(s2_paper_id=f"s{i}", title=f"T{i}", first_seen_at="2026-01-01", arxiv_id=a)
        for i, a in enumerate([BERT, RESNET, BATCHNORM, TRANSFORMER])
    ]
    enriched = resolver.enrich(papers)
    assert [p.primary_arxiv_category for p in enriched] == ["cs.CL", "cs.CV", "cs.LG", "cs.CL"]
    assert resolver.queries == 1


def test_enrich_preserves_order_and_length(resolver: CategoryResolver) -> None:
    from app.models import Paper

    papers = [
        Paper(s2_paper_id="a", title="A", first_seen_at="2026-01-01", arxiv_id=BERT),
        Paper(s2_paper_id="b", title="B", first_seen_at="2026-01-01"),
        Paper(s2_paper_id="c", title="C", first_seen_at="2026-01-01", arxiv_id=RESNET),
    ]
    enriched = resolver.enrich(papers)
    assert [p.s2_paper_id for p in enriched] == ["a", "b", "c"]


def test_enrich_on_an_empty_list_does_no_work(resolver: CategoryResolver) -> None:
    assert resolver.enrich([]) == []
    assert resolver.queries == 0


def test_enrichment_does_not_mutate_the_input(resolver: CategoryResolver) -> None:
    """Papers are frozen; enrich must return new objects, not mutate."""
    from app.models import Paper

    paper = Paper(s2_paper_id="x", title="BERT", first_seen_at="2026-01-01", arxiv_id=BERT)
    resolver.enrich([paper])
    assert paper.primary_arxiv_category is None


# --------------------------------------------------------------------------
# Coverage -- makes silent degradation visible
# --------------------------------------------------------------------------


def test_coverage_reports_the_fraction_that_resolved(resolver: CategoryResolver) -> None:
    """
    A falling coverage number is the early warning that the snapshot has gone
    stale. Without it, a NULL category is indistinguishable from a paper that
    genuinely has none.
    """
    from app.models import Paper

    papers = [
        Paper(s2_paper_id="a", title="A", first_seen_at="2026-01-01", arxiv_id=BERT),
        Paper(s2_paper_id="b", title="B", first_seen_at="2026-01-01", arxiv_id="2609.99999"),
    ]
    resolver.enrich(papers)
    assert resolver.resolved == 1
    assert resolver.with_arxiv_id == 2
    assert resolver.coverage == 0.5


def test_coverage_of_nothing_is_not_a_division_by_zero(resolver: CategoryResolver) -> None:
    assert resolver.coverage == 0.0
