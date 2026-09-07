"""
expansion.py
------------
Two explicitly separate network expansions.

`expand_references(...)`
    Pulls the REFERENCES of the given seed/liked papers. Uncapped -- every
    reference that passes the admission filter is added, because the whole
    point of a seed is that its bibliography defines the field.

`expand_citations(...)`
    Pulls the CITATIONS (papers citing the given papers). This is the
    capped phase: the GUI's max-papers budget is split EQUALLY across the
    seed papers, and each seed's citing papers are ranked by
    graph_expander.citation_hybrid_score before the quota is applied.
    A seed with fewer citations than its quota simply leaves the remainder
    to the seeds that follow.

Shared behaviour:
  * Papers are processed FEWEST-NEIGHBOURS-FIRST, so sparsely connected
    corners of the graph get filled in before well-covered hubs.
  * Each paper is fetched at most once per run (and a paper already
    present in the store is not re-hydrated).
  * Candidate abstracts are hydrated in small batches through S2's
    /paper/batch endpoint, then every candidate must clear
    field_filter.is_core_ai_paper -- so applied papers that merely use AI
    as a tool never enter the network.
  * A failure on one paper is logged and skipped; it never aborts the run.
"""

from __future__ import annotations

import logging

from field_filter import is_core_ai_paper
from graph_expander import citation_hybrid_score
from openalex_client import OpenAlexClient
from semantic_scholar_client import (
    BATCH_SIZE,
    SemanticScholarClient,
    s2_record_to_kwargs,
)
from storage import Paper, Store

logger = logging.getLogger(__name__)

# S2 returns at most 1000 neighbours per request.
MAX_NEIGHBORS_PER_CALL = 1000


def order_by_fewest_neighbors(store: Store, paper_ids: list[str]) -> list[str]:
    """
    Sort paper_ids ascending by how many neighbours each already has, so
    the least-connected papers are expanded first. Unknown papers count
    as zero and therefore sort first.
    """
    counts: dict[str, int] = {}
    for citing, cited in store.all_edges():
        counts[citing] = counts.get(citing, 0) + 1
        counts[cited] = counts.get(cited, 0) + 1
    return sorted(paper_ids, key=lambda pid: counts.get(pid, 0))


