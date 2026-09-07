"""
storage.py
----------
SQLite persistence layer for the citation network and paper recommender.

Schema
------
papers:          one row per paper (seed, candidate, liked, disliked, skipped)
edges:           directed citation edges (citing_id -> cited_id)
seed_titles:     raw user inputs, kept for traceability / re-runs
network_metrics: PageRank, in-degree, co-citation counts per paper

A paper's `label` column drives the recommendation loop:
    'seed'      -> came from the user's original input
    'liked'     -> user gave thumbs-up (becomes a seed next run)
    'disliked'  -> user gave thumbs-down (used as a negative signal)
    'skipped'   -> shown but no strong opinion; excluded from re-recommendation
    NULL        -> a candidate not yet shown to the user
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_DB_PATH = Path(__file__).parent / "paper_store.sqlite3"

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id          TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    abstract          TEXT,
    year              INTEGER,
    venue             TEXT,
    authors           TEXT,
    arxiv_id          TEXT,
    doi               TEXT,
    citation_count    INTEGER,
    tldr              TEXT,
    embedding         TEXT,
    field_of_study    TEXT,
    source_api        TEXT,
    distance_from_seed INTEGER,
    seed_connections  TEXT,
    label             TEXT,
    added_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS edges (
    citing_id TEXT NOT NULL,
    cited_id  TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id)
);

CREATE TABLE IF NOT EXISTS seed_titles (
    title             TEXT PRIMARY KEY,
    matched_paper_id  TEXT,
    match_confidence  REAL
);

CREATE TABLE IF NOT EXISTS network_metrics (
    paper_id    TEXT PRIMARY KEY,
    pagerank    REAL,
    in_degree   INTEGER,
    co_citation INTEGER
);
"""

# Columns added after initial deployment — applied with ALTER TABLE so existing
# databases are migrated transparently.
_MIGRATION_STMTS = [
    "ALTER TABLE papers ADD COLUMN doi TEXT",
    "ALTER TABLE papers ADD COLUMN field_of_study TEXT",
    "ALTER TABLE papers ADD COLUMN source_api TEXT",
    "ALTER TABLE papers ADD COLUMN distance_from_seed INTEGER",
    "ALTER TABLE papers ADD COLUMN seed_connections TEXT",
]


@dataclass
class Paper:
    paper_id: str
    title: str
    abstract: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    authors: list = field(default_factory=list)
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    citation_count: int = 0
    tldr: Optional[str] = None
    embedding: Optional[list] = None
    field_of_study: list = field(default_factory=list)
    source_api: Optional[str] = None
    distance_from_seed: Optional[int] = None
    seed_connections: list = field(default_factory=list)
    label: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Paper":
        keys = row.keys()
        return cls(
            paper_id=row["paper_id"],
            title=row["title"],
            abstract=row["abstract"],
            year=row["year"],
            venue=row["venue"],
            authors=json.loads(row["authors"]) if row["authors"] else [],
            arxiv_id=row["arxiv_id"],
            doi=row["doi"] if "doi" in keys else None,
            citation_count=row["citation_count"] or 0,
            tldr=row["tldr"],
            embedding=json.loads(row["embedding"]) if row["embedding"] else None,
            field_of_study=json.loads(row["field_of_study"]) if ("field_of_study" in keys and row["field_of_study"]) else [],
            source_api=row["source_api"] if "source_api" in keys else None,
            distance_from_seed=row["distance_from_seed"] if "distance_from_seed" in keys else None,
            seed_connections=json.loads(row["seed_connections"]) if ("seed_connections" in keys and row["seed_connections"]) else [],
            label=row["label"],
        )


