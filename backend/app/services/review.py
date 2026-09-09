"""
What the graph is *not* showing you, and why (R2.14).

PLAN.md M5 makes the case in one sentence: "A filter you cannot audit is a
filter you cannot tune, and you will silently discard good papers for weeks
without noticing." This is the audit.

Three tabs, three genuinely different kinds of absence:

    Quarantined  `outcome = QUARANTINE` -- "probably applied ML, but I am not
                 sure". PLAN.md notes most of the hard cases land here, which
                 makes them the papers most worth a human glance and the worst
                 ones to bury under a pile of confident rejections.
    Rejected     `outcome = REJECT` -- the cascade said no and gave a reason.
    Removed      the latest event is a tombstone -- either you said no, or the
                 sweep collected the paper as a consequence of something you
                 said no to.

**Counts are complete; rows are a capped sample.** A mature corpus rejects
thousands of papers, and materialising all of them to render a drawer nobody
scrolls to the bottom of would make this endpoint slowest exactly when the
graph is most interesting. BUILD.md's verification is about the counts, so the
counts are whole and the rows carry a `truncated` flag rather than leaving the
caller to guess from a suspiciously round number.

**Removed groups by event type rather than by a reason code**, because that is
the distinction that actually exists there: `REMOVED` is a decision you made,
`GC_SWEPT` is one the system derived from it. R2.6 keeps them apart in its own
response for the same reason -- merging them answers "why is this gone?" with
"it is gone".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.repo import events as events_repo

#: Rows per tab when the caller does not say. Enough to scan and act on;
#: small enough that opening the drawer on a large corpus stays instant.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class ReviewPaper:
    """One row: enough to read it and decide, nothing more."""

    paper_id: int
    title: str
    reason_code: str
    stage: str
    year: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewBucket:
    """One tab. `total` and `by_reason` are complete even when `papers` is not."""

    total: int = 0
    by_reason: list[tuple[str, int]] = field(default_factory=list)
    papers: list[ReviewPaper] = field(default_factory=list)
    truncated: bool = False


def _filter_bucket(conn: Connection, session_id: int, outcome: str, limit: int) -> ReviewBucket:
    """
    One tab from `filter_decisions`.

    The scope clause matches the table's own contract: a verdict with
    `session_id IS NULL` is global -- a 2013 dataset paper is one in every
    session -- while a session-scoped row belongs only to its own graph.
    """
    scope = " AND (fd.session_id IS NULL OR fd.session_id = :sid)"

    counts = conn.execute(
        text(
            "SELECT fd.reason_code, COUNT(*) FROM filter_decisions fd"
            f" WHERE fd.outcome = :outcome{scope}"
            " GROUP BY fd.reason_code ORDER BY COUNT(*) DESC, fd.reason_code"
        ),
        {"outcome": outcome, "sid": session_id},
    ).fetchall()
    by_reason = [(str(code), int(n)) for code, n in counts]
    total = sum(n for _, n in by_reason)

    rows = conn.execute(
        text(
            "SELECT fd.paper_id, p.title, fd.reason_code, fd.stage, p.year"
            " FROM filter_decisions fd JOIN papers p ON p.id = fd.paper_id"
            f" WHERE fd.outcome = :outcome{scope}"
            # Ordered so two calls over an unchanged corpus agree
            # (CLAUDE.md rule 7), and newest-first so the rows a reader sees
            # are the ones the most recent expansion produced.
            " ORDER BY fd.id DESC LIMIT :limit"
        ),
        {"outcome": outcome, "sid": session_id, "limit": limit},
    )
    papers = [
        ReviewPaper(
            paper_id=int(pid),
            title=str(title),
            reason_code=str(code),
            stage=str(stage),
            year=int(year) if year is not None else None,
        )
        for pid, title, code, stage, year in rows
    ]
    return ReviewBucket(
        total=total, by_reason=by_reason, papers=papers, truncated=total > len(papers)
    )


def _removed_bucket(conn: Connection, session_id: int, limit: int) -> ReviewBucket:
    """
    Papers whose latest event is a tombstone.

    Derived from the log rather than from a flag, the same way
    `events.removed_paper_ids` does it -- which is what makes a `RESTORED`
    event drop a paper out of this tab with nothing to remember to clear.
    """
    placeholders = ",".join(f":t{i}" for i in range(len(events_repo.TOMBSTONE_EVENTS)))
    params: dict[str, object] = {
        "sid": session_id,
        **{f"t{i}": v for i, v in enumerate(events_repo.TOMBSTONE_EVENTS)},
    }
    latest = (
        "SELECT e.paper_id, e.event_type, e.id FROM interaction_events e"
        " WHERE e.session_id = :sid"
        "   AND e.id = (SELECT MAX(i.id) FROM interaction_events i"
        "               WHERE i.session_id = e.session_id AND i.paper_id = e.paper_id)"
        f"   AND e.event_type IN ({placeholders})"
    )

    counts = conn.execute(
        text(f"SELECT t.event_type, COUNT(*) FROM ({latest}) t GROUP BY t.event_type"),
        params,
    ).fetchall()
    # Sorted by size, then name: the same ordering the filter tabs use, so the
    # heaviest reason is the first thing read in every tab.
    by_reason = sorted(((str(k), int(n)) for k, n in counts), key=lambda r: (-r[1], r[0]))
    total = sum(n for _, n in by_reason)

    rows = conn.execute(
        text(
            "SELECT t.paper_id, p.title, t.event_type, p.year"
            f" FROM ({latest}) t JOIN papers p ON p.id = t.paper_id"
            " ORDER BY t.id DESC LIMIT :limit"
        ),
        {**params, "limit": limit},
    )
    papers = [
        ReviewPaper(
            paper_id=int(pid),
            title=str(title),
            reason_code=str(event_type),
            # The actor, which is the honest "stage" for a removal: R2.6 writes
            # REMOVED as USER and GC_SWEPT as SYSTEM, and that is exactly the
            # difference between a decision and its consequence.
            stage="USER" if event_type == "REMOVED" else "SYSTEM",
            year=int(year) if year is not None else None,
        )
        for pid, title, event_type, year in rows
    ]
    return ReviewBucket(
        total=total, by_reason=by_reason, papers=papers, truncated=total > len(papers)
    )


def build_review(
    conn: Connection, session_id: int, limit: int = DEFAULT_LIMIT
) -> dict[str, ReviewBucket]:
    """The three tabs. Always all three, even when every one of them is empty."""
    return {
        "quarantined": _filter_bucket(conn, session_id, "QUARANTINE", limit),
        "rejected": _filter_bucket(conn, session_id, "REJECT", limit),
        "removed": _removed_bucket(conn, session_id, limit),
    }


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "ReviewBucket", "ReviewPaper", "build_review"]
