"""
R0.2 -- config loader.

Two things are being pinned here. First, that `config/filters.yaml` and
`config/ranking.yaml` on disk still say what BUILD.md Appendix A says they
say -- these are the numbers the filter cascade and the ranker are built
against, and a silent edit to one of them changes recommendations without
changing any code. Second, that `config_version` is a pure function of file
content, because it gets stamped into every `filter_decisions` and
`expansions` row (PLAN.md section C) and is the only thing making yesterday's
run reproducible.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import (
    FiltersConfig,
    RankingConfig,
    Settings,
    filters,
    load_filters,
    load_ranking,
    ranking,
    settings,
)

ROOT = Path(__file__).resolve().parents[3]
FILTERS_YAML = ROOT / "config" / "filters.yaml"
RANKING_YAML = ROOT / "config" / "ranking.yaml"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_settings_defaults_match_env_example() -> None:
    """The four keys .env.example documents are the four Settings exposes."""
    assert settings.db_path == "data/app.db"
    assert settings.s2_rate_limit == 1.0
    assert settings.log_level == "INFO"


def test_session_id_defaults_to_one() -> None:
    """BUILD.md session_id contract: R0/R1 hardcode SESSION_ID = 1."""
    assert settings.session_id == 1


def test_settings_reads_overrides_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", "data/other.db")
    monkeypatch.setenv("S2_RATE_LIMIT", "0.5")
    monkeypatch.setenv("SESSION_ID", "7")
    fresh = Settings()
    assert fresh.db_path == "data/other.db"
    assert fresh.s2_rate_limit == 0.5
    assert fresh.session_id == 7


def test_api_key_is_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """structlog arrives at R0.12; repr(settings) must not leak the key into logs."""
    monkeypatch.setenv("S2_API_KEY", "sk-do-not-log-me")
    fresh = Settings()
    assert "sk-do-not-log-me" not in repr(fresh)
    assert "sk-do-not-log-me" not in str(fresh)
    assert fresh.s2_api_key is not None
    assert fresh.s2_api_key.get_secret_value() == "sk-do-not-log-me"


def test_rate_limit_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S2_RATE_LIMIT", "0")
    with pytest.raises(ValidationError):
        Settings()


# --------------------------------------------------------------------------
# filters.yaml -- values are verbatim BUILD.md Appendix A
# --------------------------------------------------------------------------


def test_year_floor_is_2015() -> None:
    assert filters.year_floor == 2015


def test_core_allow_is_exactly_appendix_a() -> None:
    assert filters.core_allow == ["cs.LG", "cs.AI", "cs.CL", "cs.NE", "stat.ML", "cs.MA"]


def test_borderline_is_exactly_appendix_a() -> None:
    assert filters.borderline == ["cs.IR", "cs.CY", "cs.DS", "math.OC"]


def test_applied_deny_includes_cs_cv_and_wildcards() -> None:
    """cs.CV denied is a locked decision (BUILD.md Locked decisions)."""
    assert "cs.CV" in filters.applied_deny
    assert "cs.RO" in filters.applied_deny
    assert "q-bio.*" in filters.applied_deny
    assert "eess.*" in filters.applied_deny


def test_core_venues_cover_the_main_ml_conferences() -> None:
    for venue in ("NeurIPS", "ICML", "ICLR", "ACL", "EMNLP", "JMLR", "TMLR"):
        assert venue in filters.core_venues


def test_paper_type_policy_covers_all_five_types() -> None:
    assert set(filters.paper_type_policy) == {
        "RESEARCH",
        "SURVEY",
        "BENCHMARK",
        "DATASET",
        "POSITION",
    }


def test_research_is_accepted_outright() -> None:
    assert filters.paper_type_policy["RESEARCH"].action == "accept"


def test_survey_is_conditional_on_citations() -> None:
    survey = filters.paper_type_policy["SURVEY"]
    assert survey.action == "accept_if"
    assert survey.citation_count_min == 200
    assert survey.or_citations_per_year_min == 40


def test_dataset_is_rejected_and_benchmark_quarantined() -> None:
    assert filters.paper_type_policy["DATASET"].action == "reject"
    assert filters.paper_type_policy["BENCHMARK"].action == "quarantine"
    assert filters.paper_type_policy["POSITION"].action == "quarantine"


def test_applied_keywords_quarantine_only() -> None:
    """Appendix A: keyword hits QUARANTINE only, never auto-reject."""
    assert "clinical" in filters.applied_keywords
    assert "crop" in filters.applied_keywords
    assert filters.applied_keyword_action == "quarantine"


def test_expansion_ceilings() -> None:
    assert filters.forward_expand_max == 2000
    assert filters.max_reference_fetch == 200
    assert filters.max_nodes == 2000
    assert filters.max_depth == 3


# --------------------------------------------------------------------------
# ranking.yaml
# --------------------------------------------------------------------------


def test_overlap_is_the_dominant_r1_weight() -> None:
    """Appendix A marks overlap 2.00 as the dominant R1 weight."""
    assert ranking.weights.overlap == 2.0
    assert ranking.weights.overlap > max(
        ranking.weights.quality, ranking.weights.recency, ranking.weights.hub
    )


def test_r3_and_r5_weights_are_zero_at_r1() -> None:
    for name in ("ppr", "cocite", "bibcoup", "venue", "author", "dislike"):
        assert getattr(ranking.weights, name) == 0.0, f"{name} should be inert until R3+"


def test_active_r1_weights() -> None:
    assert ranking.weights.quality == 0.40
    assert ranking.weights.recency == 0.30
    assert ranking.weights.hub == 0.60


def test_budget_fractions() -> None:
    assert ranking.budget.recency_lane_frac == 0.15
    assert ranking.budget.direction_floor_frac == 0.20
    assert ranking.budget.source_cap_frac == 0.40
    assert ranking.budget.score_floor == 0.0


@pytest.mark.parametrize("frac", ["recency_lane_frac", "direction_floor_frac", "source_cap_frac"])
def test_budget_fractions_are_in_unit_range(frac: str) -> None:
    assert 0.0 <= getattr(ranking.budget, frac) <= 1.0


# --------------------------------------------------------------------------
# config_version -- stamped into every filter_decisions and expansions row
# --------------------------------------------------------------------------


def test_config_version_is_sha256_of_file_contents() -> None:
    expected = hashlib.sha256(FILTERS_YAML.read_bytes()).hexdigest()
    assert expected.startswith(filters.config_version)
    assert len(filters.config_version) == 12
    assert filters.config_version.islower()


def test_filters_and_ranking_have_independent_versions() -> None:
    assert filters.config_version != ranking.config_version


def test_config_version_is_stable_across_loads(tmp_path: Path) -> None:
    dst = tmp_path / "filters.yaml"
    dst.write_bytes(FILTERS_YAML.read_bytes())
    assert load_filters(dst).config_version == filters.config_version


def test_config_version_changes_when_a_weight_changes(tmp_path: Path) -> None:
    data = yaml.safe_load(RANKING_YAML.read_text(encoding="utf-8"))
    data["weights"]["overlap"] = 1.5
    dst = tmp_path / "ranking.yaml"
    dst.write_text(yaml.safe_dump(data), encoding="utf-8")
    changed = load_ranking(dst)
    assert changed.config_version != ranking.config_version
    assert changed.weights.overlap == 1.5


# --------------------------------------------------------------------------
# Strictness -- a typo in YAML must fail loudly, not silently default
# --------------------------------------------------------------------------


def test_unknown_key_in_filters_yaml_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(FILTERS_YAML.read_text(encoding="utf-8"))
    data["year_flooor"] = 2016
    dst = tmp_path / "filters.yaml"
    dst.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_filters(dst)


def test_unknown_weight_in_ranking_yaml_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(RANKING_YAML.read_text(encoding="utf-8"))
    data["weights"]["overlapp"] = 2.0
    dst = tmp_path / "ranking.yaml"
    dst.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_ranking(dst)


def test_missing_config_file_raises_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_filters(tmp_path / "nope.yaml")


def test_loaded_types_are_the_declared_models() -> None:
    assert isinstance(filters, FiltersConfig)
    assert isinstance(ranking, RankingConfig)
