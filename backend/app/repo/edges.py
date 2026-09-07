"""
The `edges` table. **Global** -- no `session_id`, by contract.

The citation graph is a fact about the world, which is the big win in PLAN.md's
data model: seeding session B with a paper already crawled in session A costs
zero API calls.

Everything here exists to make `upsert_edge` a *merge* rather than a
last-write-wins overwrite. The same pair arrives from both directions -- once
when we fetch A's references, once when we fetch B's citations -- and the merged
result is what the direction-floor budget (R1.10) allocates against:

    BACKWARD + FORWARD  -> BOTH        provenance, not the newer of the two
    is_influential      -> OR          one endpoint saying yes is enough
    intents             -> sorted union, so the stored value is deterministic

SQL skeleton from BUILD.md Appendix B.2; the intents union is computed in
Python because SQLite has no array type.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from app.models import DISCOVERED_VIA, Edge


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _existing(
    conn: Connection, citing_id: int, cited_id: int
) -> tuple[str, tuple[str, ...]] | None:
    row = conn.execute(
        text(
            "SELECT discovered_via, intents FROM edges"
            " WHERE citing_id = :citing AND cited_id = :cited"
        ),
        {"citing": citing_id, "cited": cited_id},
    ).fetchone()
    if row is None:
        return None
    try:
        intents = tuple(json.loads(row[1])) if row[1] else ()
    except (json.JSONDecodeError, TypeError):
        intents = ()
    return row[0], intents


def upsert_edge(
    conn: Connection,
    citing_id: int,
    cited_id: int,
    via: str,
    *,
    is_influential: bool = False,
    intents: tuple[str, ...] = (),
    first_seen_at: str | None = None,
) -> None:
    """Insert or merge one citation edge. Direction is always citing -> cited."""
    if citing_id == cited_id:
        # S2 really does return self-citations. The CHECK constraint would
        # reject them; failing here gives a caller-shaped error instead.
        raise ValueError(f"edge cannot be a self citation: {citing_id}")
    if via not in DISCOVERED_VIA:
        raise ValueError(f"discovered_via must be one of {sorted(DISCOVERED_VIA)}, got {via!r}")

    # Only unioning intents genuinely needs the prior value. Reading
    # unconditionally cost a second round trip on every edge -- 400 statements
    # for one hub's 200 references -- so the common case (a citation edge with
    # no intents) skips the read entirely and lets SQL do the merge.
    merged_intents: set[str] | None = None
    if intents:
        prior = _existing(conn, citing_id, cited_id)
        merged_intents = set(intents)
        if prior is not None:
            merged_intents |= set(prior[1])

    conn.execute(
        text(
            "INSERT INTO edges (citing_id, cited_id, discovered_via, is_influential,"
            " intents, first_seen_at)"
            " VALUES (:citing, :cited, :via, :infl, :intents, :now)"
            " ON CONFLICT (citing_id, cited_id) DO UPDATE SET"
            # BUILD.md Appendix B.2: the direction merge is pure SQL, so it
            # needs no prior read. Same direction stays; anything else is BOTH.
            "   discovered_via = CASE"
            "     WHEN edges.discovered_via = excluded.discovered_via THEN edges.discovered_via"
            "     ELSE 'BOTH' END,"
            # COALESCE around MAX: SQLite's multi-argument MAX returns NULL if
            # ANY argument is NULL, and is_influential is a nullable column.
            # Without this, one NULL row silently clears the flag forever.
            "   is_influential = MAX("
            "     COALESCE(edges.is_influential, 0), COALESCE(excluded.is_influential, 0)),"
            # NULL intents means "this write carried none", not "erase what is
            # stored". COALESCE keeps the existing union.
            "   intents = COALESCE(excluded.intents, edges.intents)"
            # first_seen_at deliberately absent: it records discovery, not the
            # most recent sighting.
        ),
        {
            "citing": citing_id,
            "cited": cited_id,
            "via": via,
            "infl": 1 if is_influential else 0,
            "intents": json.dumps(sorted(merged_intents)) if merged_intents else None,
            "now": first_seen_at or _utcnow(),
        },
    )


def get_edges_for(conn: Connection, paper_ids: list[int]) -> list[Edge]:
    """
    Every edge touching any of `paper_ids`, in either direction.

    A node's neighbourhood is what it cites *and* what cites it, and an edge
    whose two endpoints are both requested is returned once, not twice.
    """
    if not paper_ids:
        return []
    seen: set[tuple[int, int]] = set()
    out: list[Edge] = []
    for start in range(0, len(paper_ids), 400):  # two placeholder sets per chunk
        chunk = paper_ids[start : start + 400]
        placeholders = ",".join(f":p{i}" for i in range(len(chunk)))
        rows = conn.execute(
            text(
                "SELECT citing_id, cited_id, discovered_via, is_influential, intents,"
                " first_seen_at FROM edges"
                f" WHERE citing_id IN ({placeholders}) OR cited_id IN ({placeholders})"
            ),
            {f"p{i}": v for i, v in enumerate(chunk)},
        )
        for row in rows:
            key = (row[0], row[1])
            if key in seen:
                continue
            seen.add(key)
            try:
                intents = tuple(json.loads(row[4])) if row[4] else ()
            except (json.JSONDecodeError, TypeError):
                intents = ()
            out.append(
                Edge(
                    citing_id=row[0],
                    cited_id=row[1],
                    discovered_via=row[2],
                    is_influential=bool(row[3]),
                    intents=intents,
                    first_seen_at=row[5],
                )
            )
    return out


__all__ = ["get_edges_for", "upsert_edge"]
