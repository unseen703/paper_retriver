"""
citation_network.py
--------------------
Builds a directed citation graph and scores candidate papers.

Two graph-native bibliometric signals:
    - Bibliographic coupling: shared references between candidate and seed set
    - Co-citation: how many seeds directly cite (or are cited by) the candidate

Combined with cosine similarity in SPECTER2 embedding space when available.

compute_metrics() adds PageRank, in-degree from seeds, and co-citation counts
to the network_metrics table for downstream ML feature export.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import networkx as nx
import numpy as np

from storage import Paper, Store
from semantic_scholar_client import SemanticScholarClient, s2_record_to_kwargs

logger = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    paper: Paper
    network_score: float
    embedding_score: float
    combined_score: float
    reasons: list[str]


def _manual_pagerank(
    graph: nx.DiGraph, alpha: float = 0.85, max_iter: int = 200, tol: float = 1e-8
) -> dict:
    """
    Pure Python/NumPy power-iteration PageRank -- no scipy dependency.
    Used as a genuine fallback when nx.pagerank's scipy-backed
    implementation is unavailable, instead of silently substituting
    degree centrality (which ignores neighbor importance entirely).
    """
    n = graph.number_of_nodes()
    if n == 0:
        return {}
    nodes = list(graph.nodes())
    idx = {node: i for i, node in enumerate(nodes)}
    out_degree = {node: graph.out_degree(node) for node in nodes}

    rank = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        new_rank = np.full(n, (1.0 - alpha) / n)
        dangling_mass = sum(rank[idx[node]] for node in nodes if out_degree[node] == 0)
        new_rank += alpha * dangling_mass / n
        for node in nodes:
            od = out_degree[node]
            if od == 0:
                continue
            share = alpha * rank[idx[node]] / od
            for successor in graph.successors(node):
                new_rank[idx[successor]] += share
        if np.abs(new_rank - rank).sum() < tol:
            rank = new_rank
            break
        rank = new_rank
    return {node: float(rank[idx[node]]) for node in nodes}


def build_graph(store: Store) -> nx.DiGraph:
    g = nx.DiGraph()
    for pid in store.all_paper_ids():
        g.add_node(pid)
    for citing, cited in store.all_edges():
        g.add_edge(citing, cited)
    return g


def compute_metrics(store: Store, graph: nx.DiGraph, seed_ids: list[str]) -> None:
    """
    Compute and persist per-node graph metrics:
      - pagerank: NetworkX PageRank within the subgraph
      - in_degree: number of seed papers that have a direct edge to this node
      - co_citation: number of seed papers that share a direct citation link
                     (seed -> candidate or candidate -> seed)
    """
    if graph.number_of_nodes() == 0:
        return

    seed_set = set(seed_ids)

    try:
        pr = nx.pagerank(graph, alpha=0.85, max_iter=200)
    except (nx.PowerIterationFailedConvergence, ModuleNotFoundError):
        logger.warning("networkx PageRank unavailable (scipy missing or no convergence); using manual power-iteration PageRank.")
        pr = _manual_pagerank(graph, alpha=0.85, max_iter=200)

    seed_ref_sets: dict[str, set] = {
        sid: set(graph.successors(sid)) for sid in seed_set if sid in graph
    }

    all_pids = list(store.all_paper_ids())
    for pid in all_pids:
        pagerank_val = pr.get(pid, 0.0)

        # in_degree: how many seeds have a direct out-edge to this node
        in_deg = sum(1 for sid, refs in seed_ref_sets.items() if pid in refs)

        # co_citation: seeds that directly cite it + seeds it directly cites
        candidate_refs = set(graph.successors(pid)) if pid in graph else set()
        co_cit = in_deg + len(candidate_refs & seed_set)

        store.upsert_metrics(pid, pagerank_val, in_deg, co_cit)

    logger.info("Metrics computed for %d nodes.", len(all_pids))


def _cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom else 0.0


def score_candidates(
    store: Store,
    graph: nx.DiGraph,
    seed_papers: list[Paper],
    negative_papers: list[Paper],
) -> list[ScoredCandidate]:
    seed_ids = {p.paper_id for p in seed_papers}
    negative_ids = {p.paper_id for p in negative_papers}
    seed_reference_sets = {
        p.paper_id: set(graph.successors(p.paper_id))
        for p in seed_papers if p.paper_id in graph
    }

    seed_embeddings = [p.embedding for p in seed_papers if p.embedding]
    centroid = np.mean(np.array(seed_embeddings), axis=0).tolist() if seed_embeddings else None

    scored: list[ScoredCandidate] = []
    for candidate in store.unlabeled_candidates():
        if candidate.paper_id in seed_ids or candidate.paper_id in negative_ids:
            continue
        if candidate.paper_id not in graph:
            continue

        reasons = []

        cited_by_seeds = sum(
            1 for refs in seed_reference_sets.values() if candidate.paper_id in refs
        )
        candidate_refs = set(graph.successors(candidate.paper_id))
        cites_seeds = len(candidate_refs & seed_ids)
        co_citation = cited_by_seeds + cites_seeds
        if cited_by_seeds:
            reasons.append(f"cited by {cited_by_seeds} of your seed papers")
        if cites_seeds:
            reasons.append(f"cites {cites_seeds} of your seed papers")

        shared_refs = sum(len(refs & candidate_refs) for refs in seed_reference_sets.values())
        if shared_refs:
            reasons.append(f"shares {shared_refs} references with your seed papers")

        network_raw = 2 * co_citation + shared_refs
        network_score = 1 - np.exp(-network_raw / 4.0)

        embedding_score = 0.0
        if centroid is not None and candidate.embedding:
            embedding_score = max(0.0, _cosine(candidate.embedding, centroid))
            if embedding_score > 0.6:
                reasons.append(f"high semantic similarity ({embedding_score:.2f})")

        # Citation count as a normalised signal (log scale, cap at 1.0)
        cit_score = min(1.0, np.log1p(candidate.citation_count or 0) / np.log1p(10000))
        if (candidate.citation_count or 0) > 500:
            reasons.append(f"{candidate.citation_count} citations")

        combined = 0.50 * network_score + 0.35 * embedding_score + 0.15 * cit_score
        if not reasons:
            reasons.append("indirect link via citation graph")

        scored.append(ScoredCandidate(candidate, network_score, embedding_score, combined, reasons))

    scored.sort(key=lambda sc: sc.combined_score, reverse=True)
    return scored
