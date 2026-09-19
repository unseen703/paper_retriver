"""
The expansion orchestrator -- PLAN.md section E, end to end.

    (1) frontier  (2) edge fetch  (3) pool  (4) dedup+exclude  (5) filter
    (6) prescore  (7) enrich  (8) features  (9) rank  (10) budget  (11) commit

**A partial expansion is a success.** BUILD.md is explicit: on hitting
`api_call_budget` or `max_nodes`, commit what you have and record the truncation
in `expansions.error`. Rolling back would throw away API calls already paid for
at one request per second and leave the user with nothing, which is strictly
worse than a smaller graph. So truncation is a normal outcome here, not an
exception -- `status` stays DONE and `error` explains.

Ordering note: edges are ingested *before* candidates are ranked, so a paper
rejected by the cascade still contributes its citation structure (R1.9). The
graph shrinks; the corpus does not.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import Engine

from app.clients.s2 import CacheMiss, S2Client, S2TransientError
from app.config import FiltersConfig, RankingConfig
from app.repo import expansions as expansions_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo
from app.services.budget import allocate
from app.services.candidates import build_pool, prescore
from app.services.categories import CategoryResolver
from app.services.features import compute_and_store_features
from app.services.ingest import IngestStats, ingest_neighbour

logger = logging.getLogger(__name__)

ANCHOR_STATES = ["SEED", "LIKED"]


@dataclass(frozen=True, slots=True)
class ExpandParams:
    max_new: int = 20
    # Hard stops. Both are "commit what you have", never "raise".
    api_call_budget: int = 60
    max_nodes: int = 2000
    max_references: int = 200
    max_citations: int = 1000


@dataclass
class ExpansionResult:
    expansion_id: int | None = None
    n_pool: int = 0
    n_filtered: int = 0
    n_added: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    truncated: bool = False
    error: str | None = None
    backward_fetched: int = 0
    forward_fetched: int = 0
    hub_skipped: int = 0
    # Which papers this run actually admitted, sorted. BUILD.md's R1.18
    # verification asks the endpoint to return them, and PLAN.md's async job
    # shape carries the same list as `result_node_ids` -- so the expander
    # reports what it added rather than making the caller diff the graph before
    # and after, which would race with anything else writing to the session.
    added_paper_ids: list[int] = field(default_factory=list)
    ingest: IngestStats = field(default_factory=IngestStats)


async def expand(
    engine: Engine,
    client: S2Client,
    session_id: int,
    params: ExpandParams,
    filters_cfg: FiltersConfig,
    ranking_cfg: RankingConfig,
    as_of_year: int,
    expansion_id: int | None = None,
) -> ExpansionResult:
    """
    Run one expansion for `session_id`. Never raises on budget exhaustion.

    `expansion_id` names a row that already exists -- R2.4's worker creates it
    QUEUED at the moment of the request and flips it to RUNNING when it claims
    it, so the id is answerable in the 202 long before this function starts.
    Called without one (the CLI, and every direct test), this opens its own
    row exactly as it always did.
    """
    result = ExpansionResult()
    # One resolver for the run: enrich() is a single bulk query per batch.
    resolver = CategoryResolver(engine)
    calls_at_start = client.api_calls
    hits_at_start = client.cache_hits

    with engine.begin() as conn:
        result.expansion_id = expansion_id or expansions_repo.start(
            conn, session_id, asdict(params), filters_cfg.config_version
        )
        # (1) Frontier: seeds and likes. Candidates are never expanded from --
        # that is what keeps depth bounded without an explicit hop counter.
        anchors = [n.paper_id for n in graph_repo.get_nodes(conn, session_id, ANCHOR_STATES)]
        anchor_papers = papers_repo.get_papers_by_ids(conn, anchors)

    if not anchors:
        return _finish(engine, session_id, result, "no anchors in this session")

    # (2) Edge fetch, then (immediately) ingest. Doing this outside the write
    # transaction would be nicer for lock duration, but the edges must be
    # visible to build_pool below, so each anchor commits as it completes.
    s2_by_id = {p.id: p.s2_paper_id for p in anchor_papers if p.id is not None}
    citations_by_id = {p.id: p.citation_count for p in anchor_papers if p.id is not None}

    for anchor_id in anchors:
        # **The cooperative-cancel checkpoint.** Between anchors, because that
        # is a boundary where nothing is half-written: each anchor commits as
        # it completes, so stopping here leaves the edges already fetched
        # stored and the ones not yet requested unspent.
        #
        # Checked per anchor rather than per request: the fetch is where the
        # API budget goes, and a cancel that only took effect at the end of the
        # run would stop nothing worth stopping.
        if result.expansion_id is not None:
            with engine.connect() as conn:
                if expansions_repo.is_cancelled(conn, session_id, result.expansion_id):
                    logger.info(
                        "expansion_cancelled_midrun session=%s job_id=%s anchors_done=%s",
                        session_id,
                        result.expansion_id,
                        anchors.index(anchor_id),
                    )
                    return _finish(engine, session_id, result, "cancelled")

        s2_id = s2_by_id.get(anchor_id)
        if s2_id is None:
            continue

        # Backward is always allowed: a reference list is bounded and
        # deliberate. Forward is skipped for hubs -- expanding forward from a
        # 191k-citation paper returns an arbitrary slice of a fifth of modern
        # ML. Same threshold build_pool uses, so the two agree.
        directions: list[tuple[str, int]] = [("BACKWARD", params.max_references)]
        if citations_by_id.get(anchor_id, 0) > filters_cfg.forward_expand_max:
            result.hub_skipped += 1
            logger.info(
                "HUB_SKIP_FORWARD paper_id=%s citations=%s",
                anchor_id,
                citations_by_id.get(anchor_id),
            )
        else:
            directions.append(("FORWARD", params.max_citations))

        for direction, limit in directions:
            if client.api_calls - calls_at_start >= params.api_call_budget:
                result.truncated = True
                result.error = f"api_call_budget of {params.api_call_budget} reached"
                break
            try:
                if direction == "BACKWARD":
                    edges = await client.get_references(s2_id, limit=limit)
                    result.backward_fetched += len(edges)
                else:
                    edges = await client.get_citations(s2_id, limit=limit)
                    result.forward_fetched += len(edges)
            except (S2TransientError, CacheMiss) as exc:
                # One direction failing degrades the run; it does not end it.
                logger.warning(
                    "ANCHOR_FETCH_FAILED paper_id=%s direction=%s: %s",
                    anchor_id,
                    direction,
                    exc,
                )
                continue

            with engine.begin() as conn:
                for edge in edges:
                    if edge.paper is None:
                        continue
                    # admit=False: this pass stores papers, edges and verdicts.
                    # Admission belongs to the budget step, so max_new is
                    # respected rather than every accepted paper landing in the
                    # graph.
                    ingest_neighbour(
                        conn,
                        session_id,
                        anchor_id,
                        edge.paper,
                        direction,
                        filters_cfg,
                        as_of_year,
                        result.ingest,
                        admit=False,
                        resolver=resolver,
                    )
        if result.truncated:
            break

    # The fetch is over, so what it cost is known and worth writing down before
    # the next stage starts. R2.4's poll derives its stage from which counters
    # are filled in, and this is what moves a job off FETCHING -- without it a
    # run would sit on one stage until it finished, which is a progress bar
    # that only ever reads 0% and then 100%.
    _record(engine, session_id, result, n_filtered=result.ingest.boundary)

    # (3)-(6) Pool, exclude, prescore. Ingest already ran the cascade, so
    # everything reachable here has been filtered.
    with engine.begin() as conn:
        pool = build_pool(conn, session_id, anchors, filters_cfg, as_of_year)
        result.n_pool = len(pool)
        result.n_filtered = result.ingest.boundary
        expansions_repo.record_progress(
            conn, session_id, result.expansion_id or 0, n_pool=result.n_pool
        )

        # (9)-(10) Rank by prescore, then allocate. R1 uses prescore as the
        # final ranking; real features arrive at R3.
        scored = [(entry, prescore(entry, filters_cfg)) for entry in pool]
        room = max(0, params.max_nodes - len(graph_repo.get_node_ids(conn, session_id)))
        selected = allocate(scored, ranking_cfg.budget, min(params.max_new, room))
        if room < params.max_new and pool:
            result.truncated = True
            result.error = result.error or f"max_nodes of {params.max_nodes} reached"

        # (11) Commit. add_node is idempotent, so a candidate already written by
        # ingest is updated rather than duplicated -- this pass attributes it to
        # the expansion and records its score.
        for entry, score in selected:
            graph_repo.add_node(
                conn,
                session_id,
                entry.paper_id,
                "CANDIDATE",
                depth=1,
                score=score,
                features={"anchor_overlap": entry.anchor_overlap},
                added_by=result.expansion_id,
            )
        result.added_paper_ids = sorted(entry.paper_id for entry, _ in selected)
        result.n_added = len(selected)
        expansions_repo.record_progress(
            conn, session_id, result.expansion_id or 0, n_added=result.n_added
        )

    result.api_calls = client.api_calls - calls_at_start
    result.cache_hits = client.cache_hits - hits_at_start

    # (12) POST-COMMIT: recompute features for the whole session (R3).
    #
    # The whole session, not only the new nodes -- every feature is a
    # rank-percentile *within the session*, so admitting one paper changes
    # where all the others sit. Scoring only the arrivals would leave the
    # existing graph ranked against a pool that no longer exists.
    #
    # Never fatal. The expansion has already committed; a feature pass that
    # fails must not turn a successful run into an error and lose the papers
    # it fetched. The scores stay stale until the next run or a PUT /api/config
    # and that is visible, where a lost expansion would not be.
    try:
        compute_and_store_features(engine, session_id, as_of_year)
    except Exception:  # noqa: BLE001 - a scoring failure must not cost the fetch
        logger.exception("feature_pass_failed session=%s", session_id)

    return _finish(engine, session_id, result, None)


def _record(
    engine: Engine, session_id: int, result: ExpansionResult, **counters: int | None
) -> None:
    """Write mid-run counters in their own transaction, if this run has a row."""
    if result.expansion_id is None:
        return
    with engine.begin() as conn:
        expansions_repo.record_progress(conn, session_id, result.expansion_id, **counters)


def _finish(
    engine: Engine, session_id: int, result: ExpansionResult, note: str | None
) -> ExpansionResult:
    """
    Always DONE. Truncation is recorded in `error`, never raised: a partial
    expansion is a success, and the caller keeps what was paid for.
    """
    if note and not result.error:
        result.error = note
    if result.expansion_id is not None:
        with engine.begin() as conn:
            expansions_repo.finish(
                conn,
                session_id,
                result.expansion_id,
                status="DONE",
                n_pool=result.n_pool,
                n_filtered=result.n_filtered,
                n_added=result.n_added,
                api_calls=result.api_calls,
                cache_hits=result.cache_hits,
                error=result.error,
            )
    return result


__all__ = ["ExpandParams", "ExpansionResult", "expand"]
