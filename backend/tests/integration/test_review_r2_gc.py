"""
Reproducers for the R2.5-R2.8 review findings.

Two of them are the same defect wearing different clothes: **the sweep is
allowed to undo a decision the user made explicitly.**

`gc.py` says `protect` exists because sweeping a just-un-liked paper "would
turn 'unlike' into 'delete', which is a different verb with a different audit
trail". `removal.py` says restore "is the only way back, and never automatic".
Neither claim is true as written -- `protect` exempts a paper from *one*
sweep, so the next unrelated label change collects it, and a restored paper
comes back as an ordinary candidate that the next sweep is free to take. The
user's action is not reversed immediately, which would at least be visible; it
is reversed later, by something they did somewhere else.

The third finding is quieter: a restored paper comes back at depth 1 whatever
depth it had, so the graph's account of how far a paper sits from a seed is
wrong for every restored node.

The fourth is a response that does not say what happened. `apply_label`
computes which papers the sweep collected and the endpoint drops it, so a
client is told "this node is now a CANDIDATE" while twelve others silently
left the graph.
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


class Graph:
    """A tiny graph builder. `depth` is explicit because one test is about it."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    def node(self, name: str, state: str = "CANDIDATE", depth: int | None = None) -> Graph:
        with self.engine.begin() as conn:
            paper_id = conn.execute(
                text(
                    "INSERT INTO papers (s2_paper_id, title, title_norm, first_seen_at)"
                    " VALUES (:s, :t, :t, '2026-01-01') RETURNING id"
                ),
                {"s": name, "t": f"Paper {name}"},
            ).scalar()
            assert paper_id is not None
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, paper_id, state, depth)"
                    " VALUES (:sid, :pid, :st, :d)"
                ),
                {
                    "sid": SID,
                    "pid": paper_id,
                    "st": state,
                    "d": depth if depth is not None else (0 if state == "SEED" else 1),
                },
            )
        self.ids[name] = int(paper_id)
        return self

    def edge(self, citing: str, cited: str) -> Graph:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO edges (citing_id, cited_id, discovered_via, first_seen_at)"
                    " VALUES (:a, :b, 'BACKWARD', '2026-01-01')"
                ),
                {"a": self.ids[citing], "b": self.ids[cited]},
            )
        return self

    def present(self) -> set[str]:
        back = {v: k for k, v in self.ids.items()}
        with self.engine.connect() as conn:
            return {
                back[r[0]]
                for r in conn.execute(
                    text("SELECT paper_id FROM graph_nodes WHERE session_id=:s"), {"s": SID}
                )
            }

    def depth(self, name: str) -> int | None:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT depth FROM graph_nodes WHERE session_id=:s AND paper_id=:p"),
                {"s": SID, "p": self.ids[name]},
            ).scalar()


def _label(client: TestClient, paper_id: int, state: str) -> object:
    return client.patch(f"/api/sessions/{SID}/nodes/{paper_id}", json={"state": state})


def _churn(client: TestClient, graph: Graph, name: str) -> None:
    """
    Trigger a sweep that has nothing to do with the paper under test: like an
    unrelated node, then un-like it. This is an ordinary next action, and it is
    what makes a deferred collection so bad -- nothing about what the user did
    names the paper it takes away.
    """
    _label(client, graph.ids[name], "LIKED")
    _label(client, graph.ids[name], "CANDIDATE")


# --------------------------------------------------------------------------
# Finding 1 -- protection that only lasts one sweep
# --------------------------------------------------------------------------


def test_an_unliked_paper_survives_a_later_unrelated_sweep(
    client: TestClient, engine: Engine
) -> None:
    """
    `gc.py`: "The user asked to stop favouring a paper, not to lose it."

    That has to hold for longer than one operation. Un-like L, then do
    something else entirely -- L must still be there.
    """
    graph = Graph(engine).node("S", "SEED").node("L", "LIKED").node("D")
    graph.edge("D", "S")  # D hangs off the seed, so churning it sweeps nothing

    _label(client, graph.ids["L"], "CANDIDATE")
    assert "L" in graph.present(), "the sweep the un-like triggered spared it"

    _churn(client, graph, "D")
    assert "L" in graph.present(), "an unrelated label change must not collect it later"


