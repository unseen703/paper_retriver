"""
R1.2 -- repo/graph.py and repo/events.py.

The session-scoped half of the contract, and the exact mirror of R1.1: here
`session_id` is the **first positional argument with no default**, on every
function. BUILD.md's reasoning is worth restating -- "if you can call it without
a session, you will eventually call it with the wrong one" -- and the failure
mode is silent cross-session data leakage, which no amount of later testing
surfaces.

`interaction_events` is append-only and `graph_nodes.state` is its materialized
projection. Two consequences the tests pin down:

* **A tombstone is "the latest event for (session, paper) is REMOVED"**, not a
  flag. So RESTORED genuinely un-tombstones, which is what makes R2.8's restore
  path possible at all.
* **Events order by `id`, never by timestamp.** Twenty labels applied inside one
  second have identical ISO timestamps; ordering by them would make R2.3's
  rebuild-from-events non-deterministic.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.db import make_engine
from app.models import Paper
from app.repo import events as events_repo
from app.repo import graph as graph_repo
from app.repo import papers as papers_repo

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"

SESSION_A = 1
SESSION_B = 2


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    db = tmp_path / "graph.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        # Session 1 is seeded by the baseline migration; session 2 exists so the
        # isolation tests have a real FK target rather than a dangling id.
        connection.execute(
            text("INSERT INTO sessions (id, name, created_at) VALUES (2, 'other', '2026-01-01')")
        )
        yield connection
    engine.dispose()


@pytest.fixture
def paper(conn: Connection) -> int:
    return papers_repo.upsert_paper(
        conn, Paper(s2_paper_id="s1", title="Attention Is All You Need", first_seen_at="2026-01-01")
    )


def _paper(conn: Connection, s2_id: str) -> int:
    return papers_repo.upsert_paper(
        conn, Paper(s2_paper_id=s2_id, title=f"Paper {s2_id}", first_seen_at="2026-01-01")
    )


# --------------------------------------------------------------------------
# The contract: session_id first positional, no default
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module", [graph_repo, events_repo], ids=lambda m: m.__name__)
def test_every_public_function_takes_session_id_first(module: object) -> None:
    """
    BUILD.md: "every function takes session_id as its FIRST positional
    argument. No defaults, no Optional."
    """
    offenders = []
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != module.__name__:  # type: ignore[attr-defined]
            continue
        params = list(inspect.signature(fn).parameters.values())
        # params[0] is conn; params[1] must be session_id.
        if len(params) < 2 or params[1].name != "session_id":
            offenders.append(f"{name}: {[p.name for p in params]}")
        elif params[1].default is not inspect.Parameter.empty:
            offenders.append(f"{name}: session_id has a default")
    assert not offenders, f"session_id contract violated: {offenders}"


# --------------------------------------------------------------------------
# Session isolation -- the case BUILD.md names for this task
# --------------------------------------------------------------------------


def test_a_node_added_to_session_1_is_invisible_to_session_2(conn: Connection, paper: int) -> None:
    """BUILD.md R1.2 verification, verbatim."""
    graph_repo.add_node(conn, SESSION_A, paper, "SEED", depth=0)
    assert graph_repo.get_node_ids(conn, SESSION_A) == {paper}
    assert graph_repo.get_node_ids(conn, SESSION_B) == set()


def test_the_same_paper_can_hold_different_states_per_session(conn: Connection, paper: int) -> None:
    """A paper LIKED in one workspace is not liked in another."""
    graph_repo.add_node(conn, SESSION_A, paper, "LIKED", depth=0)
    graph_repo.add_node(conn, SESSION_B, paper, "CANDIDATE", depth=2)
    (a,) = graph_repo.get_nodes(conn, SESSION_A)
    (b,) = graph_repo.get_nodes(conn, SESSION_B)
    assert (a.state, b.state) == ("LIKED", "CANDIDATE")


def test_removing_from_one_session_leaves_the_other_intact(conn: Connection, paper: int) -> None:
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=1)
    graph_repo.add_node(conn, SESSION_B, paper, "CANDIDATE", depth=1)
    graph_repo.remove_node(conn, SESSION_A, paper)
    assert graph_repo.get_node_ids(conn, SESSION_A) == set()
    assert graph_repo.get_node_ids(conn, SESSION_B) == {paper}


def test_events_do_not_leak_across_sessions(conn: Connection, paper: int) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "LIKED")
    assert events_repo.latest_state(conn, SESSION_A, paper) == "LIKED"
    assert events_repo.latest_state(conn, SESSION_B, paper) is None


def test_a_tombstone_in_one_session_is_not_a_tombstone_in_another(
    conn: Connection, paper: int
) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "REMOVED")
    assert events_repo.removed_paper_ids(conn, SESSION_A) == {paper}
    assert events_repo.removed_paper_ids(conn, SESSION_B) == set()


# --------------------------------------------------------------------------
# graph_nodes
# --------------------------------------------------------------------------


def test_add_node_stores_the_documented_fields(conn: Connection, paper: int) -> None:
    graph_repo.add_node(
        conn,
        SESSION_A,
        paper,
        "CANDIDATE",
        depth=2,
        score=1.75,
        features={"overlap": 2.0},
        score_breakdown={"overlap": 4.0},
    )
    (node,) = graph_repo.get_nodes(conn, SESSION_A)
    assert node.paper_id == paper
    assert node.state == "CANDIDATE"
    assert node.depth == 2
    assert node.score == 1.75
    assert node.features == {"overlap": 2.0}
    assert node.score_breakdown == {"overlap": 4.0}


def test_add_node_is_idempotent(conn: Connection, paper: int) -> None:
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=1)
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=1)
    n = conn.execute(text("SELECT COUNT(*) FROM graph_nodes")).scalar()
    assert n == 1


def test_re_adding_updates_the_state(conn: Connection, paper: int) -> None:
    """Re-encountering a candidate at a shorter depth should update it."""
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=3)
    graph_repo.add_node(conn, SESSION_A, paper, "LIKED", depth=1)
    (node,) = graph_repo.get_nodes(conn, SESSION_A)
    assert node.state == "LIKED"
    assert node.depth == 1


def test_get_nodes_filters_by_state(conn: Connection) -> None:
    seed = _paper(conn, "seed")
    liked = _paper(conn, "liked")
    cand = _paper(conn, "cand")
    graph_repo.add_node(conn, SESSION_A, seed, "SEED", depth=0)
    graph_repo.add_node(conn, SESSION_A, liked, "LIKED", depth=1)
    graph_repo.add_node(conn, SESSION_A, cand, "CANDIDATE", depth=1)
    anchors = graph_repo.get_nodes(conn, SESSION_A, states=["SEED", "LIKED"])
    assert {n.paper_id for n in anchors} == {seed, liked}


def test_get_nodes_with_no_filter_returns_everything(conn: Connection) -> None:
    for i in range(3):
        graph_repo.add_node(conn, SESSION_A, _paper(conn, f"p{i}"), "CANDIDATE", depth=1)
    assert len(graph_repo.get_nodes(conn, SESSION_A)) == 3


def test_get_nodes_with_an_empty_state_list_returns_nothing(conn: Connection, paper: int) -> None:
    """An empty filter means "none of these", not "no filter"."""
    graph_repo.add_node(conn, SESSION_A, paper, "SEED", depth=0)
    assert graph_repo.get_nodes(conn, SESSION_A, states=[]) == []


def test_get_nodes_is_deterministically_ordered(conn: Connection) -> None:
    """CLAUDE.md rule 7: never rely on insertion or set iteration order."""
    ids = [_paper(conn, f"p{i}") for i in range(5)]
    for pid in reversed(ids):
        graph_repo.add_node(conn, SESSION_A, pid, "CANDIDATE", depth=1)
    assert [n.paper_id for n in graph_repo.get_nodes(conn, SESSION_A)] == sorted(ids)


def test_remove_node_is_idempotent(conn: Connection, paper: int) -> None:
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=1)
    graph_repo.remove_node(conn, SESSION_A, paper)
    graph_repo.remove_node(conn, SESSION_A, paper)
    assert graph_repo.get_node_ids(conn, SESSION_A) == set()


def test_remove_node_leaves_the_paper_row_alone(conn: Connection, paper: int) -> None:
    """
    PLAN.md section C: a paper can exist in `papers` without being in the graph.
    Removing a node must not delete the corpus row, or its edges vanish with it.
    """
    graph_repo.add_node(conn, SESSION_A, paper, "CANDIDATE", depth=1)
    graph_repo.remove_node(conn, SESSION_A, paper)
    assert conn.execute(text("SELECT COUNT(*) FROM papers")).scalar() == 1


def test_get_node_ids_returns_a_set(conn: Connection) -> None:
    ids = {_paper(conn, f"p{i}") for i in range(4)}
    for pid in ids:
        graph_repo.add_node(conn, SESSION_A, pid, "CANDIDATE", depth=1)
    assert graph_repo.get_node_ids(conn, SESSION_A) == ids


# --------------------------------------------------------------------------
# interaction_events -- append-only
# --------------------------------------------------------------------------


def test_append_event_returns_the_new_id(conn: Connection, paper: int) -> None:
    event_id = events_repo.append_event(conn, SESSION_A, paper, "SEED_ADDED")
    assert isinstance(event_id, int)
    assert event_id > 0


def test_events_accumulate_rather_than_replace(conn: Connection, paper: int) -> None:
    """Append-only is what makes R2.3's rebuild-from-events possible."""
    for event_type in ("SEED_ADDED", "LIKED", "UNLABELED", "DISLIKED"):
        events_repo.append_event(conn, SESSION_A, paper, event_type)
    assert conn.execute(text("SELECT COUNT(*) FROM interaction_events")).scalar() == 4


