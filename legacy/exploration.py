"""
exploration.py
---------------
Full single-node exploration, decoupled from graph_expander.py's
priority-capped multi-hop BFS traversal.

graph_expander.py answers "which papers should the frontier visit next,
and how many can be added before a cap fires" -- a breadth-first search
across many nodes at once, deliberately capped (max_new, max_nodes,
per_paper_limit) to keep an initial network build tractable.

This module answers a narrower question: "given ONE paper the user has
flagged as relevant (liked), fetch its ENTIRE direct citation
neighborhood -- every reference and every citing paper -- and add the
on-topic ones to the network." There is no frontier, no priority queue,
and no node cap; the only filter is field-of-study relevance.

Triggered automatically wherever a paper's label becomes "liked":
  - graph_viz.py:api_set_label (GUI)
  - recommend.py:run_feedback_session (CLI)
"""

from __future__ import annotations

import logging

from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs
from openalex_client import OpenAlexClient
from storage import Paper, Store
from field_filter import is_relevant_paper
from graph_expander import _oa_neighbors

logger = logging.getLogger(__name__)

# Semantic Scholar's /references and /citations endpoints cap at 1000
# results per request (SemanticScholarClient has no pagination beyond
# that). This is the largest single-page fetch the API allows -- as
# close to "every reference" as one call can get.
MAX_NEIGHBORS_PER_CALL = 1000


def explore_paper(
    paper_id: str,
    s2_client: SemanticScholarClient,
    oa_client: OpenAlexClient,
    store: Store,
    *,
    max_nodes: int = 10000,
) -> dict:
    """
    Fully explore paper_id's direct citation neighborhood:
      - fetch every reference (paper_id cites them) -> edge paper_id -> ref
      - fetch every citing paper (they cite paper_id) -> edge citer -> paper_id
      - drop any candidate that fails is_relevant_paper() (out-of-domain field)
      - fall back to OpenAlex (by DOI) when S2 has no coverage at all,
        same as graph_expander's multi-hop BFS does
      - propagate distance_from_seed / seed_connections from paper_id to
        each newly-discovered neighbor, so exploration-added papers are
        scored consistently with BFS-added ones

    A fetch failure for either side is logged and treated as zero results
    for that side; it never raises. max_nodes is a hard ceiling on total
    network size (matching expand_network's default) so repeated "like"
    actions can't grow the network without bound.

    Returns {"refs_added", "refs_skipped", "cites_added", "cites_skipped"}.
    """
    result = {"refs_added": 0, "refs_skipped": 0, "cites_added": 0, "cites_skipped": 0}

    try:
        raw_refs = s2_client.get_references(paper_id, limit=MAX_NEIGHBORS_PER_CALL)
    except Exception as exc:
        logger.warning("explore_paper: references fetch failed for %s: %s", paper_id, exc)
        raw_refs = []

    try:
        raw_cites = s2_client.get_citations(paper_id, limit=MAX_NEIGHBORS_PER_CALL)
    except Exception as exc:
        logger.warning("explore_paper: citations fetch failed for %s: %s", paper_id, exc)
        raw_cites = []

    ref_kwargs = [s2_record_to_kwargs(r) for r in raw_refs if r.get("paperId")]
    cite_kwargs = [s2_record_to_kwargs(c) for c in raw_cites if c.get("paperId")]

    if not ref_kwargs and not cite_kwargs:
        stored = store.get_paper(paper_id)
        doi = stored.doi if stored else None
        if doi:
            ref_kwargs, cite_kwargs = _oa_neighbors(doi, oa_client)

    known_ids = store.all_paper_ids()
    current_total = len(known_ids)

    # New neighbors build on paper_id's own position in the network rather
    # than assuming it's a seed at distance 0.
    liked = store.get_paper(paper_id)
    base_distance = liked.distance_from_seed if (liked and liked.distance_from_seed is not None) else 0
    base_connections = list(liked.seed_connections) if (liked and liked.seed_connections) else [paper_id]
    new_distance = base_distance + 1

    for kwargs in ref_kwargs:
        if not is_relevant_paper(kwargs.get("field_of_study") or []):
            result["refs_skipped"] += 1
            continue
        if current_total >= max_nodes:
            result["refs_skipped"] += 1
            continue
        actual_id = store.upsert_paper(Paper(**kwargs))
        store.add_edges(paper_id, [actual_id])
        store.update_distance(actual_id, new_distance, base_connections)
        if actual_id not in known_ids:
            known_ids.add(actual_id)
            current_total += 1
        result["refs_added"] += 1

    for kwargs in cite_kwargs:
        if not is_relevant_paper(kwargs.get("field_of_study") or []):
            result["cites_skipped"] += 1
            continue
        if current_total >= max_nodes:
            result["cites_skipped"] += 1
            continue
        actual_id = store.upsert_paper(Paper(**kwargs))
        store.add_edges(actual_id, [paper_id])
        store.update_distance(actual_id, new_distance, base_connections)
        if actual_id not in known_ids:
            known_ids.add(actual_id)
            current_total += 1
        result["cites_added"] += 1

    logger.info("explore_paper(%s): %s", paper_id, result)
    return result
