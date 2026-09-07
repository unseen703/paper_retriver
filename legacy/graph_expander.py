"""
graph_expander.py
-----------------
Multi-hop BFS citation graph expander.

Strategy
--------
Starting from seed paper IDs, expand outward N hops in both citation directions
(references the paper cites + papers that cite it). A priority queue keeps the
expansion focused on papers that are relevant to multiple seeds or have high
citation counts, preventing runaway growth.

S2 is the primary data source. When S2 returns no results for a paper, we fall
back to OpenAlex using the paper's DOI (from externalIds).

Deduplication uses the canonical S2 paperId (or OpenAlex W-ID for OA-only papers).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Optional

from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs
from openalex_client import OpenAlexClient
from storage import Paper, Store
from venue_config import venue_score
from field_filter import is_relevant_paper

logger = logging.getLogger(__name__)

# Hybrid-score weights for ranking papers that CITE a paper already in the
# network (citation-direction BFS candidates only -- reference-direction
# candidates keep the equal-weight/2026 formula below). Listed in the
# priority order requested: citation count > network indegree/relevance >
# author h-index > venue.
_CITE_CIT_W = 0.4
_CITE_INDEGREE_W = 0.3
_CITE_HINDEX_W = 0.2
_CITE_VENUE_W = 0.1


def citation_hybrid_score(
    citation_count: int,
    max_citation_count: int,
    indegree: int,
    max_indegree: int,
    hindex: int,
    max_hindex: int,
    venue: str | None,
) -> float:
    """
    Hybrid relevance score for a paper CITING a paper already in the
    network, combining -- in the explicit priority order requested --
    citation count (0.4), network indegree / relevance to papers already
    in the network (0.3), representative author h-index (0.2), and venue
    tier (0.1). Each raw feature (except venue, already in [0,1] via
    venue_score) is normalized against the max value among competing
    candidates in the same BFS wave.
    """
    cit = citation_count / max(max_citation_count, 1)
    ind = indegree / max(max_indegree, 1)
    hi = hindex / max(max_hindex, 1)
    ven = venue_score(venue)
    return (
        _CITE_CIT_W * cit
        + _CITE_INDEGREE_W * ind
        + _CITE_HINDEX_W * hi
        + _CITE_VENUE_W * ven
    )


# Papers whose own references are always added in full, regardless of
# max_new -- seeds, liked papers, and GUI-added papers (which are stored
# with label "seed" by /api/paper/add-by-title). Checked against the
# paper's CURRENT store label rather than the seed_ids parameter, so this
# applies even when such a paper is discovered mid-BFS (e.g. a paper the
# user already liked in a prior session, reached as a reference of an
# unrelated seed) and not just to the literal seed_ids passed in.
_UNCAPPED_REF_LABELS = {"seed", "liked"}


def _bypasses_ref_cap(paper_id: str, store: Store) -> bool:
    paper = store.get_paper(paper_id)
    return paper is not None and paper.label in _UNCAPPED_REF_LABELS


def _representative_hindex(record: dict) -> int:
    """Max h-index among a raw S2 record's authors (0 if unavailable)."""
    authors = record.get("authors") or []
    values = [
        a.get("hIndex") for a in authors
        if isinstance(a, dict) and a.get("hIndex") is not None
    ]
    return max(values) if values else 0


