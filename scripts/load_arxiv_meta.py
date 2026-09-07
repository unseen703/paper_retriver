#!/usr/bin/env python
"""
Load arXiv category metadata into `arxiv_meta`.

    # bulk: the Cornell Kaggle snapshot (reads the .zip directly)
    uv run python scripts/load_arxiv_meta.py --source kaggle --path archive.zip

    # smoke test on a 5.5GB file
    uv run python scripts/load_arxiv_meta.py --source kaggle --path archive.zip --limit 1000

    # delta: everything arXiv revised since the snapshot was cut
    uv run python scripts/load_arxiv_meta.py --source oai

Two sources, one table. The snapshot gives 2.7M rows in one reproducible pass;
the OAI harvest tops up whatever arXiv has published since. Running the OAI pass
without `--since` resumes from `MAX(updated)` already in the table, so the two
meet without a gap.

Why both: the corpus this feeds is 2022-2025 heavy. A snapshot alone leaves the
newest papers with `primary_arxiv_category = NULL`, and NULL is not neutral --
CAT_PRIMARY_APPLIED is what rejects cs.CV, so an uncategorized recent cs.CV
paper is never denied at that stage. It falls through to the weaker
venue/keyword fallback with nothing looking broken.

Idempotent (`ON CONFLICT DO UPDATE`), so an interrupted load is resumed by
simply running it again.
"""

from __future__ import annotations

import asyncio
import sys
import time
from enum import StrEnum
from pathlib import Path

import httpx
import typer

from app.clients.arxiv import (
    OAI_ENDPOINT,
    OAI_RATE,
    ArxivRecord,
    KaggleSnapshotSource,
    OaiPmhSource,
    parse_oai_page,
)
from app.clients.rate_limit import TokenBucket
from app.config import REPO_ROOT
from app.db import make_engine
from app.repo import arxiv_meta

app = typer.Typer(add_completion=False, help="Load arXiv category metadata.")

PROGRESS_EVERY = 50_000


class Source(StrEnum):
    kaggle = "kaggle"
    oai = "oai"


def _progress(label: str, n: int, started: float) -> None:
    rate = n / max(time.monotonic() - started, 1e-9)
    typer.echo(f"  {label}: {n:,} rows ({rate:,.0f}/s)")


def _load_kaggle(engine, path: Path, limit: int | None) -> int:
    source = KaggleSnapshotSource(path)
    started = time.monotonic()
    seen = 0

    def counted() -> object:
        nonlocal seen
        for record in source.records(limit=limit):
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                _progress("loaded", seen, started)
            yield record

    total = arxiv_meta.load(engine, counted())
    typer.echo(f"\nsnapshot: {total:,} rows, {source.skipped:,} unparseable lines skipped")
    if source.max_updated:
        typer.echo(f"newest update_date in snapshot: {source.max_updated}")
    return total


async def _load_oai(engine, since: str | None, limit: int | None) -> int:
    # Resume from what is already stored, so the delta abuts the snapshot
    # rather than overlapping it or leaving a hole.
    since = since or arxiv_meta.max_updated(engine)
    if not since:
        typer.echo("no watermark and no --since: load the snapshot first", err=True)
        return 0

    typer.echo(f"harvesting arXiv OAI-PMH from {since} (set=cs)")
    source = OaiPmhSource(since=since)
    bucket = TokenBucket(rate=OAI_RATE, capacity=1.0)
    started = time.monotonic()
    token: str | None = None
    seen = 0
    batch: list[ArxivRecord] = []

    async with httpx.AsyncClient(timeout=60.0) as http:
        while True:
            await bucket.acquire()
            response = await http.get(OAI_ENDPOINT, params=source.params(token))
            if response.status_code == 503:
                # arXiv throttles harvesters with 503 + Retry-After. This is
                # expected traffic shaping, not an error.
                wait = float(response.headers.get("Retry-After", "20"))
                typer.echo(f"  throttled, sleeping {wait:.0f}s")
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()

            records, token = parse_oai_page(response.text)
            batch.extend(records)
            seen += len(records)
            if len(batch) >= 5_000:
                arxiv_meta.load(engine, batch)
                batch = []
            if seen % PROGRESS_EVERY < len(records):
                _progress("harvested", seen, started)
            if token is None or (limit is not None and seen >= limit):
                break

    if batch:
        arxiv_meta.load(engine, batch)
    typer.echo(f"\ndelta: {seen:,} rows harvested")
    return seen


@app.command()
def load(
    source: Source = Source.kaggle,
    path: Path = REPO_ROOT / "archive.zip",
    limit: int | None = None,
    since: str | None = None,
) -> None:
    engine = make_engine()
    before = arxiv_meta.count(engine)

    if source is Source.kaggle:
        _load_kaggle(engine, path, limit)
    else:
        asyncio.run(_load_oai(engine, since, limit))

    after = arxiv_meta.count(engine)
    typer.echo(f"arxiv_meta: {before:,} -> {after:,} rows (+{after - before:,})")
    typer.echo("\ntop primary categories:")
    for category, n in arxiv_meta.category_counts(engine):
        typer.echo(f"  {category:16} {n:>9,}")
    watermark = arxiv_meta.max_updated(engine)
    typer.echo(f"\nwatermark (resume point for --source oai): {watermark}")
    engine.dispose()


if __name__ == "__main__":
    sys.exit(app())
