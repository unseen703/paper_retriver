"""
Computing and persisting `node_features` (R3).

The link between everything else in R3. `graphops` measures the graph,
`similarity` measures relatedness, `ranking` turns features into a score, and
`PUT /api/config` rescores from what is stored -- but until something writes
those features the rescore reads nothing and every score is null.

**Normalized before storage, not at score time.** PLAN.md wants rank-percentile
within the pool. Storing raw citation counts and normalizing later would work
arithmetically, but the pool would then be whatever happened to be loaded when
the score was computed -- so a paper's score would depend on what else was
being looked at. Normalizing once, against the whole session, makes the stored
number mean one thing.

That is also what makes `PUT /api/config` free: a stored feature is already in
[0, 1], so rescoring is a multiply and an add.

**`anchor_overlap` is read, not recomputed.** It is counted during pooling,
where the union is being built and the information is in hand. Recomputing it
here would be a second implementation of the same count, free to drift from the
first (`candidates.py` says the same thing from the other side).

**Names match `ranking.yaml` exactly.** A feature the config has no weight for
is dead computation; a weight with no feature is a lever connected to nothing.
Both fail silently, so `test_features.py` asserts the correspondence.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import Engine, text

from app.config import ranking
from app.services.graphops import compute_signals
from app.services.ranking import citations_per_year, rank_percentile, recency, score_paper
from app.services.similarity import compute_similarity

logger = logging.getLogger(__name__)

#: The features this module computes, matching weight names in `ranking.yaml`.
#:
#: `ppr`, `venue`, `author` and `dislike` are deliberately absent -- they are
#: weighted at 0.00 and not yet implemented, and inventing a value for them
#: would put a number behind a lever that does nothing.
FEATURE_NAMES = ("overlap", "quality", "recency", "hub", "cocite", "bibcoup")

#: States whose papers anchor "related to what?". The same set the sweep marks
#: from and the expander builds its frontier from.
ANCHOR_STATES = ("SEED", "LIKED")


def _rows(
    engine: Engine, session_id: int
) -> list[tuple[int, str, int | None, int, dict[str, float]]]:
    """(paper_id, state, year, citation_count, existing features) for the session."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT g.paper_id, g.state, p.year, p.citation_count, g.features"
                " FROM graph_nodes g JOIN papers p ON p.id = g.paper_id"
                # Sorted so two runs write in the same order and agree byte for
                # byte (CLAUDE.md rule 7).
                " WHERE g.session_id = :sid ORDER BY g.paper_id"
            ),
            {"sid": session_id},
        ).fetchall()

    out: list[tuple[int, str, int | None, int, dict[str, float]]] = []
    for paper_id, state, year, citations, raw in rows:
        try:
            existing = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            existing = {}
        out.append(
            (
                int(paper_id),
                str(state),
                int(year) if year is not None else None,
                int(citations or 0),
                existing if isinstance(existing, dict) else {},
            )
        )
    return out


def compute_and_store_features(
    engine: Engine, session_id: int, as_of_year: int, weights: dict[str, float] | None = None
) -> int:
    """
    Compute every feature for this session, normalize, persist, and score.

    Returns the number of nodes written. Scores are written too: features with
    no score would leave the graph looking unranked until somebody happened to
    PUT the config.

    `weights` defaults to `config/ranking.yaml`. The *runtime* weights -- which
    a PUT may have overridden -- live in the API layer, and the caller passes
    them in; reaching up for them from here would put a service on the wrong
    side of CLAUDE.md rule 3.
    """
    rows = _rows(engine, session_id)
    if not rows:
        return 0

    with engine.connect() as conn:
        signals = compute_signals(conn, session_id)
        anchors = [pid for pid, state, _, _, _ in rows if state in ANCHOR_STATES]
        similarity = compute_similarity(conn, anchors)

    # Raw values first, one dict per feature across the whole session -- the
    # normalization pool is the session, so it has to be gathered before
    # anything is ranked.
    raw: dict[str, dict[int, float]] = {name: {} for name in FEATURE_NAMES}
    for paper_id, _, year, citations, existing in rows:
        raw["overlap"][paper_id] = float(existing.get("anchor_overlap") or 0)
        raw["quality"][paper_id] = citations_per_year(citations, year, as_of_year)
        raw["recency"][paper_id] = recency(year, as_of_year)
        # In-degree *within this graph*, which is what "everyone here cites it"
        # means. The global citation count is `quality`'s business.
        raw["hub"][paper_id] = float(signals.in_degree.get(paper_id, 0))
        raw["cocite"][paper_id] = float(similarity.co_citation.get(paper_id, 0))
        raw["bibcoup"][paper_id] = float(similarity.bib_coupling.get(paper_id, 0))

    normalized = {name: rank_percentile(values) for name, values in raw.items()}

    active = weights if weights is not None else ranking.weights.model_dump()
    written = 0
    with engine.begin() as conn:
        for paper_id, _, _, _, _ in rows:
            features = {name: float(normalized[name][paper_id]) for name in FEATURE_NAMES}
            total, breakdown = score_paper(features, active)
            conn.execute(
                text(
                    "UPDATE graph_nodes SET features = :features, score = :score,"
                    " score_breakdown = :breakdown"
                    " WHERE session_id = :sid AND paper_id = :pid"
                ),
                {
                    "features": json.dumps(features),
                    "score": total,
                    "breakdown": json.dumps(breakdown),
                    "sid": session_id,
                    "pid": paper_id,
                },
            )
            written += 1

    logger.info(
        "features_computed session=%s nodes=%s anchors=%s pagerank_suppressed=%s",
        session_id,
        written,
        len(anchors),
        signals.pagerank_suppressed,
    )
    return written


__all__ = ["ANCHOR_STATES", "FEATURE_NAMES", "compute_and_store_features"]
