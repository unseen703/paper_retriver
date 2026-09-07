"""
The `filter_decisions` table. **Scope-dependent** -- `session_id` is nullable.

This is the subtle corner of the session_id contract, and PLAN.md spells out
why. Verdicts come in two kinds:

* **Global** -- `PRE_ERA`, `IS_DATASET`, `CAT_PRIMARY_APPLIED`, `FIELD_NON_CS`.
  A 2013 dataset paper is a 2013 dataset paper in every session forever. These
  are written with `session_id = NULL` and cached: on a mature corpus, reusing
  them skips most of the filter work, and session 2 pays nothing for what
  session 1 already decided.
* **Session** -- `ALREADY_IN_GRAPH`, `REMOVED_TOMBSTONE`,
  `BELOW_SCORE_THRESHOLD`. Facts about one graph, never reusable.

A cached verdict is only valid for the `config_version` that produced it.
Moving a threshold in `filters.yaml` must re-open every decision it could have
changed, so the version is part of the lookup key rather than merely recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from app.models import FilterDecision, Outcome


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def record(
    conn: Connection,
    session_id: int | None,
    paper_id: int,
    decision: FilterDecision,
    config_version: str,
) -> None:
    """
    Persist one verdict, replacing any prior verdict for the same paper at the
    same scope and config version.

    Replace rather than append: re-running the cascade after a config change is
    normal, and appending would grow the table without bound while leaving two
    contradictory rows for the same question.
    """
    scope = None if decision.is_global else session_id
    conn.execute(
        text(
            "DELETE FROM filter_decisions"
            " WHERE paper_id = :paper_id AND config_version = :config_version"
            "   AND ((:scope IS NULL AND session_id IS NULL)"
            "        OR session_id = :scope)"
        ),
        {"paper_id": paper_id, "config_version": config_version, "scope": scope},
    )
    conn.execute(
        text(
            "INSERT INTO filter_decisions"
            " (paper_id, session_id, outcome, stage, reason_code, details,"
            "  config_version, decided_at)"
            " VALUES (:paper_id, :session_id, :outcome, :stage, :reason_code, :details,"
            "  :config_version, :decided_at)"
        ),
        {
            "paper_id": paper_id,
            "session_id": scope,
            "outcome": decision.outcome.value,
            "stage": str(decision.stage),
            "reason_code": decision.reason_code,
            "details": json.dumps(decision.details) if decision.details else None,
            "config_version": config_version,
            "decided_at": _utcnow(),
        },
    )


def find_global_rejection(
    conn: Connection, paper_id: int, config_version: str
) -> FilterDecision | None:
    """
    A cached global REJECT for this paper under this config, if one exists.

    Only rejections are cached. An ACCEPT must be re-evaluated every time,
    because a config change can turn it into a rejection and a stale accept
    would silently readmit a paper the new rules exclude.
    """
    row = conn.execute(
        text(
            "SELECT outcome, stage, reason_code, details FROM filter_decisions"
            " WHERE paper_id = :paper_id AND session_id IS NULL"
            "   AND config_version = :config_version AND outcome = 'REJECT'"
            " ORDER BY id DESC LIMIT 1"
        ),
        {"paper_id": paper_id, "config_version": config_version},
    ).fetchone()
    if row is None:
        return None
    details = {}
    if row[3]:
        try:
            details = json.loads(row[3])
        except (json.JSONDecodeError, TypeError):
            details = {}
    return FilterDecision(
        outcome=Outcome(row[0]),
        stage=row[1],
        reason_code=row[2],
        details=details,
        is_global=True,
    )


def non_accepted_paper_ids(conn: Connection, session_id: int) -> set[int]:
    """
    Papers this session must not admit: anything whose latest verdict is not
    ACCEPT, in either scope.

    This is the third clause of BUILD.md's stage-4 exclusion query, alongside
    "already in the graph" and "tombstoned". Leaving it out lets a rejected
    paper back into the candidate pool, because a rejection stores a decision
    row but no graph node -- so nothing else would exclude it.

    QUARANTINE is excluded too: it means "hold for review" (R2.14), not
    "admit quietly".
    """
    rows = conn.execute(
        text(
            "SELECT paper_id FROM filter_decisions"
            " WHERE outcome != 'ACCEPT'"
            "   AND (session_id IS NULL OR session_id = :session_id)"
        ),
        {"session_id": session_id},
    )
    return {row[0] for row in rows}


def count_by_reason(conn: Connection, session_id: int | None = None) -> list[tuple[str, str, int]]:
    """(outcome, reason_code, n) for R2.14's review drawer."""
    rows = conn.execute(
        text(
            "SELECT outcome, reason_code, COUNT(*) FROM filter_decisions"
            " WHERE (:session_id IS NULL OR session_id = :session_id OR session_id IS NULL)"
            " GROUP BY outcome, reason_code ORDER BY COUNT(*) DESC, reason_code"
        ),
        {"session_id": session_id},
    )
    return [(r[0], r[1], r[2]) for r in rows]


__all__ = [
    "count_by_reason",
    "find_global_rejection",
    "non_accepted_paper_ids",
    "record",
]