def expand_network(
    s2_client: SemanticScholarClient,
    oa_client: OpenAlexClient,
    store: Store,
    seed_ids: list[str],
    *,
    hops: int = 2,
    max_nodes: int = 10000,
    max_new: int = 300,
    per_paper_limit: int = 500,
) -> int:
    """
    BFS from seed_ids, expanding references and citations up to `hops` levels.

    Reference-direction candidates keep the equal-weight (citation/venue/
    connectivity) formula, with a reduced citation weight for 2026 papers.
    Citation-direction candidates (papers citing a paper already in the
    network) are ranked instead by citation_hybrid_score(): citation count,
    network indegree, author h-index, venue -- for every node's citations,
    not just seeds'.

    A seed, liked, or GUI-added paper's own references are always added in
    full, regardless of max_new AND max_nodes -- checked against the
    paper's current store label, so this applies whether it was passed in
    seed_ids or discovered mid-BFS. Its citations remain subject to both
    caps like any other node. Both caps are enforced per-candidate (a
    capped candidate is skipped, not a reason to abandon the whole BFS),
    so one seed's large reference list can never prevent a later-queued
    seed from getting its own references processed.

    max_new  — hard cap on papers added in this single expansion run (default 300),
               bypassed only for a seed/liked/GUI-added paper's own references.
    max_nodes — hard ceiling on the total graph size for non-bypass candidates;
                bypassed only for a seed/liked/GUI-added paper's own references.

    Returns the number of new papers added.
    """
    seed_set = set(seed_ids)
    existing = store.all_paper_ids()
    initial_count = len(existing)
    current_total = initial_count  # maintained as a counter to avoid per-iteration DB call

    # BFS state: (paper_id, hop_distance, set_of_seed_ids_that_connect_here)
    queue: deque[tuple[str, int, set]] = deque()
    for sid in seed_ids:
        queue.append((sid, 0, {sid}))
        store.update_distance(sid, 0, list({sid}))

    visited: set[str] = set(seed_ids)

    # Track which seeds can reach each node (for distance/seed_connections updates)
    seed_connections: dict[str, set[str]] = defaultdict(set)
    for sid in seed_ids:
        seed_connections[sid] = {sid}

    # Track citation counts, venues, years, author h-index and network
    # indegree of discovered papers for BFS prioritization. network_indegree
    # counts edges (either direction) to papers already in the network,
    # accumulated across the whole run.
    citation_counts: dict[str, int] = {}
    years: dict[str, int | None] = {}
    author_hindex: dict[str, int] = {}
    network_indegree: dict[str, int] = defaultdict(int)
    directions: dict[str, str] = {}

    while queue:
        paper_id, dist, connected_seeds = queue.popleft()

        if dist >= hops:
            continue

        bypasses_cap = _bypasses_ref_cap(paper_id, store)
        newly_added = current_total - initial_count
        if not bypasses_cap and (current_total >= max_nodes or newly_added >= max_new):
            # Skip this one non-seed/liked candidate, but keep the loop
            # running -- a hard break here would abandon everything still
            # queued, including seeds that haven't had a chance yet to add
            # their own (always-uncapped) references. That was the bug:
            # an early seed's references alone could exhaust the cap and
            # leave every later-queued seed with zero edges at all.
            logger.debug(
                "Skipping capped node %s (new=%d/%d, total=%d/%d).",
                paper_id, newly_added, max_new, current_total, max_nodes,
            )
            continue

        refs, cites, hindex_by_id = _fetch_neighbors(
            paper_id, s2_client, oa_client, store, per_paper_limit
        )

        ref_items = [(r, "ref") for r in refs]
        cite_items = [(c, "cite") for c in cites]

        # Pre-sort candidates by provisional priority so the mid-wave cap hits
        # lowest-priority papers first — ensures max_new=1 inserts the best paper.
        _eq3 = 1.0 / 3.0
        _cit26 = _eq3 * 0.8
        _oth26 = (1.0 - _cit26) / 2.0
        _conn_pre = len(connected_seeds) / max(len(seed_ids), 1)
        _pmax = max((r[0].get("citation_count", 0) or 0 for r in ref_items), default=1) or 1

        _cmax_cit = max(
            (c[0].get("citation_count", 0) or 0 for c in cite_items), default=1
        ) or 1
        _cmax_hindex = max(
            (hindex_by_id.get(c[0].get("paper_id"), 0) for c in cite_items), default=1
        ) or 1
        _cmax_indegree = max(
            (network_indegree.get(c[0].get("paper_id"), 0) for c in cite_items), default=1
        ) or 1

        def _pre_pri(item):
            rec, direction = item
            if direction == "cite":
                pid = rec.get("paper_id")
                return citation_hybrid_score(
                    rec.get("citation_count", 0) or 0, _cmax_cit,
                    network_indegree.get(pid, 0), _cmax_indegree,
                    hindex_by_id.get(pid, 0), _cmax_hindex,
                    rec.get("venue"),
                )
            c = (rec.get("citation_count", 0) or 0) / _pmax
            v = venue_score(rec.get("venue"))
            y = rec.get("year") or 0
            if y >= 2026:
                return _cit26 * c + _oth26 * v + _oth26 * _conn_pre
            return _eq3 * c + _eq3 * v + _eq3 * _conn_pre

        if bypasses_cap:
            # Process ALL of this paper's references before any of its
            # citations, so an uncapped reference is never skipped because
            # the loop already broke out on a capped citation earlier in a
            # combined, score-sorted order.
            ref_items.sort(key=_pre_pri, reverse=True)
            cite_items.sort(key=_pre_pri, reverse=True)
            _candidates = ref_items + cite_items
        else:
            _candidates = sorted(ref_items + cite_items, key=_pre_pri, reverse=True)

        new_ids: list[str] = []
        venues: dict[str, str | None] = {}

        for record, direction in _candidates:
            pid = record.get("paper_id")
            if not pid:
                continue

            # Skip papers outside the allowed CS/AI/ML/NLP domain
            fields = record.get("field_of_study") or []
            if not is_relevant_paper(fields):
                logger.debug("Skipping out-of-domain paper %s (fields=%s)", pid, fields)
                continue

            paper = Paper(**record)
            # upsert_paper returns the canonical id (may differ if DOI-deduped)
            actual_pid = store.upsert_paper(paper)
            citation_counts[actual_pid] = record.get("citation_count", 0)
            venues[actual_pid] = record.get("venue")
            years[actual_pid] = record.get("year")
            author_hindex[actual_pid] = hindex_by_id.get(pid, 0)

            # Persist edge using the canonical id to avoid orphans
            if direction == "ref":
                store.add_edges(paper_id, [actual_pid])
            else:
                store.add_edges(actual_pid, [paper_id])
            network_indegree[actual_pid] += 1

            # Propagate seed connections under the canonical id
            new_connections = connected_seeds | seed_connections.get(actual_pid, set())
            seed_connections[actual_pid] = new_connections

            new_dist = dist + 1
            store.update_distance(actual_pid, new_dist, list(new_connections))

            if actual_pid not in visited:
                visited.add(actual_pid)
                new_ids.append(actual_pid)
                directions[actual_pid] = direction
                # Only count truly new DB rows (canonical id not in initial snapshot)
                if actual_pid not in existing:
                    current_total += 1

            # Mid-wave cap check — a seed/liked/GUI-added paper's own
            # references are never evicted by either cap (max_new or
            # max_nodes); its citations and any non-bypass candidate
            # remain subject to both.
            is_uncapped = bypasses_cap and direction == "ref"
            if not is_uncapped:
                if current_total >= max_nodes:
                    break
                if (current_total - initial_count) >= max_new:
                    break

        # Reference priority: equal 1/3 each; 2026 papers get 20% less citation weight.
        # Citation priority: hybrid score (citation count, network indegree,
        # author h-index, venue), weighted 0.4/0.3/0.2/0.1.
        _eq = 1.0 / 3.0
        _cit_2026 = _eq * 0.8
        _other_2026 = (1.0 - _cit_2026) / 2.0
        max_cit = max(
            (citation_counts.get(p, 0) for p in new_ids if directions.get(p) != "cite"),
            default=1,
        ) or 1
        n_seeds = max(len(seed_ids), 1)

        cite_new_ids = [p for p in new_ids if directions.get(p) == "cite"]
        _wave_cmax_cit = max((citation_counts.get(p, 0) for p in cite_new_ids), default=1) or 1
        _wave_cmax_hindex = max((author_hindex.get(p, 0) for p in cite_new_ids), default=1) or 1
        _wave_cmax_indegree = max((network_indegree.get(p, 0) for p in cite_new_ids), default=1) or 1

        def _priority(p: str) -> float:
            if directions.get(p) == "cite":
                return citation_hybrid_score(
                    citation_counts.get(p, 0), _wave_cmax_cit,
                    network_indegree.get(p, 0), _wave_cmax_indegree,
                    author_hindex.get(p, 0), _wave_cmax_hindex,
                    venues.get(p),
                )
            cit = citation_counts.get(p, 0) / max_cit
            v = venue_score(venues.get(p))
            conn = len(seed_connections.get(p, set())) / n_seeds
            if (years.get(p) or 0) >= 2026:
                return _cit_2026 * cit + _other_2026 * v + _other_2026 * conn
            return _eq * cit + _eq * v + _eq * conn

        new_ids.sort(key=_priority, reverse=True)
        for pid in new_ids:
            queue.append((pid, dist + 1, seed_connections[pid]))

    added = current_total - initial_count
    logger.info("Graph expansion done. Added %d papers (total %d).", added, current_total)
    return added


