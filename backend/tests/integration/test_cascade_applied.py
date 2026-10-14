# ruff: noqa: F811  -- the imported `conn` fixture is requested by parameter name
"""R6.17 -- stage 3 wired into the cascade. Offline, synthetic 2-d vectors."""

from __future__ import annotations

import numpy as np
from sqlalchemy import Connection

from app.config import filters as cfg
from app.models import Outcome
from app.repo import filter_decisions
from app.services.embeddings import EmbeddingStore
from app.services.exemplars import AppliedStage, Centroids, build_applied_stage
from app.services.filters.cascade import run_cascade
from tests.integration.test_cascade import (  # noqa: F401
    AS_OF,
    SESSION,
    _decisions,
    _paper,
    _stored,
    conn,
)

CORE = np.array([1.0, 0.0], dtype=np.float32)
APPLIED = np.array([0.0, 1.0], dtype=np.float32)


def _stage(store: EmbeddingStore) -> AppliedStage:
    return AppliedStage(Centroids(CORE, APPLIED, 3, 3), store)


def _store(pid: int, vec: list[float]) -> EmbeddingStore:
    store = EmbeddingStore(dim=2)
    store.put(pid, vec, "h")
    return store


def test_a_paper_near_the_applied_centroid_is_quarantined_for_this_session(
    conn: Connection,
) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=_stage(_store(pid, [0.0, 1.0])))
    assert d.outcome is Outcome.QUARANTINE
    assert d.reason_code == "APPLIED_LIKELY_APPLIED"
    (row,) = _decisions(conn)
    assert row[1] == SESSION  # not NULL: it is the user's exemplars talking, not a global fact


def test_a_paper_near_the_core_centroid_is_accepted(conn: Connection) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=_stage(_store(pid, [1.0, 0.0])))
    assert d.outcome is Outcome.ACCEPT
    assert d.stage == "APPLIED"


def test_no_vector_means_stage_three_is_silent(conn: Connection) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=_stage(EmbeddingStore(dim=2)))
    assert d.outcome is Outcome.ACCEPT
    assert d.stage == "TOPIC"


def test_stage_three_never_sees_a_paper_an_earlier_stage_refused(conn: Connection) -> None:
    paper = _paper(year=2014)
    pid = _stored(conn, paper)
    d = run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=_stage(_store(pid, [0.0, 1.0])))
    assert d.reason_code == "PRE_ERA"


def test_a_stale_quarantine_does_not_outlive_the_evidence(conn: Connection) -> None:
    paper = _paper()
    pid = _stored(conn, paper)
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=_stage(_store(pid, [0.0, 1.0])))
    assert pid in filter_decisions.non_accepted_paper_ids(conn, SESSION, cfg.config_version)
    # Vectors gone (or too few exemplars): the old verdict must not keep excluding it.
    run_cascade(conn, SESSION, pid, paper, cfg, AS_OF, applied=None)
    assert pid not in filter_decisions.non_accepted_paper_ids(conn, SESSION, cfg.config_version)


def test_build_applied_stage_is_none_without_a_store_or_evidence(conn: Connection) -> None:
    assert build_applied_stage(conn, SESSION, None, cfg.config_version) is None
    assert build_applied_stage(conn, SESSION, EmbeddingStore(dim=2), cfg.config_version) is None
    pid = _stored(conn, _paper())
    # one vector, no exemplars: below MIN_EXEMPLARS for both classes
    assert build_applied_stage(conn, SESSION, _store(pid, [1.0, 0.0]), cfg.config_version) is None
