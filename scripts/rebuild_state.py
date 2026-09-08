#!/usr/bin/env python
"""
Rebuild `graph_nodes.state` from `interaction_events` (R2.3).

    uv run python scripts/rebuild_state.py [--session 1] [--check]

BUILD.md calls this the event-log safety net. `graph_nodes.state` is a
materialised projection and the log is the source of truth; this is the thing
that makes that claim checkable rather than merely stated.

`--check` reports disagreements and changes nothing, which is the form worth
running on a schedule: a return of 0 is the healthy answer, and the first
non-zero result is the first evidence that some write path forgot its event.
"""

from __future__ import annotations

import argparse

from app.db import make_engine
from app.services.rebuild import rebuild_states, states_from_events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=int, default=1)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report disagreements without writing; exit 1 if any are found",
    )
    args = parser.parse_args()

    engine = make_engine()
    try:
        if args.check:
            with engine.connect() as conn:
                derived = states_from_events(conn, args.session)
                from sqlalchemy import text

                current = {
                    row[0]: row[1]
                    for row in conn.execute(
                        text("SELECT paper_id, state FROM graph_nodes WHERE session_id = :sid"),
                        {"sid": args.session},
                    )
                }
            drift = [
                (pid, current[pid], state)
                for pid, state in derived.items()
                if pid in current and current[pid] != state
            ]
            for paper_id, materialised, from_log in drift:
                print(f"  paper {paper_id}: table={materialised} log={from_log}")
            print(f"{len(drift)} disagreement(s) in session {args.session}")
            return 1 if drift else 0

        changed = rebuild_states(engine, args.session)
        print(f"{changed} row(s) corrected in session {args.session}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
