"""
Joins `arxiv_meta` onto freshly fetched papers so `primary_arxiv_category`
populates (BUILD.md R0.11).

S2 gives coarse `fieldsOfStudy` -- "Computer Science" -- which cannot separate
cs.CL from cs.CV. The whole topic filter depends on the fine-grained arXiv
category, so this join is what makes `CAT_PRIMARY_APPLIED` possible at all.

The category is read from the *primary* only. Batch Normalization is `cs.LG`
cross-listed `cs.CV`; denying on membership rather than primary would reject a
large slice of core ML.

`coverage` exists because a NULL category is indistinguishable from "this paper
genuinely has no arXiv entry". Tracking the ratio turns a silently stale
snapshot into a visible number.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import Engine

from app.models import Paper
from app.repo import arxiv_meta


class CategoryResolver:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.queries = 0
        self.with_arxiv_id = 0
        self.resolved = 0

    @property
    def coverage(self) -> float:
        """Fraction of arXiv-identified papers that got a category."""
        if not self.with_arxiv_id:
            return 0.0
        return self.resolved / self.with_arxiv_id

    def primary_category(self, arxiv_id: str) -> str | None:
        record = arxiv_meta.get(self._engine, arxiv_id)
        return record.primary_category if record else None

    def categories(self, arxiv_id: str) -> tuple[str, ...]:
        record = arxiv_meta.get(self._engine, arxiv_id)
        return record.categories if record else ()

    def enrich(self, papers: list[Paper]) -> list[Paper]:
        """
        Return copies carrying their arXiv categories.

        One bulk lookup, not one per paper: an expansion enriches hundreds of
        candidates at a time, and N+1 here would dominate the wall clock.
        Papers are frozen, so this replaces rather than mutates.
        """
        if not papers:
            return []

        ids = [p.arxiv_id for p in papers if p.arxiv_id]
        if not ids:
            return list(papers)

        found = arxiv_meta.get_many(self._engine, ids)
        self.queries += 1

        out: list[Paper] = []
        for paper in papers:
            if not paper.arxiv_id:
                out.append(paper)
                continue
            self.with_arxiv_id += 1
            record = found.get(paper.arxiv_id)
            if record is None:
                # Newer than the snapshot, or not an arXiv paper after all.
                # Left NULL deliberately; R1.5 must quarantine, never accept.
                out.append(paper)
                continue
            self.resolved += 1
            out.append(
                dataclasses.replace(
                    paper,
                    primary_arxiv_category=record.primary_category,
                    arxiv_categories=record.categories,
                )
            )
        return out


__all__ = ["CategoryResolver"]
