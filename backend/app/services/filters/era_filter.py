"""
Stage 0 of the cascade: the year floor.

The corpus floor is 2015 (a locked decision). Pre-floor papers are still stored
as **boundary papers** -- a `papers` row and its edges, but no `graph_nodes` row
(R1.9) -- because bibliographic coupling depends on shared old references. This
filter decides graph membership, never storage.

`year is None` quarantines rather than accepts. S2 omits `year` constantly, on
preprints and workshop papers and anything with a thin record, so admitting on
absence would leak precisely the papers whose provenance is weakest. Quarantine
keeps them visible and reversible in R2.14's drawer.
"""

from __future__ import annotations

from app.config import FiltersConfig
from app.models import FilterDecision, Paper
from app.services.filters.base import FilterStage, accept, quarantine, reject


def era_filter(paper: Paper, cfg: FiltersConfig) -> FilterDecision:
    if paper.year is None:
        return quarantine(FilterStage.ERA, "YEAR_UNKNOWN", year_floor=cfg.year_floor)
    if paper.year < cfg.year_floor:
        return reject(FilterStage.ERA, "PRE_ERA", year=paper.year, year_floor=cfg.year_floor)
    return accept(FilterStage.ERA, year=paper.year, year_floor=cfg.year_floor)


__all__ = ["era_filter"]
