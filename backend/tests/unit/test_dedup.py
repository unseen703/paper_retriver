"""
R1.3 -- services/dedup.py. Pure functions: no I/O, no DB (CLAUDE.md rule 3).

BUILD.md is explicit about where the risk lies: "test the false-positive
direction hardest". A missed duplicate costs one redundant node. A *wrong* merge
silently attributes one paper's citations to another, and there is no later
stage that can detect it -- the graph just quietly becomes wrong.

So the headline case is negative:

    "Attention Is All You Need"  vs  "Attention Is Not All You Need"

must NOT collide. Titles in this field differ by single words on purpose, and
any normalization aggressive enough to merge those is too aggressive.

The positive cases that must work: arXiv v1/v3 collapse to one paper, case and
punctuation variants collide, and a preprint/conference pair merges.
"""

from __future__ import annotations

import pytest

from app.models import Paper
from app.services.dedup import canonical_key, first_author_surname, normalize_title


def _paper(**over: object) -> Paper:
    base: dict[str, object] = {
        "s2_paper_id": "s1",
        "title": "Attention Is All You Need",
        "first_seen_at": "2026-01-01",
    }
    base.update(over)
    return Paper(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The false-positive direction, tested hardest
# --------------------------------------------------------------------------


def test_attention_is_all_you_need_does_not_collide_with_its_negation() -> None:
    """BUILD.md's named case. Both are real papers."""
    a = _paper(title="Attention Is All You Need", year=2017)
    b = _paper(title="Attention Is Not All You Need", year=2021)
    assert canonical_key(a) != canonical_key(b)


def test_a_single_inserted_word_changes_the_key() -> None:
    a = _paper(title="Deep Residual Learning for Image Recognition")
    b = _paper(title="Deep Residual Learning for Better Image Recognition")
    assert canonical_key(a) != canonical_key(b)


def test_the_same_title_by_different_authors_does_not_collide() -> None:
    """ "Deep Learning" is a title several unrelated papers share."""
    a = _paper(title="Deep Learning", year=2015, authors=(("a1", "Yann LeCun"),))
    b = _paper(title="Deep Learning", year=2015, authors=(("a2", "Geoffrey Hinton"),))
    assert canonical_key(a) != canonical_key(b)


def test_the_same_title_in_different_years_does_not_collide() -> None:
    a = _paper(title="A Survey of Reinforcement Learning", year=2018)
    b = _paper(title="A Survey of Reinforcement Learning", year=2024)
    assert canonical_key(a) != canonical_key(b)


def test_different_dois_never_collide() -> None:
    a = _paper(doi="10.1109/CVPR.2016.90", title="Same Title")
    b = _paper(doi="10.5555/3295222", title="Same Title")
    assert canonical_key(a) != canonical_key(b)


# --------------------------------------------------------------------------
# Key precedence: doi > arxiv > title
# --------------------------------------------------------------------------


def test_doi_wins_when_present() -> None:
    key = canonical_key(_paper(doi="10.5555/3295222", arxiv_id="1706.03762"))
    assert key[0] == "doi"


def test_arxiv_wins_when_there_is_no_doi() -> None:
    assert canonical_key(_paper(arxiv_id="1706.03762"))[0] == "arxiv"


def test_title_is_the_last_resort() -> None:
    assert canonical_key(_paper())[0] == "title"


def test_doi_is_lowercased() -> None:
    """DOIs are case-insensitive by spec; S2 is inconsistent about case."""
    upper = canonical_key(_paper(doi="10.1109/CVPR.2016.90"))
    lower = canonical_key(_paper(doi="10.1109/cvpr.2016.90"))
    assert upper == lower
    assert upper[1] == "10.1109/cvpr.2016.90"


def test_an_empty_doi_falls_through_rather_than_keying_on_it() -> None:
    """S2 sometimes returns "" rather than omitting the field."""
    assert canonical_key(_paper(doi="", arxiv_id="1706.03762"))[0] == "arxiv"


# --------------------------------------------------------------------------
# arXiv version suffixes
# --------------------------------------------------------------------------


def test_arxiv_version_suffix_is_stripped() -> None:
    assert canonical_key(_paper(arxiv_id="1706.03762v3"))[1] == "1706.03762"


def test_v1_and_v3_are_the_same_paper() -> None:
    """The single most common duplicate S2 hands you."""
    assert canonical_key(_paper(arxiv_id="1706.03762v1")) == canonical_key(
        _paper(arxiv_id="1706.03762v3")
    )


def test_a_bare_arxiv_id_is_unchanged() -> None:
    assert canonical_key(_paper(arxiv_id="1706.03762"))[1] == "1706.03762"


def test_an_old_style_arxiv_id_survives_version_stripping() -> None:
    """Pre-2007 ids look like `cs/0701001` and contain no 'v'."""
    assert canonical_key(_paper(arxiv_id="cs/0701001v2"))[1] == "cs/0701001"


def test_version_stripping_does_not_eat_a_v_inside_the_id() -> None:
    """Splitting on 'v' naively would truncate ids that contain one."""
    assert canonical_key(_paper(arxiv_id="cs.CV/0701001"))[1] == "cs.CV/0701001"


# --------------------------------------------------------------------------
# normalize_title
# --------------------------------------------------------------------------


def test_case_and_punctuation_variants_collide() -> None:
    assert normalize_title("BERT: Pre-training of Deep Bidirectional Transformers") == (
        normalize_title("bert pretraining of deep bidirectional transformers")
    )


def test_whitespace_is_collapsed() -> None:
    assert normalize_title("  Deep   Residual\tLearning  ") == "deep residual learning"


def test_unicode_is_nfkd_normalized() -> None:
    """
    "Scholkopf" with a precomposed umlaut and with a combining diaeresis are
    the same string to a reader and different bytes to Python.
    """
    precomposed = "Schölkopf on Kernels"
    combining = "Schölkopf on Kernels"
    assert precomposed != combining
    assert normalize_title(precomposed) == normalize_title(combining)


def test_a_leading_article_is_stripped() -> None:
    assert normalize_title("The Transformer Architecture") == "transformer architecture"
    assert normalize_title("A Survey of Deep Learning") == "survey of deep learning"
    assert normalize_title("An Empirical Study") == "empirical study"


def test_articles_inside_the_title_are_kept() -> None:
    """Stripping every article would merge genuinely different titles."""
    assert normalize_title("Attention Is All You Need") == "attention is all you need"
    assert "a" in normalize_title("Learning a Deep Model").split()


def test_a_title_that_is_only_an_article_does_not_normalize_to_empty() -> None:
    assert normalize_title("The") == "the"


def test_normalization_is_idempotent() -> None:
    once = normalize_title("The  BERT: Pre-training!")
    assert normalize_title(once) == once


# --------------------------------------------------------------------------
# first_author_surname
# --------------------------------------------------------------------------


def test_first_author_surname_takes_position_zero() -> None:
    paper = _paper(authors=(("a1", "Ashish Vaswani"), ("a2", "Noam Shazeer")))
    assert first_author_surname(paper) == "vaswani"


def test_first_author_surname_is_none_without_authors() -> None:
    """Most papers arrive with no authors persisted; the key must tolerate it."""
    assert first_author_surname(_paper()) is None


def test_first_author_surname_handles_middle_initials() -> None:
    assert first_author_surname(_paper(authors=(("a1", "Geoffrey E. Hinton"),))) == "hinton"


def test_first_author_surname_of_a_blank_name_is_none() -> None:
    assert first_author_surname(_paper(authors=(("a1", "   "),))) is None


# --------------------------------------------------------------------------
# The positive case: a preprint and its conference version merge
# --------------------------------------------------------------------------


def test_a_preprint_and_its_conference_version_merge() -> None:
    """
    The pair that motivates dedup. Same work, two S2 records: the arXiv preprint
    and the published version, with punctuation and casing drift between them.
    """
    preprint = _paper(
        s2_paper_id="preprint",
        title="BERT: Pre-training of Deep Bidirectional Transformers",
        year=2018,
        authors=(("a1", "Jacob Devlin"),),
    )
    published = _paper(
        s2_paper_id="published",
        title="BERT — Pretraining of Deep Bidirectional Transformers",
        year=2018,
        authors=(("a1", "Jacob Devlin"),),
    )
    assert canonical_key(preprint) == canonical_key(published)


def test_a_shared_arxiv_id_merges_regardless_of_title_drift() -> None:
    a = _paper(arxiv_id="1810.04805v1", title="BERT: Pre-training of Deep Bidirectional")
    b = _paper(arxiv_id="1810.04805", title="Bert  pretraining of deep bidirectional!")
    assert canonical_key(a) == canonical_key(b)


# --------------------------------------------------------------------------
# Key shape -- repo/papers.find_by_canonical_key destructures these
# --------------------------------------------------------------------------


def test_a_title_key_has_four_elements_in_the_documented_order() -> None:
    paper = _paper(title="Deep Learning", year=2015, authors=(("a1", "Yann LeCun"),))
    assert canonical_key(paper) == ("title", "deep learning", "lecun", 2015)


@pytest.mark.parametrize(
    "paper_kwargs",
    [
        {"doi": "10.1/x"},
        {"arxiv_id": "1706.03762"},
        {},
    ],
)
def test_every_key_is_hashable(paper_kwargs: dict[str, object]) -> None:
    """Keys land in sets and dicts during the exclusion pass."""
    assert isinstance(hash(canonical_key(_paper(**paper_kwargs))), int)


def test_keys_are_stable_across_calls() -> None:
    paper = _paper(title="Deep Learning", year=2015, authors=(("a1", "Yann LeCun"),))
    assert canonical_key(paper) == canonical_key(paper)
