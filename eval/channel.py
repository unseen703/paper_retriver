"""
The embedding channel's justification (R6): how many benchmark ground-truth
papers does it recover that the citation graph cannot reach?

BUILD.md R6: "Report how many benchmark recoveries came only from this channel.
If that number is near zero, the release didn't earn its place -- say so."

`recovery` is **pure** over id sets. `channel_ids` is the thin adapter that
asks the embedding store for a case's neighbours and applies the same temporal
guard the graph pool gets, so the channel cannot see the future either.

"Graph-reachable" means *in the graph pool* (`ScoredPool.features`), not in its
top K: the question is whether the graph can surface the paper at all, which is
what the embedding channel is claimed to add.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from build_benchmark import BenchmarkCase
from sqlalchemy import Connection
from temporal import enforce_cutoff

from app.repo import papers as papers_repo
from app.services.embeddings import EmbeddingStore, embedding_candidates

#: Matches the largest cutoff in the recall metrics (Recall@50).
CHANNEL_K = 50


@dataclass(frozen=True, slots=True)
class ChannelRecovery:
    truth: int
    graph: int  # truth in the graph pool
    embedding: int  # truth in the embedding channel's top-K
    embedding_only: int  # embedding, and not in the graph pool
    both: int


def recovery(
    truth: Iterable[int], graph_pool: Iterable[int], embedding_ids: Iterable[int]
) -> ChannelRecovery:
    t = frozenset(truth)
    g = t & frozenset(graph_pool)
    e = t & frozenset(embedding_ids)
    return ChannelRecovery(
        truth=len(t),
        graph=len(g),
        embedding=len(e),
        embedding_only=len(e - g),
        both=len(e & g),
    )


def channel_ids(
    conn: Connection, store: EmbeddingStore, case: BenchmarkCase, k: int = CHANNEL_K
) -> list[int]:
    """The case's embedding neighbours that survive the temporal cutoff, best first."""
    near = [pid for pid, _ in embedding_candidates(store, case.seed_ids, k)]
    papers = {p.id: p for p in papers_repo.get_papers_by_ids(conn, near) if p.id is not None}
    dated = [
        (pid, papers[pid].publication_date or (str(papers[pid].year) if papers[pid].year else None))
        for pid in near
        if pid in papers
    ]
    visible = set(enforce_cutoff(dated, case.cutoff))
    return [pid for pid in near if pid in visible]


def summarize(per_case: Sequence[ChannelRecovery]) -> dict[str, object]:
    """Totals across cases, plus the share of recoveries only this channel made."""
    totals = {
        "truth": sum(r.truth for r in per_case),
        "graph": sum(r.graph for r in per_case),
        "embedding": sum(r.embedding for r in per_case),
        "embedding_only": sum(r.embedding_only for r in per_case),
        "both": sum(r.both for r in per_case),
    }
    found = totals["graph"] + totals["embedding_only"]
    return {
        **totals,
        "k": CHANNEL_K,
        "embedding_only_share_of_found": totals["embedding_only"] / found if found else 0.0,
    }


def not_run(reason: str) -> Mapping[str, str]:
    return {"status": "not_run", "reason": reason}
