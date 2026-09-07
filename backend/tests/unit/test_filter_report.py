"""
R1.7 CHECKPOINT -- `filter-report`, the first end-to-end look at the cascade.

The command fetches a paper's references, runs each through the full cascade,
and prints `title | year | primary_cat | outcome | reason_code`. Its value is
not the code: it is that a human reads the table and notices the cascade is
wrong in a way no unit test would flag, because unit tests assert what you
already believed.

Tested here are the formatting and aggregation -- the parts that can be wrong
silently. The fetch itself runs against the committed fixture cache, so nothing
in this file touches the network.
"""

from __future__ import annotations

from app.cli import ReportRow, format_report, summarize


def _row(**over: object) -> ReportRow:
    base: dict[str, object] = {
        "title": "Attention Is All You Need",
        "year": 2017,
        "primary_category": "cs.CL",
        "outcome": "ACCEPT",
        "reason_code": "CAT_PRIMARY_CORE",
    }
    base.update(over)
    return ReportRow(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def test_the_report_has_the_five_documented_columns() -> None:
    out = format_report([_row()])
    for header in ("title", "year", "primary_cat", "outcome", "reason_code"):
        assert header in out


def test_a_row_shows_its_verdict() -> None:
    out = format_report([_row(outcome="REJECT", reason_code="CAT_PRIMARY_APPLIED")])
    assert "REJECT" in out
    assert "CAT_PRIMARY_APPLIED" in out


def test_a_missing_year_renders_as_a_dash_not_none() -> None:
    """ "None" in a table reads as a value; a dash reads as absence."""
    out = format_report([_row(year=None)])
    assert "None" not in out
    assert "-" in out


def test_a_missing_category_renders_as_a_dash() -> None:
    out = format_report([_row(primary_category=None)])
    assert "None" not in out


def test_long_titles_are_truncated_so_columns_stay_aligned() -> None:
    long_title = "A " + ("Very " * 40) + "Long Title"
    lines = format_report([_row(title=long_title)]).splitlines()
    assert max(len(line) for line in lines) < 140


def test_an_empty_report_does_not_crash() -> None:
    assert format_report([]) is not None


def test_rows_keep_the_order_they_were_given() -> None:
    """
    Deliberately not re-sorted. The reference order is the order S2 returned,
    and preserving it makes the table comparable across runs.
    """
    rows = [_row(title=f"Paper {i}") for i in range(5)]
    out = format_report(rows)
    positions = [out.index(f"Paper {i}") for i in range(5)]
    assert positions == sorted(positions)


# --------------------------------------------------------------------------
# Aggregation -- the line you actually read
# --------------------------------------------------------------------------


def test_the_summary_counts_each_reason_code() -> None:
    rows = [
        _row(outcome="ACCEPT", reason_code="CAT_PRIMARY_CORE"),
        _row(outcome="ACCEPT", reason_code="CAT_PRIMARY_CORE"),
        _row(outcome="REJECT", reason_code="PRE_ERA"),
    ]
    counts = summarize(rows)
    assert counts[("ACCEPT", "CAT_PRIMARY_CORE")] == 2
    assert counts[("REJECT", "PRE_ERA")] == 1


def test_the_summary_is_ordered_most_common_first() -> None:
    """The dominant rejection reason is the thing worth noticing."""
    rows = [_row(outcome="REJECT", reason_code="PRE_ERA") for _ in range(5)]
    rows += [_row(outcome="ACCEPT", reason_code="CAT_PRIMARY_CORE")]
    assert next(iter(summarize(rows))) == ("REJECT", "PRE_ERA")


def test_summarizing_nothing_is_empty_not_an_error() -> None:
    assert summarize([]) == {}


def test_every_row_lands_in_exactly_one_bucket() -> None:
    rows = [_row(reason_code=f"CODE_{i % 3}") for i in range(30)]
    assert sum(summarize(rows).values()) == 30
