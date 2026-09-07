# legacy/ — archived Flask prototype

Archived 2026-09-07 when the repo started executing `docs/BUILD.md` from R0.1.

This is the pre-plan spike: Flask + vanilla JS/D3 + raw `sqlite3`. It is **not**
a partial implementation of `docs/PLAN.md` — it diverges on every locked
decision (FastAPI, SQLAlchemy Core + Alembic, React/Vite/Cytoscape, the
`session_id` contract, the `repo/`/`services/`/`api/` layer boundaries).
It is kept for reference only: nothing here is on the build path, and it is
excluded from `ruff`, `mypy`, and `pytest` collection.

## Known-broken on arrival

`legacy/tests/test_review_findings_v2.py` holds 21 reproducers for 10 real bugs
found in a code review just before archiving. They were deliberately left RED —
see the top of that file for the finding-by-finding breakdown. The four that
corrupt network data are worth re-reading before the same logic is rebuilt:

| # | Where | Defect |
|---|---|---|
| F1 | `field_filter.py` | applied term `"law"` swallows core `"scaling law"`/`"scaling laws"` |
| F2 | `expansion.py` | `store.update_distance()` never called, so added papers score 0 |
| F3 | `expansion.py` | `result["added"]` counts papers that already existed |
| F4 | `expansion.py` | citation budget is decremented for already-known papers |

## Still worth mining

- `semantic_scholar_client.py` — `NEIGHBOR_FIELDS` vs `SEARCH_FIELDS`. The
  `/references` and `/citations` endpoints reject `authors.hIndex` with a 400.
  Rediscovering that cost a day. Carry it into R0.7.
- `field_filter.py` — the AI/DS subdomain and venue lists. Their *content* is
  reusable; their substring-containment matching is what produced F1. In the
  rebuild these become `config/filters.yaml` (BUILD.md Appendix A).
- `paper_store.sqlite3` (repo root, ~6MB of real fetched papers and edges) is
  **not** archived here. It stays put for migration into `data/app.db`.
