"""
R2.14 -- `GET /api/sessions/{sid}/review`, behind the ReviewDrawer.

Journey:

    As someone tuning a filter, I want to see what it threw away and why, so a
    filter that is quietly discarding good papers becomes something I can
    notice rather than something I find out about in six weeks.

PLAN.md M5 states the case: "A filter you cannot audit is a filter you cannot
tune, and you will silently discard good papers for weeks without noticing."

Three tabs, three different kinds of absence, and conflating them would hide
the distinction that makes the drawer useful:

    Quarantined  outcome = QUARANTINE -- "probably applied ML, not sure".
                 PLAN.md: most of the hard cases land here.
    Rejected     outcome = REJECT -- the cascade said no.
    Removed      latest event is a tombstone -- *you* said no, or the sweep
                 collected it as a consequence.

**Counts are complete; rows are capped.** A mature corpus rejects thousands of
papers, and shipping every one to render a drawer nobody scrolls to the end of
would make the endpoint slow exactly when the graph is interesting. The
per-reason counts are what BUILD.md's verification checks, so those are always
whole; the rows are a sample, and `truncated` says so rather than leaving the
client to infer it from a suspiciously round number.

**Removed groups by event type, not by a reason code**, because that is the
distinction that exists: `REMOVED` is a decision you made and `GC_SWEPT` is a
consequence the system drew from it. R2.6 kept those apart in its response for
the same reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
SID = 1


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = tmp_path / "review.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(db_url=str(engine.url))) as test_client:
        yield test_client


def _paper(engine: Engine, name: str) -> int:
    with engine.begin() as conn:
        paper_id = conn.execute(
            text(
                "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at, year)"
                " VALUES (:s, :t, :t, '2026-01-01', 2020) RETURNING id"
            ),
            {"s": name, "t": f"Paper {name}"},
        ).scalar()
    assert paper_id is not None
    return int(paper_id)


def _decide(
    engine: Engine,
    paper_id: int,
    outcome: str,
    reason: str,
    *,
    stage: str = "TOPIC",
    session_scoped: bool = False,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO filter_decisions (paper_id, session_id, outcome, stage,"
                " reason_code, config_version, decided_at)"
                " VALUES (:p, :sid, :o, :st, :r, 'v1', '2026-01-01')"
            ),
            {
                "p": paper_id,
                "sid": SID if session_scoped else None,
                "o": outcome,
                "st": stage,
                "r": reason,
            },
        )


def _node(engine: Engine, name: str, state: str = "CANDIDATE") -> int:
    paper_id = _paper(engine, name)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                " VALUES (:sid, :p, :st, 1)"
            ),
            {"sid": SID, "p": paper_id, "st": state},
        )
    return paper_id


def _review(client: TestClient, **params: object) -> dict[str, object]:
    response = client.get(f"/api/sessions/{SID}/review", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------
# BUILD.md's verification: the rejected count matches filter_decisions
# --------------------------------------------------------------------------


def test_the_rejected_count_matches_filter_decisions(client: TestClient, engine: Engine) -> None:
    for i in range(4):
        _decide(engine, _paper(engine, f"r{i}"), "REJECT", "IS_DATASET")
    for i in range(3):
        _decide(engine, _paper(engine, f"c{i}"), "REJECT", "CAT_NOT_ALLOWED")

    body = _review(client)
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT COUNT(*) FROM filter_decisions WHERE outcome='REJECT'")
        ).scalar()
    assert body["rejected"]["total"] == stored == 7  # type: ignore[index]


def test_rejections_are_grouped_by_reason_code(client: TestClient, engine: Engine) -> None:
    for i in range(4):
        _decide(engine, _paper(engine, f"r{i}"), "REJECT", "IS_DATASET")
    for i in range(2):
        _decide(engine, _paper(engine, f"c{i}"), "REJECT", "CAT_NOT_ALLOWED")

    by_reason = {r["reason_code"]: r["count"] for r in _review(client)["rejected"]["by_reason"]}  # type: ignore[index,union-attr]
    assert by_reason == {"IS_DATASET": 4, "CAT_NOT_ALLOWED": 2}


def test_the_largest_reason_comes_first(client: TestClient, engine: Engine) -> None:
    """
    A drawer is read top-down. The reason discarding the most papers is the one
    worth tuning, so it should not be somewhere in the middle of an
    alphabetical list.
    """
    _decide(engine, _paper(engine, "a"), "REJECT", "AAA_RARE")
    for i in range(5):
        _decide(engine, _paper(engine, f"b{i}"), "REJECT", "ZZZ_COMMON")

    reasons = [r["reason_code"] for r in _review(client)["rejected"]["by_reason"]]  # type: ignore[index,union-attr]
    assert reasons[0] == "ZZZ_COMMON"


# --------------------------------------------------------------------------
# Three tabs, three kinds of absence
# --------------------------------------------------------------------------


def test_quarantined_is_separate_from_rejected(client: TestClient, engine: Engine) -> None:
    """
    PLAN.md: quarantine is "probably applied ML but I'm not sure", which is
    most of the hard cases. Folding it into Rejected would bury exactly the
    papers most worth a human glance.
    """
    _decide(engine, _paper(engine, "q"), "QUARANTINE", "MAYBE_BENCHMARK")
    _decide(engine, _paper(engine, "r"), "REJECT", "IS_DATASET")

    body = _review(client)
    assert body["quarantined"]["total"] == 1  # type: ignore[index]
    assert body["rejected"]["total"] == 1  # type: ignore[index]


def test_accepted_papers_appear_in_no_tab(client: TestClient, engine: Engine) -> None:
    """The drawer is about absence. A paper that got in has nothing to explain."""
    _decide(engine, _paper(engine, "ok"), "ACCEPT", "PASSED")
    body = _review(client)
    assert body["quarantined"]["total"] == 0  # type: ignore[index]
    assert body["rejected"]["total"] == 0  # type: ignore[index]


def test_removed_lists_papers_the_user_removed(client: TestClient, engine: Engine) -> None:
    seed = _node(engine, "S", "SEED")
    victim = _node(engine, "B")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": victim, "b": seed},
        )
    client.delete(f"/api/sessions/{SID}/nodes/{victim}", params={"dry_run": "false"})

    body = _review(client)
    assert body["removed"]["total"] == 1  # type: ignore[index]
    assert [p["paper_id"] for p in body["removed"]["papers"]] == [victim]  # type: ignore[index,union-attr]


def test_removed_distinguishes_your_decision_from_the_sweep(
    client: TestClient, engine: Engine
) -> None:
    """
    `REMOVED` is a decision you made; `GC_SWEPT` is a consequence the system
    drew from it. R2.6 keeps them apart in its response and so does this --
    merging them would answer "why is this gone?" with "it is gone".
    """
    liked = _node(engine, "L", "LIKED")
    orphan = _node(engine, "C")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": orphan, "b": liked},
        )
    # Un-liking costs the graph its only anchor, so the sweep collects C.
    client.patch(f"/api/sessions/{SID}/nodes/{liked}", json={"state": "CANDIDATE"})

    by_reason = {r["reason_code"]: r["count"] for r in _review(client)["removed"]["by_reason"]}  # type: ignore[index,union-attr]
    assert by_reason == {"GC_SWEPT": 1}
    assert orphan not in (0,)


def test_a_restored_paper_leaves_the_removed_tab(client: TestClient, engine: Engine) -> None:
    """
    BUILD.md's other verification: "Restore works". The drawer has to reflect
    it, or the user restores a paper and watches it sit there looking removed.
    """
    seed = _node(engine, "S", "SEED")
    victim = _node(engine, "B")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
            ),
            {"a": victim, "b": seed},
        )
    client.delete(f"/api/sessions/{SID}/nodes/{victim}", params={"dry_run": "false"})
    assert _review(client)["removed"]["total"] == 1  # type: ignore[index]

    assert client.post(f"/api/sessions/{SID}/nodes/{victim}/restore").status_code == 200
    assert _review(client)["removed"]["total"] == 0  # type: ignore[index]


# --------------------------------------------------------------------------
# Rows: enough to act on, capped so the endpoint stays fast
# --------------------------------------------------------------------------


def test_rows_carry_what_a_row_needs_to_render(client: TestClient, engine: Engine) -> None:
    """A row you cannot read is not restorable in any useful sense."""
    paper_id = _paper(engine, "x")
    _decide(engine, paper_id, "REJECT", "IS_DATASET", stage="TYPE")

    row = _review(client)["rejected"]["papers"][0]  # type: ignore[index]
    assert row["paper_id"] == paper_id
    assert row["title"] == "Paper x"
    assert row["reason_code"] == "IS_DATASET"
    assert row["stage"] == "TYPE"


def test_rows_are_capped_and_say_so(client: TestClient, engine: Engine) -> None:
    """
    Counts stay complete while rows are a sample. `truncated` states it rather
    than leaving the client to infer it from a suspiciously round number.
    """
    for i in range(12):
        _decide(engine, _paper(engine, f"r{i}"), "REJECT", "IS_DATASET")

    body = _review(client, limit=5)
    assert body["rejected"]["total"] == 12  # type: ignore[index]
    assert len(body["rejected"]["papers"]) == 5  # type: ignore[index,arg-type]
    assert body["rejected"]["truncated"] is True  # type: ignore[index]


def test_nothing_is_truncated_when_everything_fits(client: TestClient, engine: Engine) -> None:
    _decide(engine, _paper(engine, "r"), "REJECT", "IS_DATASET")
    assert _review(client, limit=5)["rejected"]["truncated"] is False  # type: ignore[index]


def test_an_absurd_limit_is_refused(client: TestClient, engine: Engine) -> None:
    assert client.get(f"/api/sessions/{SID}/review", params={"limit": 100_000}).status_code == 422
    assert client.get(f"/api/sessions/{SID}/review", params={"limit": 0}).status_code == 422


# --------------------------------------------------------------------------
# Scoping and contract
# --------------------------------------------------------------------------


def test_an_empty_session_reports_three_empty_tabs(client: TestClient) -> None:
    """
    A new session opens the drawer too. Every tab present and empty, so the UI
    renders its three headings from the shape rather than from a special case.
    """
    body = _review(client)
    for tab in ("quarantined", "rejected", "removed"):
        assert body[tab]["total"] == 0, tab  # type: ignore[index]
        assert body[tab]["papers"] == []  # type: ignore[index]
        assert body[tab]["by_reason"] == []  # type: ignore[index]


def test_another_sessions_removals_are_not_listed(client: TestClient, engine: Engine) -> None:
    """
    Removal is an opinion, and opinions are session-scoped. A paper you removed
    in session 1 is not removed in session 2.
    """
    paper_id = _paper(engine, "other")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        conn.execute(
            text(
                "INSERT INTO interaction_events (session_id, paper_id, event_type, actor,"
                " created_at) VALUES (2, :p, 'REMOVED', 'USER', '2026-01-01')"
            ),
            {"p": paper_id},
        )
    assert _review(client)["removed"]["total"] == 0  # type: ignore[index]


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.get("/api/sessions/999/review").status_code == 404


def test_the_route_is_in_the_openapi_schema(client: TestClient) -> None:
    assert "/api/sessions/{sid}/review" in client.get("/openapi.json").json()["paths"]
