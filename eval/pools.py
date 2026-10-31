"""
Turning a benchmark case into a rankable pool (R4.6).

For one case this does what the product does for one session -- pool the
neighbours of the seeds, compute the R3 features, normalize -- but without a
session, a graph or any write. The output is `ScoredPool`: features per
candidate and the ground truth, which is all `sweep` needs and exactly what
`PUT /api/config` rescoring needs too.

**Leakage, stated where it happens.** Candidates published after the case's
cutoff are removed (`temporal.enforce_cutoff`), and an undated one is treated
as invisible. Two things are *not* made historical, because the corpus stores
only today's values: `citation_count` (and so `quality`) and the citing side of
`cocite`. Both include citations that arrived after the cutoff. That inflates
the citation-count baseline and the real ranker alike; the write-up has to say
so rather than let the numbers read as clean.

Pooling runs against session id 0, which owns no rows, so the session-scoped
exclusions (graph nodes, tombstones, filter verdicts) are all empty -- a
benchmark case has no history. The filter cascade is likewise not re-run:
stored verdicts are per session, and the ground truth is whatever the target
cited, filtered or not.
"""

from __future__ import annotations

import networkx as nx
from build_benchmark import BenchmarkCase
from sqlalchemy import Connection, text
from sweep import ScoredPool
from temporal import enforce_cutoff

from app.config import FiltersConfig
from app.repo import papers as papers_repo
from app.services.candidates import PoolEntry, build_pool
from app.services.features import FEATURE_NAMES
from app.services.filters import is_core_venue
from app.services.graphops import personalized_pagerank
from app.services.ranking import citations_per_year, rank_percentile, recency
from app.services.similarity import compute_similarity

#: A session id that owns nothing. See the module docstring.
NO_SESSION = 0

#: Indicators are not rank-normalized -- same rule as `features.py`.
BINARY_FEATURES = frozenset({"venue"})


def _in_degree(conn: Connection, ids: list[int]) -> dict[int, int]:
    """Citations received from inside `ids`: what 'everyone here cites it' means."""
    if not ids:
        return {}
    marks = ",".join(f":p{i}" for i in range(len(ids)))
    params = {f"p{i}": v for i, v in enumerate(ids)}
    rows = conn.execute(
        text(
            "SELECT cited_id, COUNT(DISTINCT citing_id) FROM edges"
            f" WHERE cited_id IN ({marks}) AND citing_id IN ({marks})"
            " GROUP BY cited_id"
        ),
        params,
    )
    return {int(cited): int(n) for cited, n in rows}


def _ppr(conn: Connection, ids: list[int], seeds: list[int]) -> dict[int, float]:
    """
    Personalized PageRank restarting on the seeds, over the edges among
    `ids`. The same undirected projection the product uses, minus the crawl-state
    suppression: a benchmark pool has no crawl frontier to be biased by.
    """
    if not ids:
        return {}
    marks = ",".join(f":p{i}" for i in range(len(ids)))
    params = {f"p{i}": v for i, v in enumerate(ids)}
    rows = conn.execute(
        text(
            "SELECT DISTINCT citing_id, cited_id FROM edges"
            f" WHERE cited_id IN ({marks}) AND citing_id IN ({marks})"
            " ORDER BY citing_id, cited_id"
        ),
        params,
    ).fetchall()
    graph: nx.Graph[int] = nx.Graph()
    graph.add_nodes_from(sorted(ids))
    graph.add_edges_from((int(a), int(b)) for a, b in rows)
    return personalized_pagerank(graph, seeds)


def build_scored_pool(
    conn: Connection,
    case_id: int,
    case: BenchmarkCase,
    cfg: FiltersConfig,
    as_of_year: int,
) -> tuple[ScoredPool, list[PoolEntry]]:
    """
    The case's candidates after the cutoff guard, with normalized features.

    Also returns the surviving `PoolEntry` list: the baselines rank on raw
    `citation_count` and `anchor_overlap`, and must see the same candidates the
    ranker did or the comparison measures the pool instead of the ranking.
    """
    entries = build_pool(conn, NO_SESSION, list(case.seed_ids), cfg, as_of_year)
    papers = {
        p.id: p
        for p in papers_repo.get_papers_by_ids(conn, [e.paper_id for e in entries])
        if p.id is not None
    }

    # The most specific date the corpus holds: a full date if there is one, else
    # the bare year, which `visible_at` reads as a range and rounds safely.
    dated = [
        (e.paper_id, papers[e.paper_id].publication_date or _year_str(papers[e.paper_id].year))
        for e in entries
    ]
    visible = set(enforce_cutoff(dated, case.cutoff))
    kept = [e for e in entries if e.paper_id in visible]

    ids = sorted(e.paper_id for e in kept)
    similarity = compute_similarity(conn, sorted(case.seed_ids))
    hub = _in_degree(conn, sorted(set(ids) | set(case.seed_ids)))

    ppr = _ppr(conn, sorted(set(ids) | set(case.seed_ids)), sorted(case.seed_ids))

    raw: dict[str, dict[int, float]] = {name: {} for name in FEATURE_NAMES}
    for e in kept:
        paper = papers[e.paper_id]
        raw["overlap"][e.paper_id] = float(e.anchor_overlap)
        raw["quality"][e.paper_id] = citations_per_year(
            paper.citation_count, paper.year, as_of_year
        )
        raw["recency"][e.paper_id] = recency(paper.year, as_of_year)
        raw["hub"][e.paper_id] = float(hub.get(e.paper_id, 0))
        raw["cocite"][e.paper_id] = float(similarity.co_citation.get(e.paper_id, 0))
        raw["bibcoup"][e.paper_id] = float(similarity.bib_coupling.get(e.paper_id, 0))
        raw["venue"][e.paper_id] = 1.0 if is_core_venue(paper.venue, cfg.core_venues) else 0.0
        raw["ppr"][e.paper_id] = ppr.get(e.paper_id, 0.0)

    normalized = {
        name: (dict(values) if name in BINARY_FEATURES else rank_percentile(values))
        for name, values in raw.items()
    }
    features = {pid: {name: float(normalized[name][pid]) for name in FEATURE_NAMES} for pid in ids}

    pool = ScoredPool(
        case_id=case_id,
        features=features,
        ground_truth=frozenset(case.ground_truth_ids),
    )
    return pool, sorted(kept, key=lambda e: e.paper_id)


def _year_str(year: int | None) -> str | None:
    return str(year) if year is not None else None


__all__ = ["BINARY_FEATURES", "NO_SESSION", "build_scored_pool"]
