"""
Turning persisted features into scores (R3).

PLAN.md states the design and its payoff together:

    "Features are computed and persisted; scores are derived on read. [...]
    This means changing weights re-ranks instantly with **zero API calls and
    zero recomputation** -- which is what makes a config sweep in the eval
    harness possible at all."

**Two operations, kept apart on purpose.** Computing features touches the
graph, the corpus and the clock. Scoring touches none of them: it reads a JSON
blob out of `graph_nodes.features` and calls `ranking.score_paper`, which is a
pure function. Keeping the second free of the first is the whole point --
otherwise every weight change costs a graph rebuild, and at R5 it would cost
embeddings.

That separation is easy to lose by accident and hard to notice when you do: a
rescore that re-derives its features returns exactly the right numbers. The
only symptom is that it got slow, which is the kind of thing that gets
explained away.

**A node with no features gets no score.** Null, not 0.0. A node an expansion
never scored has no score, and writing a zero would rank it below every paper
that genuinely scored badly -- a judgment nobody made.

**Every session is rescored.** Weights are global: they live in one config file
rather than per session. Rescoring only the session that happened to be open
would leave every other one ranked by weights nobody is using any more.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from sqlalchemy import Engine, text

from app.services.ranking import score_paper

logger = logging.getLogger(__name__)


def _features(raw: str | None) -> dict[str, float]:
    """
    Parse a persisted feature blob, tolerantly.

    A malformed blob means one node loses its score, not that the whole rescore
    fails -- the same reasoning as `repo/graph._loads`. Anything non-numeric is
    dropped rather than coerced: a feature that arrived as a string is a bug
    upstream, and turning it into a number here would hide it inside a
    plausible score.
    """
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(k): float(v)
        for k, v in loaded.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def rescore_all(engine: Engine, weights: Mapping[str, float]) -> int:
    """
    Rescore every node in every session from its persisted features.

    Returns how many nodes were scored. **No API calls and no feature
    recomputation** -- this reads `features`, calls a pure function, and writes
    `score` and `score_breakdown` back. That is BUILD.md's "payoff for
    persisting features", and it is what makes tuning a conversation rather
    than a batch job.

    One transaction: a half-rescored graph would be ranked by two different
    weight sets at once, which is worse than being ranked by the old one.
    """
    scored = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT session_id, paper_id, features FROM graph_nodes"
                # Ordered so a rescore writes in a stable sequence, which keeps
                # two runs over one graph byte-identical (CLAUDE.md rule 7).
                " ORDER BY session_id, paper_id"
            )
        ).fetchall()

        for session_id, paper_id, raw in rows:
            features = _features(raw)
            if not features:
                # No features, no score. See the module docstring on why this
                # is not a zero.
                continue
            total, breakdown = score_paper(features, weights)
            conn.execute(
                text(
                    "UPDATE graph_nodes SET score = :score, score_breakdown = :breakdown"
                    " WHERE session_id = :session_id AND paper_id = :paper_id"
                ),
                {
                    "score": total,
                    "breakdown": json.dumps(breakdown),
                    "session_id": session_id,
                    "paper_id": paper_id,
                },
            )
            scored += 1

    logger.info("rescored nodes=%s weights=%s", scored, dict(weights))
    return scored


__all__ = ["rescore_all"]
