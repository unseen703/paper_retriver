"""
Typer CLI. Stays useful forever (PLAN.md section K) -- it is the only way to
exercise the client before the web layer exists at R1.14.

    python -m app.cli fetch "Attention Is All You Need"

Running it twice is BUILD.md's R0.8 checkpoint: the second run must report
`api_calls=0`. The counters are printed on every run so that is observable
rather than something you have to instrument.
"""

from __future__ import annotations

import asyncio
import json

import typer

from app.clients.cache import ResponseCache
from app.clients.s2 import S2Client
from app.config import settings
from app.db import make_engine

app = typer.Typer(add_completion=False, help="Citation-graph paper recommender.")


def _make_client() -> tuple[S2Client, ResponseCache]:
    engine = make_engine()
    cache = ResponseCache(engine)
    key = settings.s2_api_key.get_secret_value() if settings.s2_api_key else None
    return (
        S2Client(cache=cache, api_key=key, rate=settings.s2_rate_limit),
        cache,
    )


async def _fetch(title: str, limit: int) -> int:
    client, _ = _make_client()
    try:
        stubs = await client.search_title(title, limit=limit)
        if not stubs:
            typer.echo(f"no match for {title!r}")
        else:
            papers = await client.get_papers([s.s2_paper_id for s in stubs[:1]])
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


if __name__ == "__main__":
    app()
