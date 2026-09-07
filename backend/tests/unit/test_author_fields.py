"""
The author-name field bug, found while building R1.15's search byline.

S2's field selection does not compose the way it reads. Naming any author
sub-field REPLACES the default author projection rather than extending it:

    fields=...,authors,authors.hIndex        [{"authorId": "40348417",
                                               "hIndex": 26}]
    fields=...,authors.name,authors.hIndex   [{"authorId": "40348417",
                                               "name": "Ashish Vaswani",
                                               "hIndex": 26}]

Both verified live against `/paper/search`. `authors` alone is fine -- that is
why `NEIGHBOR_FIELDS` was never affected -- but the moment `authors.hIndex`
was added for C4's h-index signal, every name silently disappeared.

Nothing raised. `to_paper` requires both an id and a name, so it dropped every
author, `Paper.authors` came back empty from every metadata fetch, and:

  * `authors` and `paper_authors` stayed at zero rows in the live database
  * dedup's canonical key fell back to its no-author variant on every
    comparison, which is the weaker key
  * R1.19's node inspector and R1.22's search list would have shown no byline

This is the third instance of the same class of bug in this project -- silent
degradation that leaves a working system quietly worse than designed -- and
like the other two it was caught by looking at real output, not by a unit test
asserting what we already believed. These tests exist so it cannot come back.
"""

from __future__ import annotations

from app.clients.s2 import NEIGHBOR_FIELDS, SEARCH_FIELDS, S2Paper, to_paper, to_stub

# --------------------------------------------------------------------------
# The field strings themselves
# --------------------------------------------------------------------------


def test_search_fields_requests_author_names_explicitly() -> None:
    """The whole bug in one assertion."""
    assert "authors.name" in SEARCH_FIELDS


def test_search_fields_still_requests_the_h_index() -> None:
    """PLAN.md C4 demotes the h-index but does not remove it."""
    assert "authors.hIndex" in SEARCH_FIELDS


def test_neighbour_fields_never_names_an_author_subfield() -> None:
    """
    `/references` and `/citations` reject `authors.hIndex` with a 400, and bare
    `authors` already returns names there. Adding a sub-field would break the
    call and gain nothing.
    """
    assert "authors" in NEIGHBOR_FIELDS
    assert "authors." not in NEIGHBOR_FIELDS


# --------------------------------------------------------------------------
# What the parsers do with the response
# --------------------------------------------------------------------------


def _raw(authors: list[dict[str, object]]) -> S2Paper:
    return S2Paper.model_validate({"paperId": "p1", "title": "A Paper", "authors": authors})


def test_a_named_author_survives_to_paper() -> None:
    paper = to_paper(_raw([{"authorId": "a1", "name": "Ada Lovelace", "hIndex": 3}]))
    assert paper is not None
    assert paper.authors == (("a1", "Ada Lovelace"),)


def test_a_nameless_author_is_still_dropped_by_to_paper() -> None:
    """
    `paper_authors` joins on the author id and displays the name, so a row with
    no name is not storable. Dropping it is correct -- the bug was that the
    request never asked for the name, not that this filter is wrong.
    """
    paper = to_paper(_raw([{"authorId": "a1", "hIndex": 3}]))
    assert paper is not None
    assert paper.authors == ()


def test_to_stub_keeps_an_author_with_no_id() -> None:
    """
    A stub's byline is displayed, never joined, so an unidentified author is
    still worth showing. Filtering it would hand the user a byline that is
    silently missing people.
    """
    stub = to_stub(_raw([{"name": "Anon Ymous"}]))
    assert stub is not None
    assert stub.authors == ("Anon Ymous",)


def test_to_stub_preserves_byline_order() -> None:
    """First-author position carries meaning; sorting would destroy it."""
    stub = to_stub(_raw([{"name": "First A."}, {"name": "Second B."}, {"name": "Third C."}]))
    assert stub is not None
    assert stub.authors == ("First A.", "Second B.", "Third C.")


def test_no_authors_is_an_empty_tuple_not_an_error() -> None:
    """CLAUDE.md rule 6: a missing field degrades a score, it never raises."""
    stub = to_stub(_raw([]))
    paper = to_paper(_raw([]))
    assert stub is not None and stub.authors == ()
    assert paper is not None and paper.authors == ()
