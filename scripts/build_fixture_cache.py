#!/usr/bin/env python
"""
Build backend/tests/fixtures/s2_cache.db -- the committed offline fixture.

    uv run python scripts/build_fixture_cache.py

BUILD.md R0.9 calls this the single highest-leverage two hours in the project.
The DB it produces is what lets every filter, dedup and ranking test run with
the network genuinely unavailable rather than merely mocked.

The paper list is not arbitrary. Each entry exists to exercise one branch of the
filter cascade, so that a rule change shows up as a specific fixture flipping
rather than as a vague drop in accepted papers. Roles are documented inline and
mirrored by one test each in tests/integration/test_s2_fixtures.py.

This script makes real network calls -- roughly two per title plus one
references fetch, paced at the configured S2 rate. It is idempotent: rerunning
it costs nothing for titles already cached, so extending the list is cheap.
"""

from __future__ import annotations

import asyncio

from alembic import command
from alembic.config import Config

from app.clients.cache import ResponseCache
from app.clients.s2 import S2Client, S2TransientError
from app.config import REPO_ROOT, settings
from app.db import make_engine

FIXTURE_DB = REPO_ROOT / "backend" / "tests" / "fixtures" / "s2_cache.db"
ALEMBIC_INI = REPO_ROOT / "backend" / "migrations" / "alembic.ini"

# (title, why it is here). The "why" is the contract -- if you remove an entry,
# the cascade branch it covered stops being tested.
FIXTURES: list[tuple[str, str]] = [
    ("Attention Is All You Need", "hub; forward-expand guard; arXiv/NeurIPS duplicate pair"),
    (
        "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "primary cs.CL",
    ),
    (
        "Efficient Estimation of Word Representations in Vector Space",
        "2013 boundary paper -> PRE_ERA, kept for bibliographic coupling",
    ),
    ("ImageNet: A Large-Scale Hierarchical Image Database", "IS_DATASET -> REJECT"),
    ("A Comprehensive Survey on Graph Neural Networks", "high-citation survey -> accept path"),
    (
        "Dermatologist-level classification of skin cancer with deep neural networks",
        "medical AI -> FIELD_NON_CS",
    ),
    ("Deep Residual Learning for Image Recognition", "primary cs.CV -> CAT_PRIMARY_APPLIED"),
    (
        "Batch Normalization: Accelerating Deep Network Training by Reducing Internal "
        "Covariate Shift",
        "cs.LG cross-listed cs.CV -- MUST PASS; stops the deny list overreaching",
    ),
    ("Adam: A Method for Stochastic Optimization", "core venue (ICLR); VENUE_CORE fallback"),
    ("Language Models are Few-Shot Learners", "hub; GPT-3"),
    # The user's own seed corpus, so R1.12's seeding path is testable offline.
    ("ReAct: Synergizing Reasoning and Acting in Language Models", "seed corpus; agentic"),
    ("Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", "seed corpus; RAG"),
    (
        "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
        "seed corpus; recent, RL for reasoning",
    ),
    ("Reflexion: Language Agents with Verbal Reinforcement Learning", "seed corpus; agentic"),
    (
        "MemoryBank: Enhancing Large Language Models with Long-Term Memory",
        "seed corpus; low-citation recent paper",
    ),
    (
        "Multi-Agent Collaboration via Evolving Orchestration",
        "seed corpus; likely cs.MA, the least obvious CORE_ALLOW member",
    ),
]

# Fetch references for these only -- references are the expensive call.
# BERT is here because BUILD.md's R1.11 and R1.13 verifications both seed BERT
# and expand from it, so its bibliography has to be available offline.
WITH_REFERENCES = {
    "Attention Is All You Need",
    "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
}


async def build() -> int:
    FIXTURE_DB.parent.mkdir(parents=True, exist_ok=True)

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{FIXTURE_DB.as_posix()}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    cache = ResponseCache(engine)
    key = settings.s2_api_key.get_secret_value() if settings.s2_api_key else None
    client = S2Client(cache=cache, api_key=key, rate=settings.s2_rate_limit)

    missing: list[str] = []
    try:
        for title, why in FIXTURES:
            # One title that S2 rate-limits must not discard the twenty already
            # fetched. A partial fixture is reported and re-runnable -- the
            # rows already written are cache hits on the next attempt, so a
            # retry costs only the titles that actually failed.
            try:
                stubs = await client.search_title(title)
            except S2TransientError as exc:
                print(f"  FAIL  {title[:58]:58} {exc}")
                missing.append(title)
                continue
            if not stubs:
                print(f"  MISS  {title}\n        ({why})")
                missing.append(title)
                continue
            top = stubs[0]
            try:
                await client.get_papers([top.s2_paper_id])
                if title in WITH_REFERENCES:
                    refs = await client.get_references(top.s2_paper_id)
                    print(f"  ok    {title[:58]:58} +{len(refs)} refs")
                else:
                    print(f"  ok    {title[:58]:58} {top.year}")
            except S2TransientError as exc:
                print(f"  FAIL  {title[:58]:58} {exc}")
                missing.append(title)
    finally:
        await client.aclose()
        engine.dispose()

    print(f"\napi_calls={client.api_calls}, cache_hits={client.cache_hits}")
    print(f"fixture: {FIXTURE_DB.relative_to(REPO_ROOT)} ({FIXTURE_DB.stat().st_size // 1024} KB)")
    if missing:
        print(f"\n{len(missing)} title(s) did not resolve:")
        for title in missing:
            print(f"  - {title}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(build()))