# ---- neighbors fetching -------------------------------------------------------

def _fetch_neighbors(
    paper_id: str,
    s2: SemanticScholarClient,
    oa: OpenAlexClient,
    store: Store,
    limit: int,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """
    Fetch references and citations for paper_id.
    Returns (refs_kwargs_list, citations_kwargs_list, hindex_by_paper_id).
    Each element of the first two lists is a dict suitable for Paper(**...).
    hindex_by_paper_id maps each candidate's raw paperId to its representative
    (max) author h-index, extracted before s2_record_to_kwargs strips author
    details down to plain names.
    Falls back to OpenAlex if S2 returns nothing and we have a DOI (OpenAlex
    has no h-index data, so those candidates get 0).
    """
    refs: list[dict] = []
    cites: list[dict] = []
    hindex_by_id: dict[str, int] = {}

    # S2 references
    try:
        raw_refs = s2.get_references(paper_id, limit=limit)
        for r in raw_refs:
            if r.get("paperId"):
                hindex_by_id[r["paperId"]] = _representative_hindex(r)
        refs = [s2_record_to_kwargs(r) for r in raw_refs if r.get("paperId")]
    except Exception as exc:
        logger.warning("S2 references failed for %s: %s", paper_id, exc)

    # S2 citations
    try:
        raw_cites = s2.get_citations(paper_id, limit=limit)
        for c in raw_cites:
            if c.get("paperId"):
                hindex_by_id[c["paperId"]] = _representative_hindex(c)
        cites = [s2_record_to_kwargs(c) for c in raw_cites if c.get("paperId")]
    except Exception as exc:
        logger.warning("S2 citations failed for %s: %s", paper_id, exc)

    # Sort by citation count descending so high-impact papers are processed first
    # (critical when max_new cap fires mid-wave — ensures we add the best papers)
    refs.sort(key=lambda r: r.get("citation_count", 0) or 0, reverse=True)
    cites.sort(key=lambda r: r.get("citation_count", 0) or 0, reverse=True)

    # OpenAlex fallback: if S2 gave nothing, try via DOI
    if not refs and not cites:
        stored = store.get_paper(paper_id)
        doi = stored.doi if stored else None
        if doi:
            refs, cites = _oa_neighbors(doi, oa)

    return refs, cites, hindex_by_id


def _oa_neighbors(
    doi: str, oa: OpenAlexClient
) -> tuple[list[dict], list[dict]]:
    work = oa.get_work_by_doi(doi)
    if not work:
        return [], []
    oa_id = work.get("id", "").split("/")[-1]

    refs: list[dict] = []
    for ref_work in oa.get_references(work):
        kwargs = oa.to_paper_kwargs(ref_work)
        if kwargs.get("paper_id"):
            refs.append(kwargs)

    cites: list[dict] = []
    for cite_work in oa.get_citations(oa_id):
        kwargs = oa.to_paper_kwargs(cite_work)
        if kwargs.get("paper_id"):
            cites.append(kwargs)

    return refs, cites


def hydrate_ids(
    ids: list[str],
    s2: SemanticScholarClient,
    store: Store,
) -> int:
    """
    Batch-fetch full metadata for a list of paper IDs using S2's batch endpoint.
    Updates papers already in store with any new fields (abstract, embeddings, etc.).
    Returns count of papers successfully hydrated.
    """
    if not ids:
        return 0
    try:
        records = s2.batch_get_papers(ids)
    except Exception as exc:
        logger.warning("hydrate_ids: batch fetch failed for %d id(s): %s", len(ids), exc)
        return 0
    count = 0
    for record in records:
        if not record or not record.get("paperId"):
            continue
        kwargs = s2_record_to_kwargs(record)
        store.upsert_paper(Paper(**kwargs))
        count += 1
    return count