# --------------------------------------------------------------------------
# Finding 2 -- restore undone by the next sweep
# --------------------------------------------------------------------------


def test_a_restored_paper_survives_a_later_unrelated_sweep(
    client: TestClient, engine: Engine
) -> None:
    """
    PLAN.md makes restore the only path back, precisely so nothing puts a paper
    back or takes it away without the user saying so. A sweep that collects a
    just-restored paper is the tool overruling the one action it promised to
    treat as final.
    """
    graph = Graph(engine).node("S", "SEED").node("L", "LIKED").node("C").node("D")
    graph.edge("C", "L")  # C is justified only by L
    graph.edge("D", "S")

    _label(client, graph.ids["L"], "CANDIDATE")  # L stops anchoring; C is swept
    assert "C" not in graph.present()

    assert client.post(f"/api/sessions/{SID}/nodes/{graph.ids['C']}/restore").status_code == 200
    assert "C" in graph.present()

    _churn(client, graph, "D")
    assert "C" in graph.present(), "a restored paper must not be swept away again"


def test_an_untouched_candidate_is_still_swept(client: TestClient, engine: Engine) -> None:
    """
    The guard against over-correcting. Protecting everything the user has ever
    seen would defeat the sweep entirely -- an orphaned candidate nobody has
    touched is exactly what mark-and-sweep is for.
    """
    graph = Graph(engine).node("S", "SEED").node("L", "LIKED").node("C")
    graph.edge("C", "L")

    _label(client, graph.ids["L"], "CANDIDATE")
    assert "C" not in graph.present(), "an orphaned candidate nobody touched is still collected"


# --------------------------------------------------------------------------
# Finding 3 -- restore invents a depth
# --------------------------------------------------------------------------


def test_restore_preserves_the_nodes_depth(client: TestClient, engine: Engine) -> None:
    """
    Depth is the graph's account of how far a paper sits from a seed, and both
    the budget and the candidate pool read it. Restoring a depth-3 paper as
    depth-1 does not merely lose information, it asserts something false.
    """
    graph = Graph(engine).node("S", "SEED").node("B", "CANDIDATE", depth=3)
    graph.edge("B", "S")

    client.delete(f"/api/sessions/{SID}/nodes/{graph.ids['B']}", params={"dry_run": "false"})
    assert client.post(f"/api/sessions/{SID}/nodes/{graph.ids['B']}/restore").status_code == 200
    assert graph.depth("B") == 3


def test_a_swept_paper_keeps_its_depth_through_restore(client: TestClient, engine: Engine) -> None:
    """The same for a paper the sweep took rather than one the user removed."""
    graph = Graph(engine).node("S", "SEED").node("L", "LIKED").node("C", "CANDIDATE", depth=2)
    graph.edge("C", "L")

    _label(client, graph.ids["L"], "CANDIDATE")
    assert "C" not in graph.present()
    client.post(f"/api/sessions/{SID}/nodes/{graph.ids['C']}/restore")
    assert graph.depth("C") == 2


# --------------------------------------------------------------------------
# Finding 4 -- a response that does not mention what left
# --------------------------------------------------------------------------


def test_the_label_response_reports_what_the_sweep_collected(
    client: TestClient, engine: Engine
) -> None:
    """
    `apply_label` already computes this and the endpoint throws it away. A
    client told only "this node is a CANDIDATE now" will redraw one node while
    the rest of its picture is stale.
    """
    graph = Graph(engine).node("S", "SEED").node("L", "LIKED").node("C")
    graph.edge("C", "L")

    body = _label(client, graph.ids["L"], "CANDIDATE").json()  # type: ignore[attr-defined]
    assert body["swept"] == [graph.ids["C"]]


def test_the_label_response_reports_an_empty_sweep_as_empty(
    client: TestClient, engine: Engine
) -> None:
    """Present-and-empty, so a client reads the shape rather than a special case."""
    graph = Graph(engine).node("S", "SEED").node("B")
    graph.edge("B", "S")
    body = _label(client, graph.ids["B"], "LIKED").json()  # type: ignore[attr-defined]
    assert body["swept"] == []