def test_latest_state_is_the_most_recent_event(conn: Connection, paper: int) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "LIKED")
    events_repo.append_event(conn, SESSION_A, paper, "DISLIKED")
    assert events_repo.latest_state(conn, SESSION_A, paper) == "DISLIKED"


def test_latest_state_orders_by_id_not_timestamp(conn: Connection, paper: int) -> None:
    """
    Twenty labels inside one second share an ISO timestamp to the microsecond
    on a fast machine. Ordering by it would make R2.3's rebuild
    non-deterministic; the monotonic id is the only safe key.
    """
    now = "2026-09-07T12:00:00+00:00"
    for event_type in ("LIKED", "UNLABELED", "DISLIKED"):
        events_repo.append_event(conn, SESSION_A, paper, event_type, created_at=now)
    assert events_repo.latest_state(conn, SESSION_A, paper) == "DISLIKED"


def test_latest_state_is_none_when_nothing_happened(conn: Connection, paper: int) -> None:
    assert events_repo.latest_state(conn, SESSION_A, paper) is None


def test_actor_defaults_to_user(conn: Connection, paper: int) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "LIKED")
    actor = conn.execute(text("SELECT actor FROM interaction_events")).scalar()
    assert actor == "USER"


def test_the_gc_writes_system_events(conn: Connection, paper: int) -> None:
    """R2.6 distinguishes a user removal from a cascade sweep by actor."""
    events_repo.append_event(conn, SESSION_A, paper, "GC_SWEPT", actor="SYSTEM")
    actor = conn.execute(text("SELECT actor FROM interaction_events")).scalar()
    assert actor == "SYSTEM"