def _dedup_preserving_order(paper_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for pid in paper_ids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _hydrate_abstracts(
    s2_client: SemanticScholarClient,
    paper_ids: list[str],
    batch_size: int,
) -> dict[str, str]:
    """
    Fetch abstracts for candidate ids in small batches. Batch failures are
    already isolated inside batch_get_papers, so a bad chunk just means
    those candidates get filtered on title+venue alone.
    """
    if not paper_ids:
        return {}
    try:
        records = s2_client.batch_get_papers(paper_ids, batch_size=batch_size)
    except Exception as exc:
        logger.warning("abstract hydration failed for %d ids: %s", len(paper_ids), exc)
        return {}
    abstracts: dict[str, str] = {}
    for rec in records or []:
        if rec and rec.get("paperId") and rec.get("abstract"):
            abstracts[rec["paperId"]] = rec["abstract"]
    return abstracts


def _admitted(records: list[dict], abstracts: dict[str, str]) -> tuple[list[dict], int]:
    """Split raw S2 records into (admitted, skipped_count) via the strict filter."""
    admitted: list[dict] = []
    skipped = 0
    for rec in records:
        pid = rec.get("paperId")
        if not pid:
            skipped += 1
            continue
        if is_core_ai_paper(
            title=rec.get("title"),
            venue=rec.get("venue"),
            abstract=abstracts.get(pid) or rec.get("abstract"),
            field_of_study=rec.get("fieldsOfStudy"),
        ):
            admitted.append(rec)
        else:
            skipped += 1
    return admitted, skipped


def _store_candidate(store: Store, rec: dict, abstract: str | None) -> str:
    kwargs = s2_record_to_kwargs(rec)
    if abstract and not kwargs.get("abstract"):
        kwargs["abstract"] = abstract
    return store.upsert_paper(Paper(**kwargs))


def expand_references(
    s2_client: SemanticScholarClient,
    oa_client: OpenAlexClient,
    store: Store,
    paper_ids: list[str],
    *,
    per_paper_limit: int = MAX_NEIGHBORS_PER_CALL,
    batch_size: int = BATCH_SIZE,
    max_nodes: int | None = None,
) -> dict:
    """
    Add every admissible reference of each paper in paper_ids. No per-run
    cap: a seed's bibliography goes in whole. `max_nodes`, when given, is
    a safety ceiling on the total size of the network.

    Returns {"added", "skipped", "processed", "failed"}.
    """
    result = {"added": 0, "skipped": 0, "processed": 0, "failed": 0}
    known = store.all_paper_ids()

    for paper_id in order_by_fewest_neighbors(store, _dedup_preserving_order(paper_ids)):
        if max_nodes is not None and len(known) >= max_nodes:
            logger.info("expand_references: max_nodes=%d reached", max_nodes)
            break
        try:
            raw = s2_client.get_references(paper_id, limit=per_paper_limit)
        except Exception as exc:
            logger.warning("expand_references: fetch failed for %s: %s", paper_id, exc)
            result["failed"] += 1
            continue

        result["processed"] += 1
        records = [r for r in (raw or []) if r.get("paperId")]
        # Only hydrate abstracts we don't already hold.
        need = [r["paperId"] for r in records if r["paperId"] not in known]
        abstracts = _hydrate_abstracts(s2_client, need, batch_size)

        admitted, skipped = _admitted(records, abstracts)
        result["skipped"] += skipped

        for rec in admitted:
            if max_nodes is not None and len(known) >= max_nodes:
                result["skipped"] += 1
                continue
            actual_id = _store_candidate(store, rec, abstracts.get(rec["paperId"]))
            store.add_edges(paper_id, [actual_id])
            if actual_id not in known:
                known.add(actual_id)
            result["added"] += 1

    logger.info("expand_references: %s", result)
    return result


def expand_citations(
    s2_client: SemanticScholarClient,
    oa_client: OpenAlexClient,
    store: Store,
    paper_ids: list[str],
    *,
    max_total: int,
    per_paper_limit: int = MAX_NEIGHBORS_PER_CALL,
    batch_size: int = BATCH_SIZE,
) -> dict:
    """
    Add citing papers, ranked by citation_hybrid_score, under a total
    budget of `max_total` split equally across paper_ids. A seed that
    cannot fill its share leaves the remainder for the seeds after it.

    Returns {"added", "skipped", "processed", "failed", "per_seed"}.
    """
    result = {"added": 0, "skipped": 0, "processed": 0, "failed": 0, "per_seed": {}}
    targets = order_by_fewest_neighbors(store, _dedup_preserving_order(paper_ids))
    if not targets or max_total <= 0:
        return result

    known = store.all_paper_ids()
    remaining_seeds = len(targets)
    budget_left = max_total

    for paper_id in targets:
        if budget_left <= 0:
            break
        # Equal share of what's still unspent; the last seed absorbs any
        # rounding remainder so the full budget can actually be used.
        quota = budget_left if remaining_seeds == 1 else max(1, budget_left // remaining_seeds)
        remaining_seeds -= 1

        try:
            raw = s2_client.get_citations(paper_id, limit=per_paper_limit)
        except Exception as exc:
            logger.warning("expand_citations: fetch failed for %s: %s", paper_id, exc)
            result["failed"] += 1
            continue

        result["processed"] += 1
        records = [r for r in (raw or []) if r.get("paperId")]
        need = [r["paperId"] for r in records if r["paperId"] not in known]
        abstracts = _hydrate_abstracts(s2_client, need, batch_size)

        admitted, skipped = _admitted(records, abstracts)
        result["skipped"] += skipped

        # Rank this seed's citing papers, best first, then take the quota.
        max_cit = max((r.get("citationCount") or 0 for r in admitted), default=1) or 1
        admitted.sort(
            key=lambda r: citation_hybrid_score(
                r.get("citationCount") or 0, max_cit,
                0, 1,          # network indegree is not yet known for a new candidate
                0, 1,          # h-index is unavailable on the citations endpoint
                r.get("venue"),
            ),
            reverse=True,
        )

        taken = 0
        for rec in admitted:
            if taken >= quota or budget_left <= 0:
                break
            actual_id = _store_candidate(store, rec, abstracts.get(rec["paperId"]))
            store.add_edges(actual_id, [paper_id])
            if actual_id not in known:
                known.add(actual_id)
            taken += 1
            budget_left -= 1
            result["added"] += 1

        result["per_seed"][paper_id] = taken

    logger.info("expand_citations: %s", result)
    return result
