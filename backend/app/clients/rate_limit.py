"""
Pre-emptive client-side rate limiting.

This sleeps *before* a request rather than backing off after a 429. By the time
S2 returns a 429 the request is already spent, and a loop that only reacts to
them spends its whole budget discovering the limit. Tenacity's retry layer
(R0.7) still handles the 429s that arrive anyway -- shared limits, other
clients -- but it should be the exception, not the pacing mechanism.

Skeleton from BUILD.md Appendix B.1, plus the two guards a bare implementation
of it lacks: a positive-rate check, and rejecting `acquire(n > capacity)`, which
would otherwise spin forever waiting for tokens the bucket can never hold.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """
    Classic token bucket. `rate` is the long-run average requests per second;
    `capacity` is how much burst is allowed after an idle period.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.rate = rate
        # max(1.0, rate) so a sub-1/s rate (the documented S2 limit without a
        # key) still admits a single request instead of deadlocking.
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        if self.capacity <= 0:
            raise ValueError(f"capacity must be positive, got {self.capacity}")
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        if n > self.capacity:
            raise ValueError(f"cannot acquire {n} tokens from a bucket of capacity {self.capacity}")
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self.rate)


__all__ = ["TokenBucket"]