def test_payload_round_trips_as_json(conn: Connection, paper: int) -> None:
    events_repo.append_event(
        conn, SESSION_A, paper, "REMOVED", payload={"cascade_count": 7, "prior_state": "LIKED"}
    )
    (event,) = events_repo.get_events(conn, SESSION_A, paper)
    assert event.payload == {"cascade_count": 7, "prior_state": "LIKED"}


def test_get_events_returns_them_in_order(conn: Connection, paper: int) -> None:
    for event_type in ("SEED_ADDED", "LIKED", "REMOVED"):
        events_repo.append_event(conn, SESSION_A, paper, event_type)
    assert [e.event_type for e in events_repo.get_events(conn, SESSION_A, paper)] == [
        "SEED_ADDED",
        "LIKED",
        "REMOVED",
    ]


# --------------------------------------------------------------------------
# Tombstones -- "latest event is REMOVED", not a flag
# --------------------------------------------------------------------------


def test_a_removed_paper_is_a_tombstone(conn: Connection, paper: int) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "REMOVED")
    assert events_repo.removed_paper_ids(conn, SESSION_A) == {paper}


def test_restoring_lifts_the_tombstone(conn: Connection, paper: int) -> None:
    """
    The reason a tombstone is derived rather than stored: RESTORED (R2.8) is
    the only path back, and it works because "latest event" is a query, not a
    flag someone has to remember to clear.
    """
    events_repo.append_event(conn, SESSION_A, paper, "REMOVED")
    events_repo.append_event(conn, SESSION_A, paper, "RESTORED")
    assert events_repo.removed_paper_ids(conn, SESSION_A) == set()


def test_removing_again_after_a_restore_re_tombstones(conn: Connection, paper: int) -> None:
    for event_type in ("REMOVED", "RESTORED", "REMOVED"):
        events_repo.append_event(conn, SESSION_A, paper, event_type)
    assert events_repo.removed_paper_ids(conn, SESSION_A) == {paper}


def test_a_liked_paper_is_not_a_tombstone(conn: Connection, paper: int) -> None:
    events_repo.append_event(conn, SESSION_A, paper, "LIKED")
    assert events_repo.removed_paper_ids(conn, SESSION_A) == set()


def test_a_gc_sweep_also_tombstones(conn: Connection, paper: int) -> None:
    """R2.7: a swept node must not silently return on the next expansion."""
    events_repo.append_event(conn, SESSION_A, paper, "GC_SWEPT", actor="SYSTEM")
    assert events_repo.removed_paper_ids(conn, SESSION_A) == {paper}


def test_removed_paper_ids_handles_many_papers(conn: Connection) -> None:
    removed = set()
    for i in range(50):
        pid = _paper(conn, f"p{i}")
        events_repo.append_event(conn, SESSION_A, pid, "LIKED")
        if i % 2 == 0:
            events_repo.append_event(conn, SESSION_A, pid, "REMOVED")
            removed.add(pid)
    assert events_repo.removed_paper_ids(conn, SESSION_A) == removed


def test_removed_paper_ids_is_empty_for_a_fresh_session(conn: Connection) -> None:
    assert events_repo.removed_paper_ids(conn, SESSION_A) == set()
