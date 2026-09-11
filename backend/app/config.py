"""
Typed access to `.env`, `config/filters.yaml` and `config/ranking.yaml`.

Everything tunable about the recommender lives in the two YAML files, never in
code. Each is loaded into a strict model -- `extra="forbid"`, so a typo in a key
name raises at import instead of silently falling back to a default and
changing the recommendations by 30% with no visible cause.

`config_version` is the first 12 hex characters of the sha256 of the file's
bytes. PLAN.md section C stamps it into every `filter_decisions` and
`expansions` row, which is what makes a past run reproducible and what lets the
eval harness A/B two weight sets. It is derived from the raw bytes rather than
the parsed data so that a comment change also produces a new version -- the
cheap direction to be wrong in.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

PolicyAction = Literal["accept", "accept_if", "quarantine", "reject"]


def _config_version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Process settings. Field names map case-insensitively onto env vars."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # .env still carries five keys from the archived prototype
        # (ARXIV_API_URL, TIME_QUERY, CAT_*). Ignore rather than reject them.
        extra="ignore",
    )

    # SecretStr, not str: repr() and str() render as '**********', so a
    # settings object caught in a structlog event (R0.12) cannot leak the key.
    s2_api_key: SecretStr | None = None
    db_path: str = "data/app.db"
    s2_rate_limit: float = Field(default=1.0, gt=0)
    log_level: str = "INFO"

    # BUILD.md session_id contract: threaded through everything from day one,
    # but the UI for switching sessions is deferred to R2.5.
    session_id: int = 1


# ---------------------------------------------------------------------------
# config/filters.yaml
# ---------------------------------------------------------------------------


class PaperTypePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: PolicyAction
    citation_count_min: int | None = None
    or_citations_per_year_min: int | None = None


class FiltersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    config_version: str
    year_floor: int

    paper_type_policy: dict[str, PaperTypePolicy]

    core_allow: list[str] = Field(alias="CORE_ALLOW")
    borderline: list[str] = Field(alias="BORDERLINE")
    applied_deny: list[str] = Field(alias="APPLIED_DENY")
    core_venues: list[str] = Field(alias="CORE_VENUES")

    applied_keywords: list[str]
    #: Terms of art for ML applied to chemical reactions. Matched against the
    #: title before the category rules, because these papers live under
    #: categories APPLIED_DENY refuses. See config/filters.yaml for why the
    #: list is deliberately narrow.
    reaction_ml_keywords: list[str] = Field(default_factory=list)
    #: Terms of art for ML applied to biochemistry -- metabolic routes, enzyme
    #: active sites, enzyme and protein function. The second corridor through
    #: STAGE 2, kept as its own list so that `BIOCHEM_ML` in the review drawer
    #: can show what it admits and either corridor can be narrowed alone.
    biochem_ml_keywords: list[str] = Field(default_factory=list)
    # Encodes the Appendix A comment "keyword hits QUARANTINE only, never
    # auto-reject" as something R1.5 can assert against, rather than a comment
    # a future edit can quietly contradict. Deliberately not a YAML key.
    applied_keyword_action: Literal["quarantine"] = "quarantine"

    forward_expand_max: int
    max_reference_fetch: int
    max_nodes: int
    max_depth: int


def load_filters(path: Path | None = None) -> FiltersConfig:
    path = path or CONFIG_DIR / "filters.yaml"
    return FiltersConfig.model_validate(
        {**_read_yaml(path), "config_version": _config_version(path)}
    )


# ---------------------------------------------------------------------------
# config/ranking.yaml
# ---------------------------------------------------------------------------


class Weights(BaseModel):
    """Zeros are inert lanes, not missing values. See Appendix A for the phase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ppr: float  # R5
    cocite: float  # R3
    bibcoup: float  # R3
    overlap: float  # R1, dominant
    quality: float
    recency: float
    venue: float  # R3
    author: float  # R3, cold-start only
    dislike: float  # R5, subtracted
    hub: float  # subtracted


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recency_lane_frac: float = Field(ge=0.0, le=1.0)
    direction_floor_frac: float = Field(ge=0.0, le=1.0)
    source_cap_frac: float = Field(ge=0.0, le=1.0)
    score_floor: float


class RankingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str
    weights: Weights
    budget: Budget


def load_ranking(path: Path | None = None) -> RankingConfig:
    path = path or CONFIG_DIR / "ranking.yaml"
    return RankingConfig.model_validate(
        {**_read_yaml(path), "config_version": _config_version(path)}
    )


# ---------------------------------------------------------------------------
# Module-level singletons. Import these; construct your own only in tests.
# ---------------------------------------------------------------------------

settings = Settings()
filters = load_filters()
ranking = load_ranking()

__all__ = [
    "Budget",
    "FiltersConfig",
    "PaperTypePolicy",
    "PolicyAction",
    "RankingConfig",
    "Settings",
    "Weights",
    "filters",
    "load_filters",
    "load_ranking",
    "ranking",
    "settings",
]
