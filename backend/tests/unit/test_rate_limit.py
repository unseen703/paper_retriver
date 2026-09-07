"""
R0.5 -- token-bucket rate limiter.

This is the only thing standing between the expansion loop and an S2 ban. It is
pre-emptive: it sleeps *before* the request rather than backing off after a 429,
because by the time you see a 429 you have already spent the request.

Timing tests use generous bands. The point is "does it actually pace", not
"is it accurate to the millisecond" -- a tight band here would flake on a loaded
Windows box and teach everyone to ignore the suite.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.clients.rate_limit import TokenBucket


def test_capacity_defaults_to_rate() -> None:
    assert TokenBucket(rate=10).capacity == 10.0


def test_capacity_is_at_least_one_even_for_slow_rates() -> None:
    """At the documented S2 rate of 1 req/sec a zero capacity would deadlock."""
    assert TokenBucket(rate=0.2).capacity == 1.0


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


async def test_first_acquisition_is_immediate() -> None:
    """The bucket starts full; the first call must not pay for tokens it has."""
    bucket = TokenBucket(rate=1.0)
    start = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - start < 0.1


async def test_five_acquisitions_at_ten_per_second_take_about_four_tenths() -> None:
    """BUILD.md R0.5 verification, verbatim: >= 0.4s and < 0.6s."""
    bucket = TokenBucket(rate=10.0, capacity=1.0)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert 0.4 <= elapsed < 0.6, f"took {elapsed:.3f}s"


async def test_a_full_bucket_lets_a_burst_through() -> None:
    """Capacity is what makes bursts possible; rate is the long-run average."""
    bucket = TokenBucket(rate=1.0, capacity=5.0)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.2


async def test_tokens_refill_over_time() -> None:
    bucket = TokenBucket(rate=20.0, capacity=1.0)
    await bucket.acquire()
    await asyncio.sleep(0.15)  # ~3 tokens' worth, capped at capacity
    start = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - start < 0.05


async def test_refill_is_capped_at_capacity() -> None:
    """Idling for a minute must not buy a 60-request burst."""
    bucket = TokenBucket(rate=10.0, capacity=2.0)
    await asyncio.sleep(0.5)  # would be 5 tokens uncapped
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    # 2 free from capacity, 2 more at 10/s = ~0.2s
    assert time.monotonic() - start >= 0.15


async def test_concurrent_callers_are_serialized_not_starved() -> None:
    """Ten coroutines through a 1-capacity bucket still respect the rate."""
    bucket = TokenBucket(rate=50.0, capacity=1.0)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(10)))
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed < 0.40, f"took {elapsed:.3f}s"


async def test_acquiring_more_than_one_token_costs_proportionally() -> None:
    """A batch POST covering 3 ids should pay for 3, not for 1."""
    bucket = TokenBucket(rate=10.0, capacity=3.0)
    await bucket.acquire(3.0)  # drains the full bucket
    start = time.monotonic()
    await bucket.acquire(3.0)  # must wait for all 3 to refill: 3/10s
    assert time.monotonic() - start >= 0.25


async def test_acquiring_more_than_capacity_is_rejected() -> None:
    """Would otherwise loop forever: the bucket can never hold that many."""
    bucket = TokenBucket(rate=10.0, capacity=2.0)
    with pytest.raises(ValueError):
        await bucket.acquire(3.0)
