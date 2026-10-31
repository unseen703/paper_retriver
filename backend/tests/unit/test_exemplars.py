# ruff: noqa: F811  -- the imported `conn` fixture is requested by parameter name
"""R6.16 -- exemplar centroids from the user's own decisions. Offline."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import Connection

from app.models import FilterDecision, Outcome
from app.repo import events as events_repo
from app.repo import filter_decisions
from app.repo import graph as graph_repo
from app.services.embeddings import EmbeddingStore
from app.services.exemplars import (
    MIN_EXEMPLARS,
    ExemplarIds,
    build_centroids,
    load_exemplar_ids,
)
from tests.unit.test_repo_graph_events import _paper, conn  # noqa: F401

CV = "cfg-1"


def _reject(conn: Connection, pid: int, reason: str, version: str = CV) -> None:
    d = FilterDecision(Outcome.REJECT, "TOPIC", reason, is_global=True)
    filter_decisions.record(conn, None, pid, d, version)


def test_core_is_seed_liked_and_restored_applied_is_global_rejections(conn: Connection) -> None:
    seed, liked, restored, plain, applied, dataset = (_paper(conn, f"p{i}") for i in range(6))
    graph_repo.add_node(conn, 1, seed, "SEED", 0)
    graph_repo.add_node(conn, 1, liked, "LIKED", 1)
    graph_repo.add_node(conn, 1, plain, "CANDIDATE", 1)
    graph_repo.add_node(conn, 1, restored, "CANDIDATE", 1)
    events_repo.append_event(conn, 1, restored, "REMOVED")
    events_repo.append_event(conn, 1, restored, "RESTORED")
    _reject(conn, applied, "CAT_PRIMARY_APPLIED")
    _reject(conn, dataset, "IS_DATASET")  # a rejection, but not evidence of "applied"

    ids = load_exemplar_ids(conn, 1, CV)
    assert ids.core == {seed, liked, restored}
    assert ids.applied == {applied}


def test_a_restored_paper_leaves_the_applied_set(conn: Connection) -> None:
    pid = _paper(conn, "x")
    _reject(conn, pid, "FIELD_NON_CS")
    events_repo.append_event(conn, 1, pid, "RESTORED")
    ids = load_exemplar_ids(conn, 1, CV)
    assert pid in ids.core and pid not in ids.applied


def test_a_rejection_from_another_config_version_is_not_evidence(conn: Connection) -> None:
    pid = _paper(conn, "x")
    _reject(conn, pid, "CAT_PRIMARY_APPLIED", version="old")
    assert load_exemplar_ids(conn, 1, CV).applied == frozenset()


def test_a_later_unlabel_removes_a_restore_vote(conn: Connection) -> None:
    pid = _paper(conn, "x")
    events_repo.append_event(conn, 1, pid, "RESTORED")
    events_repo.append_event(conn, 1, pid, "UNLABELED")
    assert load_exemplar_ids(conn, 1, CV).core == frozenset()


def test_exemplars_are_session_scoped(conn: Connection) -> None:
    pid = _paper(conn, "x")
    graph_repo.add_node(conn, 2, pid, "LIKED", 0)
    assert load_exemplar_ids(conn, 1, CV).core == frozenset()
    assert load_exemplar_ids(conn, 2, CV).core == {pid}


def _store(vectors: dict[int, list[float]]) -> EmbeddingStore:
    s = EmbeddingStore(dim=2)
    for pid, v in vectors.items():
        s.put(pid, v, f"h{pid}")
    return s


def test_centroids_by_hand() -> None:
    store = _store({1: [1, 0], 2: [1, 0], 3: [1, 0], 4: [0, 1], 5: [0, 1], 6: [0, 1]})
    ids = ExemplarIds(frozenset({1, 2, 3}), frozenset({4, 5, 6}))
    c = build_centroids(store, ids)
    assert c is not None
    assert (c.n_core, c.n_applied) == (3, 3)
    np.testing.assert_allclose(c.core, [1, 0], atol=1e-6)
    np.testing.assert_allclose(c.applied, [0, 1], atol=1e-6)


def test_too_few_vectors_means_no_centroids() -> None:
    store = _store({1: [1, 0], 2: [1, 0], 4: [0, 1], 5: [0, 1], 6: [0, 1]})
    ids = ExemplarIds(frozenset({1, 2, 3}), frozenset({4, 5, 6}))  # 3 has no vector
    assert build_centroids(store, ids) is None
    assert build_centroids(store, ids, min_exemplars=2) is not None
    assert MIN_EXEMPLARS == 3


def test_one_empty_class_means_no_centroids() -> None:
    store = _store({1: [1, 0], 2: [1, 0], 3: [1, 0]})
    assert build_centroids(store, ExemplarIds(frozenset({1, 2, 3}), frozenset())) is None


def test_deterministic() -> None:
    vecs = {i: [1.0, i / 7] for i in range(1, 7)}
    ids = ExemplarIds(frozenset({1, 2, 3}), frozenset({4, 5, 6}))
    a, b = build_centroids(_store(vecs), ids), build_centroids(_store(vecs), ids)
    assert a is not None and b is not None
    assert a.core.tobytes() == b.core.tobytes()
    assert a.applied.tobytes() == b.applied.tobytes()


@pytest.mark.parametrize("n", [0, 1])
def test_min_exemplars_boundary(n: int) -> None:
    store = _store({1: [1, 0], 2: [0, 1]})
    ids = ExemplarIds(frozenset({1}), frozenset({2}))
    assert (build_centroids(store, ids, min_exemplars=n) is not None) == (n <= 1)
