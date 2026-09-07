"""
Typer CLI. Stays useful forever (PLAN.md section K) -- it is the only way to
exercise the pipeline before the web layer exists at R1.14.

    python -m app.cli fetch "Attention Is All You Need"
    python -m app.cli filter-report --limit 50

Running `fetch` twice is BUILD.md's R0.8 checkpoint: the second run must report
`api_calls=0`. The counters print on every run so that is observable rather than
something you have to instrument.

`filter-report` is R1.7's checkpoint. Its value is not the code -- it is that a
human reads the table and notices the cascade is wrong in a way no unit test
would flag, because unit tests assert what you already believed.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

import typer

from app.clients.cache import ResponseCache
from app.clients.s2 import S2Client
from app.config import filters, settings
from app.db import make_engine
from app.logging_setup import bind_session, configure_logging
from app.repo import papers as papers_repo
from app.services.categories import CategoryResolver
from app.services.filters.cascade import CascadeStats, run_cascade

app = typer.Typer(add_completion=False, help="Citation-graph paper recommender.")


@app.callback()
def main() -> None:
    """
    Root callback.

    Without it Typer collapses a lone command into the app default, so
    `python -m app.cli fetch "..."` would parse "fetch" as the title.

    It is also the single place logging is configured, before any command runs.
    """
    configure_logging(level=settings.log_level)
    bind_session(session_id=settings.session_id)


def _make_client() -> tuple[S2Client, ResponseCache]:
    engine = make_engine()
    cache = ResponseCache(engine)
    key = settings.s2_api_key.get_secret_value() if settings.s2_api_key else None
    return S2Client(cache=cache, api_key=key, rate=settings.s2_rate_limit), cache


# ---------------------------------------------------------------------------
# fetch -- R0.8 and R0.11 checkpoints
# ---------------------------------------------------------------------------


async def _fetch(title: str, limit: int) -> int:
    client, _ = _make_client()
    # S2's fieldsOfStudy is too coarse to separate cs.CL from cs.CV, so the
    # fine-grained category comes from the arxiv_meta join.
    resolver = CategoryResolver(make_engine())
    try:
        stubs = await client.search_title(title, limit=limit)
        if not stubs:
            typer.echo(f"no match for {title!r}")
        else:
            papers = resolver.enrich(await client.get_papers([s.s2_paper_id for s in stubs[:1]]))
            for paper in papers:
                typer.echo(
                    json.dumps(
                        {
                            "s2_paper_id": paper.s2_paper_id,
                            "title": paper.title,
                            "year": paper.year,
                            "venue": paper.venue,
                            "citation_count": paper.citation_count,
                            "arxiv_id": paper.arxiv_id,
                            "primary_arxiv_category": paper.primary_arxiv_category,
                            "doi": paper.doi,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
    finally:
        # Printed unconditionally: this line IS the R0.8 checkpoint.
        typer.echo(f"api_calls={client.api_calls}, cache_hits={client.cache_hits}")
        await client.aclose()
    return 0


@app.command()
def fetch(title: str, limit: int = 10) -> None:
    """Fetch one paper by title and print its normalized metadata."""
    raise typer.Exit(asyncio.run(_fetch(title, limit)))


# ---------------------------------------------------------------------------
# filter-report -- R1.7 checkpoint
# ---------------------------------------------------------------------------

_COLUMNS = (58, 6, 14, 11, 24)
_HEADERS = ("title", "year", "primary_cat", "outcome", "reason_code")


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One line of the filter report."""

    title: str
    year: int | None
    primary_category: str | None
    outcome: str
    reason_code: str


def _cell(value: object, width: int) -> str:
    """
    Render one cell, truncating rather than wrapping.

    None renders as "-", not "None": in a table "None" reads as a value while a
    dash reads as absence, and the difference matters when you are scanning
    fifty rows for the papers that lack a category.
    """
    rendered = "-" if value is None else str(value)
    if len(rendered) > width:
        rendered = rendered[: width - 1] + "…"
    return rendered.ljust(width)


def format_report(rows: list[ReportRow]) -> str:
    """
    The table BUILD.md asks for.

    Row order is preserved deliberately: it is the order S2 returned the
    references in, which keeps two runs comparable line by line.
    """
    lines = [
        "".join(_cell(h, w) for h, w in zip(_HEADERS, _COLUMNS, strict=True)),
        "-" * sum(_COLUMNS),
    ]
    for row in rows:
        values = (row.title, row.year, row.primary_category, row.outcome, row.reason_code)
        lines.append("".join(_cell(v, w) for v, w in zip(values, _COLUMNS, strict=True)))
    return "\n".join(lines)


def summarize(rows: list[ReportRow]) -> dict[tuple[str, str], int]:
    """(outcome, reason_code) -> count, most common first."""
    return dict(Counter((r.outcome, r.reason_code) for r in rows).most_common())


async def _filter_report(title: str, limit: int) -> int:
    client, _ = _make_client()
    engine = make_engine()
    resolver = CategoryResolver(engine)
    stats = CascadeStats()
    rows: list[ReportRow] = []
    as_of = datetime.now(UTC).year

    try:
        stubs = await client.search_title(title, limit=5)
        if not stubs:
            typer.echo(f"no match for {title!r}")
            return 1
        seed = stubs[0]
        typer.echo(f"references of: {seed.title}")
        typer.echo("")

        edges = await client.get_references(seed.s2_paper_id, limit=limit)
        neighbours = [e.paper for e in edges[:limit] if e.paper is not None]
        enriched = resolver.enrich(neighbours)

        with engine.begin() as conn:
            for paper in enriched:
                paper_id = papers_repo.upsert_paper(conn, paper)
                decision = run_cascade(
                    conn, settings.session_id, paper_id, paper, filters, as_of, stats=stats
                )
                rows.append(
                    ReportRow(
                        title=paper.title,
                        year=paper.year,
                        primary_category=paper.primary_arxiv_category,
                        outcome=str(decision.outcome),
                        reason_code=decision.reason_code,
                    )
                )
    finally:
        await client.aclose()

    typer.echo(format_report(rows))
    typer.echo("")
    typer.echo(f"{len(rows)} references, arXiv category coverage {resolver.coverage:.0%}")
    typer.echo(f"cascade: evaluated={stats.evaluated}, cache_hits={stats.cache_hits}")
    typer.echo(f"api_calls={client.api_calls}, cache_hits={client.cache_hits}")
    typer.echo("")
    for (outcome, reason), n in summarize(rows).items():
        typer.echo(f"  {n:>4}  {outcome:<11} {reason}")
    engine.dispose()
    return 0


@app.command("filter-report")
def filter_report(title: str = "BERT", limit: int = 50) -> None:
    """Fetch a paper's references, run the cascade, print the verdict table."""
    raise typer.Exit(asyncio.run(_filter_report(title, limit)))


if __name__ == "__main__":
    app()
