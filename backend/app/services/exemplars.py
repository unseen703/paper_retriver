"""
Exemplar centroids for filter stage 3, built from the user's own decisions.

PLAN.md E4: stage 3 compares a paper to a *core* and an *applied* centroid. The
exemplars are the decisions the user has already made, not a hand-curated list:

* **core**    -- SEED and LIKED papers, plus papers the user restored from the
                 drawer (restoring is a vote that the filter was wrong).
* **applied** -- papers carrying a global `CAT_PRIMARY_APPLIED` / `FIELD_NON_CS`
                 verdict. A paper in both sets is the user overruling the filter,
                 so the core vote wins and it leaves the applied set.

Only papers with a stored vector count. With fewer than `MIN_EXEMPLARS` of either
kind the answer is None, and the cascade stays silent -- two exemplars is an
anecdote, not a centroid.

`build_applied_stage` packages the centroids and the store for `run_cascade`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Connection

from app.repo import events, filter_decisions, graph
from app.services.embeddings import EmbeddingStore, centroid

#: Fewest vectors per class before a centroid is trusted. Untuned module constant,
#: not a YAML key: nothing tunes it yet (CLAUDE.md rule 8).
MIN_EXEMPLARS = 3

APPLIED_REASONS = ["CAT_PRIMARY_APPLIED", "FIELD_NON_CS"]


@dataclass(frozen=True)
class ExemplarIds:
    core: frozenset[int]
    applied: frozenset[int]


@dataclass(frozen=True)
class Centroids:
    core: NDArray[np.float32]
    applied: NDArray[np.float32]
    n_core: int
    n_applied: int


@dataclass(frozen=True)
class AppliedStage:
    """
    What stage 3 needs at call time: the exemplar centroids and the vector store.

    Optional on `run_cascade`. Without it -- or for a paper with no stored
    vector -- stage 3 does not run at all, rather than quarantining everything
    it cannot see: no vectors means no evidence, and no evidence is not a verdict.
    """

    centroids: Centroids
    store: EmbeddingStore


def load_exemplar_ids(conn: Connection, session_id: int, config_version: str) -> ExemplarIds:
    by_state = graph.get_nodes_by_state(conn, session_id)
    core = (
        by_state.get("SEED", set())
        | by_state.get("LIKED", set())
        | events.paper_ids_with_latest_event(conn, session_id, "RESTORED")
    )
    applied = filter_decisions.global_paper_ids_by_reason(conn, config_version, APPLIED_REASONS)
    return ExemplarIds(core=frozenset(core), applied=frozenset(applied - core))


def build_centroids(
    store: EmbeddingStore, ids: ExemplarIds, min_exemplars: int = MIN_EXEMPLARS
) -> Centroids | None:
    """Both centroids, or None when either class has too few stored vectors."""
    core_ids = sorted(i for i in ids.core if store.get(i) is not None)
    applied_ids = sorted(i for i in ids.applied if store.get(i) is not None)
    if len(core_ids) < min_exemplars or len(applied_ids) < min_exemplars:
        return None
    core = centroid(store, core_ids)
    applied = centroid(store, applied_ids)
    if core is None or applied is None:
        return None
    return Centroids(core, applied, len(core_ids), len(applied_ids))


def build_applied_stage(
    conn: Connection, session_id: int, store: EmbeddingStore | None, config_version: str
) -> AppliedStage | None:
    """Stage 3's inputs, or None when there is no store or too little evidence."""
    if store is None or not len(store):
        return None
    centroids = build_centroids(store, load_exemplar_ids(conn, session_id, config_version))
    return None if centroids is None else AppliedStage(centroids, store)


__all__ = [
    "APPLIED_REASONS",
    "MIN_EXEMPLARS",
    "Centroids",
    "ExemplarIds",
    "build_applied_stage",
    "build_centroids",
    "load_exemplar_ids",
]
