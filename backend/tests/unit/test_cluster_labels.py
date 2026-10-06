from app.services.cluster_labels import label_communities

TITLES = {
    0: [
        "Contrastive pre-training of image encoders",
        "Contrastive language-image pretraining",
        "Scaling image encoders",
    ],
    1: [
        "Sparse attention for long documents",
        "Efficient sparse attention kernels",
    ],
}


def test_distinctive_terms_label_each_cluster() -> None:
    got = label_communities(TITLES, n_terms=2)
    assert got[0].split()[0] in {"contrastive", "image", "encoders"}
    assert got[1].split()[0] in {"sparse", "attention"}
    assert set(got[0].split()).isdisjoint(got[1].split())


def test_stopwords_never_label() -> None:
    got = label_communities({0: ["Learning with neural networks", "Deep learning methods"]})
    assert got[0] == ""


def test_term_shared_by_every_cluster_ranks_below_a_distinctive_one() -> None:
    got = label_communities(
        {0: ["transformer alpha", "transformer alpha"], 1: ["transformer beta"]}, n_terms=1
    )
    assert got == {0: "alpha", 1: "beta"}


def test_ties_break_alphabetically_and_ignore_input_order() -> None:
    a = label_communities({0: ["zeta alpha mud"]})
    b = label_communities({0: ["mud zeta alpha"]})
    assert a == b == {0: "alpha mud zeta"}


def test_missing_titles_and_empty_communities_degrade() -> None:
    got = label_communities({0: [None, ""], 1: []})
    assert got == {0: "", 1: ""}


def test_single_community_still_labels() -> None:
    assert label_communities({3: ["graph partitioning heuristics"]}, n_terms=1) == {3: "graph"}


def test_empty_input() -> None:
    assert label_communities({}) == {}
