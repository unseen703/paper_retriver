"""
The temporal cutoff (R4.2) -- the guard that makes every other number honest.

BUILD.md:

    "**Temporal cutoff enforcement** -- the harness must reject any candidate
    published after `min(seed.publication_date)`, or you leak the future and
    every number is fiction."

The benchmark takes a target paper, samples three of its references as seeds,
and holds the rest back as ground truth. The cutoff is the earliest seed's
publication date: anything after it is the future as far as this case is
concerned, and a system that can see the future scores well for a reason that
has nothing to do with recommending.

**Pure.** Dates in, booleans out. The guard is the single most load-bearing
thing in the harness, so it is testable without a database, a benchmark file or
a network.

**A partial date is a range, and the two sides round in opposite directions.**
For a *seed* the cutoff takes the earliest day the range could mean; for a
*candidate* visibility takes the latest. So "2020" narrows the window from both
ends -- as a seed it means 2020-01-01, as a candidate 2020-12-31. Any other
pairing lets an ambiguous date smuggle a paper through, and the ambiguous ones
are exactly the ones nobody would notice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import TypeVar

ID = TypeVar("ID")

_LAST_DAY_OF_YEAR = (12, 31)


def _parse(raw: str | None, *, latest: bool) -> date | None:
    """
    An ISO-ish date, resolved to a single day, or None if unusable.

    Accepts `YYYY-MM-DD`, `YYYY-MM` and `YYYY`, which is the range S2 actually
    returns. `latest` picks which end of a partial date to resolve to.

    Anything unparseable -- a malformed date, a month of 13, a February 30th --
    returns None rather than raising. A sweep of 300 cases must not end because
    one record is malformed, and None routes to the conservative branch.
    """
    if not raw:
        return None
    parts = raw.strip().split("-")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None

    try:
        if len(numbers) >= 3:
            return date(numbers[0], numbers[1], numbers[2])
        if len(numbers) == 2:
            year, month = numbers
            if latest:
                # The last day of that month, without a calendar table: step
                # to the first of the next month and back off one day.
                following = date(year + (month == 12), (month % 12) + 1, 1)
                return date.fromordinal(following.toordinal() - 1)
            return date(year, month, 1)
        if len(numbers) == 1:
            year = numbers[0]
            return date(year, *_LAST_DAY_OF_YEAR) if latest else date(year, 1, 1)
    except ValueError:
        return None
    return None


def cutoff_from_seeds(published: Iterable[str | None]) -> date | None:
    """
    The earliest usable seed date, or None if there is no usable one.

    `min`, not `max`: the seeds are where the user is imagined to have started,
    and the system may only see what existed when the *first* of them did.
    Taking the latest would hand it a window in which much of the ground truth
    had already appeared.

    None rather than "the beginning of time" when nothing parses. A case with
    no cutoff cannot be evaluated honestly, and returning a permissive default
    would turn an unevaluable case into the best-scoring one in the benchmark.
    """
    dates = [parsed for parsed in (_parse(raw, latest=False) for raw in published) if parsed]
    return min(dates) if dates else None


def visible_at(published: str | None, cutoff: date | None) -> bool:
    """
    Could the system have known about this paper at the cutoff?

    On the cutoff counts as visible: BUILD.md says reject what was published
    *after* it, and a paper the seed could have cited on the day it appeared is
    not the future.

    **An unknown date is not visible.** A paper that cannot be *shown* to
    predate the cutoff is exactly the kind that would leak the future without
    anyone noticing. It costs recall -- an undated paper that really was old
    counts as a miss -- and that is the right direction to be wrong in.
    """
    if cutoff is None:
        return False
    appeared = _parse(published, latest=True)
    return appeared is not None and appeared <= cutoff


def enforce_cutoff(
    candidates: Sequence[tuple[ID, str | None]],
    cutoff: date | None,
) -> list[ID]:
    """
    Keep only the candidates that existed at the cutoff, in the order given.

    It filters and does not reorder: ranking is someone else's decision, and
    quietly resorting here would rewrite the thing being measured.

    A `cutoff` of None admits nothing, so a case with no usable seed date
    cannot evaluate with the guard switched off.
    """
    return [paper_id for paper_id, published in candidates if visible_at(published, cutoff)]


__all__ = ["cutoff_from_seeds", "enforce_cutoff", "visible_at"]
