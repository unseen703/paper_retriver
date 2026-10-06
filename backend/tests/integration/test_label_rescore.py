"""
R5.4 -- a label change re-ranks the session, live.

Journey:

    As someone curating a graph, I want the candidate list to reorder the
    moment I like or dislike a paper, so the ranking learns from me while I am
    still looking at it.

`ppr` and `dislike` are functions of the labels, so unlike a PUT /api/config
rescore this one recomputes features. Only the labelled session is touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.db import make_engine
from app.services.labelling import apply_label
from app.services.scoring import rescore_after_label

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
AS_OF = 2026
WEIGHTS = {"overlap": 0.5, "quality": 0.3, "recency": 0.2, "ppr": 0.0, "hub": -0.1}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "rescore.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


def _add(engine: Engine, sid: int, name: str, state: str, year: int, cites: int) -> int:
    with engine.begin() as conn:
        pid = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year,"
                " citation_count, crawl_state) VALUES (:s, :t, :t, '2026-01-01', :y, :c,"
                " 'METADATA') ON CONFLICT(s2_paper_id) DO UPDATE SET year = year"
                " RETURNING id"
            ),
            {"s": name, "t": name, "y": year, "c": cites},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth, features)"
                " VALUES (:sid, :p, :st, 1, :f)"
            ),
            {"sid": sid, "p": pid, "st": state, "f": json.dumps({"anchor_overlap": 1})},
        )
    assert pid is not None
    return int(pid)


def _scores(engine: Engine, sid: int) -> dict[int, float | None]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT paper_id, score FROM graph_nodes WHERE session_id = :s"), {"s": sid}
        ).fetchall()
    return {int(p): s for p, s in rows}


def _two_sessions(engine: Engine) -> tuple[int, int]:
    with engine.begin() as conn:
        for sid in (1, 2):
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO sessions (id, name, created_at)"
                    " VALUES (:i, 's', '2026-01-01')"
                ),
                {"i": sid},
            )
    seed = _add(engine, 1, "seed", "SEED", 2020, 10)
    cand = _add(engine, 1, "cand", "CANDIDATE", 2023, 50)
    return seed, cand


def test_label_rescores_the_session_and_reports_the_count(engine: Engine) -> None:
    _, cand = _two_sessions(engine)
    assert _scores(engine, 1)[cand] is None

    result = apply_label(engine, 1, cand, "LIKED", weights=WEIGHTS, as_of_year=AS_OF)

    assert result.rescored_count == 2
    assert _scores(engine, 1)[cand] is not None
    assert result.node.score == _scores(engine, 1)[cand]


def test_other_sessions_are_not_touched(engine: Engine) -> None:
    _, cand = _two_sessions(engine)
    other = _add(engine, 2, "other", "CANDIDATE", 2022, 5)

    apply_label(engine, 1, cand, "LIKED", weights=WEIGHTS, as_of_year=AS_OF)

    assert _scores(engine, 2)[other] is None


def test_a_noop_relabel_rescores_nothing(engine: Engine) -> None:
    _, cand = _two_sessions(engine)
    apply_label(engine, 1, cand, "LIKED", weights=WEIGHTS, as_of_year=AS_OF)
    again = apply_label(engine, 1, cand, "LIKED", weights=WEIGHTS, as_of_year=AS_OF)
    assert again.rescored_count == 0


def test_without_weights_the_label_does_not_rescore(engine: Engine) -> None:
    _, cand = _two_sessions(engine)
    assert apply_label(engine, 1, cand, "LIKED").rescored_count == 0
    assert _scores(engine, 1)[cand] is None


def test_rescoring_is_deterministic(engine: Engine) -> None:
    _two_sessions(engine)
    rescore_after_label(engine, 1, WEIGHTS, AS_OF)
    first = _scores(engine, 1)
    rescore_after_label(engine, 1, WEIGHTS, AS_OF)
    assert _scores(engine, 1) == first


def test_the_nudge_applies_to_scores_but_is_never_persisted(engine: Engine) -> None:
    """Enough labels to trigger Rocchio; the stored features stay un-nudged."""
    _two_sessions(engine)
    ids = [_add(engine, 1, f"x{i}", "LIKED", 2024, 100 + i) for i in range(5)]
    for i in range(3):
        _add(engine, 1, f"d{i}", "DISLIKED", 2016, 1)

    rescore_after_label(engine, 1, WEIGHTS, AS_OF)
    nudged = _scores(engine, 1)[ids[0]]

    # Same labels, nudging bypassed by a MIN_LABELS-defeating weight set of
    # zeros would differ; instead compare against the plain per-weight score.
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT features, score_breakdown FROM graph_nodes WHERE paper_id = :p"),
            {"p": ids[0]},
        ).fetchone()
    assert raw is not None
    features = json.loads(raw[0])
    plain = sum(WEIGHTS[k] * v for k, v in features.items() if k in WEIGHTS)
    assert nudged is not None
    assert nudged != pytest.approx(plain)
    assert set(features) <= {
        "overlap",
        "quality",
        "recency",
        "hub",
        "cocite",
        "bibcoup",
        "venue",
        "ppr",
        "dislike",
    }
