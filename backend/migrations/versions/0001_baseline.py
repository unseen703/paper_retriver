"""baseline: the full PLAN.md section C schema

Every table lands at once, including columns nothing reads until R6
(`community_id`, `pos_x`, `pos_y`). Schema churn is free before there is data
and expensive after, so the cheap moment is now.

Revision ID: 0001
Revises:
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── sessions ────────────────────────────────────────────────────────────
    # Created first: graph_nodes, interaction_events and expansions all
    # reference it, and SQLite resolves FKs at insert time.
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # ── corpus ──────────────────────────────────────────────────────────────
    op.create_table(
        "papers",
        # Surrogate PK, not s2_paper_id: dedup merges rewrite canonical_paper_id
        # without having to rewrite every edge and event in lockstep.
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("s2_paper_id", sa.Text(), nullable=False, unique=True),
        sa.Column("s2_corpus_id", sa.Integer()),
        sa.Column("canonical_paper_id", sa.Integer(), sa.ForeignKey("papers.id")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text()),
        sa.Column("year", sa.Integer()),
        sa.Column("publication_date", sa.Text()),
        sa.Column("venue", sa.Text()),
        sa.Column("venue_tier", sa.Text()),
        sa.Column("citation_count", sa.Integer(), server_default="0"),
        sa.Column("reference_count", sa.Integer(), server_default="0"),
        sa.Column("influential_citation_count", sa.Integer(), server_default="0"),
        sa.Column("doi", sa.Text()),
        sa.Column("arxiv_id", sa.Text()),
        sa.Column("primary_arxiv_category", sa.Text()),
        sa.Column("arxiv_categories", sa.Text()),
        sa.Column("s2_fields", sa.Text()),
        sa.Column("publication_types", sa.Text()),
        sa.Column("paper_type", sa.Text(), nullable=False, server_default="UNKNOWN"),
        sa.Column("crawl_state", sa.Text(), nullable=False, server_default="STUB"),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("metadata_fetched_at", sa.Text()),
    )
    op.create_index("ix_papers_title_norm", "papers", ["title_norm"])
    op.create_index("ix_papers_arxiv", "papers", ["arxiv_id"])
    op.create_index("ix_papers_doi", "papers", ["doi"])

    op.create_table(
        "authors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("s2_author_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("h_index", sa.Integer()),  # NULL until lazily fetched (C4)
        sa.Column("citation_count", sa.Integer()),
        sa.Column("paper_count", sa.Integer()),
        sa.Column("fetched_at", sa.Text()),
    )

    op.create_table(
        "paper_authors",
        sa.Column("paper_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("authors.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),  # 0-based; first/last matter
        sa.PrimaryKeyConstraint("paper_id", "author_id"),
    )

    # ── edges: direction is always citing -> cited ───────────────────────────
    op.create_table(
        "edges",
        sa.Column("citing_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("cited_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("discovered_via", sa.Text(), nullable=False),  # BACKWARD|FORWARD|BOTH
        sa.Column("is_influential", sa.Integer(), server_default="0"),
        sa.Column("intents", sa.Text()),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("citing_id", "cited_id"),
        # S2 really does return self-citations; a self-loop breaks PageRank.
        sa.CheckConstraint("citing_id != cited_id", name="ck_edges_no_self_citation"),
    )
    op.create_index("ix_edges_cited", "edges", ["cited_id"])  # reverse traversal

    # ── graph: the visible working set ──────────────────────────────────────
    op.create_table(
        "graph_nodes",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("paper_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),  # SEED|CANDIDATE|LIKED|DISLIKED
        sa.Column("score", sa.Float()),
        sa.Column("features", sa.Text()),
        sa.Column("score_breakdown", sa.Text()),  # powers the UI's "why?"
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("added_by", sa.Integer()),  # -> expansions.id, created below
        sa.Column("pos_x", sa.Float()),
        sa.Column("pos_y", sa.Float()),
        sa.Column("community_id", sa.Integer()),  # Leiden, R6
        sa.PrimaryKeyConstraint("session_id", "paper_id"),
    )
    op.create_index("ix_graph_state", "graph_nodes", ["session_id", "state"])

    # ── history: append-only; graph_nodes.state is its projection ───────────
    op.create_table(
        "interaction_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("paper_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, server_default="USER"),
        sa.Column("payload", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_events_paper", "interaction_events", ["session_id", "paper_id", "id"])

    # ── observability ───────────────────────────────────────────────────────
    op.create_table(
        "filter_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("paper_id", sa.Integer(), sa.ForeignKey("papers.id"), nullable=False),
        # Nullable on purpose. NULL = a global verdict (PRE_ERA, IS_DATASET) that
        # holds in every session forever and can be cached; non-NULL = a fact
        # about one graph. See BUILD.md "The session_id contract".
        sa.Column("session_id", sa.Integer()),
        sa.Column("outcome", sa.Text(), nullable=False),  # ACCEPT|QUARANTINE|REJECT
        sa.Column("stage", sa.Text(), nullable=False),  # TYPE|TOPIC|APPLIED|DUPLICATE
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("config_version", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_filter_decisions_paper", "filter_decisions", ["paper_id", "session_id"])

    op.create_table(
        "expansions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),  # QUEUED|RUNNING|DONE|FAILED|CANCELLED
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Text(), nullable=False),
        sa.Column("n_pool", sa.Integer()),
        sa.Column("n_filtered", sa.Integer()),
        sa.Column("n_added", sa.Integer()),
        sa.Column("api_calls", sa.Integer()),
        sa.Column("cache_hits", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.Text()),
        sa.Column("finished_at", sa.Text()),
    )

    op.create_table(
        "api_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("params_hash", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        # Verbatim JSON. Never normalized on write -- normalizing here means a
        # later parser change silently reinterprets already-cached responses.
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("endpoint", "params_hash", name="uq_api_cache_endpoint_params"),
    )

    op.create_table(
        "arxiv_meta",
        sa.Column("arxiv_id", sa.Text(), primary_key=True),
        sa.Column("primary_category", sa.Text(), nullable=False),
        sa.Column("categories", sa.Text(), nullable=False),
        sa.Column("updated", sa.Text()),
    )

    # ── seed ────────────────────────────────────────────────────────────────
    # config.settings.session_id is hardcoded to 1 for R0/R1; without this row
    # every graph_nodes insert fails the FK.
    op.execute(
        "INSERT INTO sessions (id, name, created_at) VALUES (1, 'default', '2026-09-07T00:00:00Z')"
    )


def downgrade() -> None:
    # Reverse creation order so FK parents outlive their children.
    op.drop_table("arxiv_meta")
    op.drop_table("api_cache")
    op.drop_table("expansions")
    op.drop_index("ix_filter_decisions_paper", table_name="filter_decisions")
    op.drop_table("filter_decisions")
    op.drop_index("ix_events_paper", table_name="interaction_events")
    op.drop_table("interaction_events")
    op.drop_index("ix_graph_state", table_name="graph_nodes")
    op.drop_table("graph_nodes")
    op.drop_index("ix_edges_cited", table_name="edges")
    op.drop_table("edges")
    op.drop_table("paper_authors")
    op.drop_table("authors")
    op.drop_index("ix_papers_doi", table_name="papers")
    op.drop_index("ix_papers_arxiv", table_name="papers")
    op.drop_index("ix_papers_title_norm", table_name="papers")
    op.drop_table("papers")
    op.drop_table("sessions")