class Store:
    """Thin wrapper around a SQLite file. Safe to call repeatedly across runs."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        with self._conn() as conn:
            conn.executescript(_CREATE_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for stmt in _MIGRATION_STMTS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- papers ----------------------------------------------------------------

    def upsert_paper(self, p: Paper, label: Optional[str] = None) -> str:
        """Upsert a paper and return the actual paper_id stored (may differ via DOI dedup)."""
        _PROTECTED = {"liked", "disliked"}
        with self._conn() as conn:
            # DOI dedup: if another row already holds this DOI, reuse its paper_id
            actual_id = p.paper_id
            if p.doi:
                doi_row = conn.execute(
                    "SELECT paper_id FROM papers WHERE doi = ? AND paper_id != ?",
                    (p.doi, p.paper_id),
                ).fetchone()
                if doi_row:
                    actual_id = doi_row["paper_id"]

            existing = conn.execute(
                "SELECT label FROM papers WHERE paper_id = ?", (actual_id,)
            ).fetchone()
            existing_label = existing["label"] if existing else None

            # Never downgrade a user preference (liked/disliked) to a lower-priority label
            if existing_label in _PROTECTED and label not in _PROTECTED:
                final_label = existing_label
            elif label is not None:
                final_label = label
            else:
                final_label = existing_label if existing else p.label

            conn.execute(
                """
                INSERT INTO papers
                    (paper_id, title, abstract, year, venue, authors, arxiv_id, doi,
                     citation_count, tldr, embedding, field_of_study, source_api,
                     distance_from_seed, seed_connections, label)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    title=excluded.title,
                    abstract=COALESCE(excluded.abstract, papers.abstract),
                    year=excluded.year,
                    venue=excluded.venue,
                    authors=excluded.authors,
                    arxiv_id=COALESCE(excluded.arxiv_id, papers.arxiv_id),
                    doi=COALESCE(excluded.doi, papers.doi),
                    citation_count=excluded.citation_count,
                    tldr=COALESCE(excluded.tldr, papers.tldr),
                    embedding=COALESCE(excluded.embedding, papers.embedding),
                    field_of_study=COALESCE(excluded.field_of_study, papers.field_of_study),
                    source_api=COALESCE(excluded.source_api, papers.source_api),
                    label=?
                """,
                (
                    actual_id, p.title, p.abstract, p.year, p.venue,
                    json.dumps(p.authors), p.arxiv_id, p.doi,
                    p.citation_count, p.tldr,
                    json.dumps(p.embedding) if p.embedding else None,
                    json.dumps(p.field_of_study) if p.field_of_study else None,
                    p.source_api,
                    p.distance_from_seed,
                    json.dumps(p.seed_connections) if p.seed_connections else None,
                    final_label,
                    final_label,
                ),
            )
        return actual_id

    def set_label(self, paper_id: str, label: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE papers SET label = ? WHERE paper_id = ?", (label, paper_id))

    def delete_paper(self, paper_id: str) -> None:
        """Delete a single paper and all edges that reference it."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM edges WHERE citing_id = ? OR cited_id = ?",
                (paper_id, paper_id),
            )
            conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))

    # Papers whose label anchors reachability for the cascade-delete sweep
    # below (a chain/island is only "still part of the network" if it can
    # reach one of these). disliked/skipped are excluded -- they're either
    # slated for separate pruning or not yet decided, so nothing should be
    # kept alive purely because it dangles off one of them.
    _CASCADE_ANCHOR_LABELS = {"seed", "liked"}
    # Papers that are never deleted by the cascade sweep, regardless of
    # reachability -- every user-labeled paper represents an explicit
    # decision and must not be swept away as a side effect.
    _CASCADE_PROTECTED_LABELS = {"seed", "liked", "disliked", "skipped"}

    def delete_paper_cascade(self, paper_id: str) -> list[str]:
        """
        Delete paper_id and its edges, then recursively remove any
        unlabeled paper left disconnected from every remaining seed/liked
        paper. Reachability is computed transitively (BFS over the
        remaining edges, ignoring direction), so an entire orphaned
        chain or island is swept in one pass -- not just paper_id's
        direct neighbors. Labeled papers (seed/liked/disliked/skipped)
        are never deleted by this sweep, though only seed/liked ones
        anchor reachability for everything else.
        Returns all paper_ids actually deleted, paper_id first.
        """
        with self._conn() as conn:
            existed = conn.execute(
                "SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)
            ).fetchone()
            if not existed:
                return []

            conn.execute(
                "DELETE FROM edges WHERE citing_id = ? OR cited_id = ?",
                (paper_id, paper_id),
            )
            conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
            deleted = [paper_id]

            edge_rows = conn.execute("SELECT citing_id, cited_id FROM edges").fetchall()
            adjacency: dict[str, set[str]] = defaultdict(set)
            for row in edge_rows:
                adjacency[row["citing_id"]].add(row["cited_id"])
                adjacency[row["cited_id"]].add(row["citing_id"])

            rows = conn.execute("SELECT paper_id, label FROM papers").fetchall()
            all_ids = {r["paper_id"] for r in rows}
            anchors = {r["paper_id"] for r in rows if r["label"] in self._CASCADE_ANCHOR_LABELS}
            protected = {r["paper_id"] for r in rows if r["label"] in self._CASCADE_PROTECTED_LABELS}

            reachable: set[str] = set(anchors)
            stack = list(anchors)
            while stack:
                cur = stack.pop()
                for nxt in adjacency.get(cur, ()):
                    if nxt not in reachable:
                        reachable.add(nxt)
                        stack.append(nxt)

            for pid in all_ids - reachable:
                if pid in protected:
                    continue
                conn.execute(
                    "DELETE FROM edges WHERE citing_id = ? OR cited_id = ?", (pid, pid)
                )
                conn.execute("DELETE FROM papers WHERE paper_id = ?", (pid,))
                deleted.append(pid)

            return deleted

    def clear_network(self) -> None:
        """Delete all papers, edges, and network metrics. Preserves seed_titles."""
        with self._conn() as conn:
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM papers")
            conn.execute("DELETE FROM network_metrics")

    def get_seed_titles(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT title, matched_paper_id, match_confidence FROM seed_titles"
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_seed_titles(self) -> None:
        """Delete all rows from seed_titles (e.g. before re-resolving fresh from a file)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM seed_titles")

    def delete_papers_by_label(self, label: str) -> int:
        """Delete all papers with the given label and any edges touching them. Returns count."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT paper_id FROM papers WHERE label = ?", (label,)
            ).fetchall()
            ids = [r["paper_id"] for r in rows]
            if not ids:
                return 0
            ph = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM edges WHERE citing_id IN ({ph}) OR cited_id IN ({ph})",
                ids + ids,
            )
            conn.execute(f"DELETE FROM papers WHERE paper_id IN ({ph})", ids)
            return len(ids)

    def get_paper(self, paper_id: str) -> Optional[Paper]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
            return Paper.from_row(row) if row else None

    def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
            return Paper.from_row(row) if row else None

    def papers_with_label(self, labels: Iterable[str]) -> list[Paper]:
        label_list = list(labels)
        placeholders = ",".join("?" * len(label_list))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM papers WHERE label IN ({placeholders})", label_list
            ).fetchall()
            return [Paper.from_row(r) for r in rows]

    def all_paper_ids(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT paper_id FROM papers").fetchall()
            return {r["paper_id"] for r in rows}

    def unlabeled_candidates(self) -> list[Paper]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM papers WHERE label IS NULL").fetchall()
            return [Paper.from_row(r) for r in rows]

    def search_papers(self, query: str, limit: int = 50) -> list[Paper]:
        """Return papers whose title contains query (case-insensitive substring)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM papers WHERE LOWER(title) LIKE LOWER(?) LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
            return [Paper.from_row(r) for r in rows]

    def get_neighbors(self, paper_id: str) -> list[dict]:
        """Return direct neighbors: direction='out' (this cites them) or 'in' (they cite this)."""
        with self._conn() as conn:
            out_rows = conn.execute(
                """SELECT p.paper_id, p.title, p.year, p.citation_count, p.venue, p.label
                   FROM edges e JOIN papers p ON p.paper_id = e.cited_id
                   WHERE e.citing_id = ?""",
                (paper_id,),
            ).fetchall()
            in_rows = conn.execute(
                """SELECT p.paper_id, p.title, p.year, p.citation_count, p.venue, p.label
                   FROM edges e JOIN papers p ON p.paper_id = e.citing_id
                   WHERE e.cited_id = ?""",
                (paper_id,),
            ).fetchall()
        result = []
        for row in out_rows:
            result.append({
                "paper_id": row["paper_id"], "title": row["title"],
                "year": row["year"], "citation_count": row["citation_count"] or 0,
                "venue": row["venue"], "label": row["label"], "direction": "out",
            })
        for row in in_rows:
            result.append({
                "paper_id": row["paper_id"], "title": row["title"],
                "year": row["year"], "citation_count": row["citation_count"] or 0,
                "venue": row["venue"], "label": row["label"], "direction": "in",
            })
        return result

    def update_distance(self, paper_id: str, distance: int,
                        seed_connections: list[str]) -> None:
        """Update distance and merge seed_connections when path is equal or shorter."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT distance_from_seed, seed_connections FROM papers WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
            if row is None:
                return
            existing_dist = row["distance_from_seed"]
            if existing_dist is not None and existing_dist < distance:
                return  # already have a shorter path
            if existing_dist is not None and existing_dist == distance:
                existing_conns = json.loads(row["seed_connections"]) if row["seed_connections"] else []
                merged = list(set(existing_conns) | set(seed_connections))
            else:
                merged = list(seed_connections)
            conn.execute(
                "UPDATE papers SET distance_from_seed = ?, seed_connections = ? WHERE paper_id = ?",
                (distance, json.dumps(merged), paper_id),
            )

    # ---- edges -----------------------------------------------------------------

    def add_edges(self, citing_id: str, cited_ids: Iterable[str]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO edges (citing_id, cited_id) VALUES (?, ?)",
                [(citing_id, cid) for cid in cited_ids],
            )

    def all_edges(self) -> list[tuple[str, str]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT citing_id, cited_id FROM edges").fetchall()
            return [(r["citing_id"], r["cited_id"]) for r in rows]

    # ---- network metrics -------------------------------------------------------

    def upsert_metrics(self, paper_id: str, pagerank: float,
                       in_degree: int, co_citation: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO network_metrics (paper_id, pagerank, in_degree, co_citation)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    pagerank=excluded.pagerank,
                    in_degree=excluded.in_degree,
                    co_citation=excluded.co_citation
                """,
                (paper_id, pagerank, in_degree, co_citation),
            )

    def get_metrics(self, paper_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM network_metrics WHERE paper_id = ?", (paper_id,)
            ).fetchone()
            if row:
                return dict(row)
            return {"paper_id": paper_id, "pagerank": 0.0, "in_degree": 0, "co_citation": 0}

    def all_papers_with_metrics(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT p.*, nm.pagerank, nm.in_degree, nm.co_citation
                FROM papers p
                LEFT JOIN network_metrics nm ON p.paper_id = nm.paper_id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- seed titles (traceability) -------------------------------------------

    def record_seed_title(self, title: str, matched_paper_id: Optional[str],
                          confidence: float) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO seed_titles (title, matched_paper_id, match_confidence)
                VALUES (?, ?, ?)
                ON CONFLICT(title) DO UPDATE SET
                    matched_paper_id=excluded.matched_paper_id,
                    match_confidence=excluded.match_confidence
                """,
                (title, matched_paper_id, confidence),
            )
