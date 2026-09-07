# TDD evidence — R0.2 Config loader

**Source plan:** `docs/BUILD.md` §R0.2, config values verbatim from Appendix A.
**Git checkpoints:** repo is under git as of R0.1 but has no commits or remote yet;
evidence is this file.

## User journey

> As the sole developer, I want every tunable number to live in a typed,
> version-stamped YAML file, so that I can change ranking behaviour without
> touching code and can still reproduce last week's results afterwards.

## Cycle

| Stage | Command | Result |
|---|---|---|
| RED | `uv run pytest backend/tests/unit/test_config.py -q` | **1 error during collection** — `ModuleNotFoundError: No module named 'app.config'` |
| GREEN | `uv run pytest backend/tests/unit/test_config.py -q` | **31 passed** |
| Full suite | `uv run pytest -q` | **98 passed** |
| BUILD.md verify | `uv run python -c "from app.config import settings, filters; print(settings.db_path, filters.config_version)"` | `data/app.db 620fa7f96045` |
| Lint / format / typecheck | `ruff check`, `ruff format --check`, `mypy` | clean |

The RED failure was compile-time and for the intended reason: bare `import app`
had already been proven to resolve, so the missing symbol was the module R0.2
creates, not the path setup.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | Settings defaults match the four keys `.env.example` documents | `test_settings_defaults_match_env_example` | unit | PASS |
| 2 | `SESSION_ID` defaults to 1 per the session_id contract | `test_session_id_defaults_to_one` | unit | PASS |
| 3 | Env vars override defaults | `test_settings_reads_overrides_from_environment` | unit | PASS |
| 4 | The S2 key never appears in `repr()` or `str()` | `test_api_key_is_not_exposed_by_repr` | unit | PASS |
| 5 | A non-positive rate limit is rejected | `test_rate_limit_must_be_positive` | unit | PASS |
| 6 | `year_floor` is 2015 | `test_year_floor_is_2015` | unit | PASS |
| 7 | `CORE_ALLOW` / `BORDERLINE` are exactly Appendix A | `test_core_allow_*`, `test_borderline_*` | unit | PASS |
| 8 | `cs.CV` and the wildcard families are denied | `test_applied_deny_includes_cs_cv_and_wildcards` | unit | PASS |
| 9 | All five paper types have a policy, with the right actions | `test_paper_type_policy_*`, `test_research_*`, `test_survey_*`, `test_dataset_*` | unit | PASS |
| 10 | Applied keywords quarantine, never auto-reject | `test_applied_keywords_quarantine_only` | unit | PASS |
| 11 | Expansion ceilings are 2000 / 200 / 2000 / 3 | `test_expansion_ceilings` | unit | PASS |
| 12 | `overlap` is 2.0 and dominates every other active weight | `test_overlap_is_the_dominant_r1_weight` | unit | PASS |
| 13 | R3/R5 weights are inert (0.0) at R1 | `test_r3_and_r5_weights_are_zero_at_r1` | unit | PASS |
| 14 | Budget fractions are the Appendix A values and lie in [0,1] | `test_budget_fractions*` | unit | PASS |
| 15 | `config_version` is the sha256 prefix of the file's bytes | `test_config_version_is_sha256_of_file_contents` | unit | PASS |
| 16 | Same bytes → same version; changed weight → different version | `test_config_version_is_stable_*`, `test_config_version_changes_*` | unit | PASS |
| 17 | filters and ranking version independently | `test_filters_and_ranking_have_independent_versions` | unit | PASS |
| 18 | A typo'd YAML key raises instead of silently defaulting | `test_unknown_key_*`, `test_unknown_weight_*` | unit | PASS |
| 19 | A missing config file raises `FileNotFoundError` | `test_missing_config_file_raises_a_clear_error` | unit | PASS |

Guarantees 4, 5, 18 and 19 are not in BUILD.md. 18 is the important one: with
pydantic's default `extra="ignore"`, `year_flooor: 2016` would parse cleanly,
leave `year_floor` at its default, and silently change what the corpus contains.
`extra="forbid"` turns that into an import-time crash.

## Decisions worth recording

- **`config_version` is 12 hex chars of sha256 over raw file bytes**, not over
  the parsed mapping. A comment-only edit therefore produces a new version. That
  is the cheap direction to be wrong in: a spurious version costs one eval row,
  a missed one silently merges two different configurations in the results.
- **`s2_api_key` is `SecretStr`.** structlog arrives at R0.12 and settings
  objects end up in log events; `SecretStr` makes that leak impossible rather
  than merely discouraged.
- **`applied_keyword_action` is a model field with no YAML counterpart**, so the
  Appendix A comment becomes an assertable invariant while `filters.yaml`
  stays verbatim.

## Coverage and known gaps

- `mypy` is scoped to `backend/app/services` and `backend/app/models` per R0.1,
  so `config.py` is not covered by `make typecheck`. Checked separately with
  `mypy backend/app/config.py --strict` → clean, after adding `types-PyYAML`.
- **`config/venues.yaml`** appears in PLAN.md §K but has no Appendix A content
  and is not named by R0.2. Not created. `CORE_VENUES` currently lives inside
  `filters.yaml`, which is where Appendix A puts it.
- **`.env` carries five prototype leftovers** (`ARXIV_API_URL`, `TIME_QUERY`,
  `CAT_AI`, `CAT_MA`, `CAT_ML`) that `.env.example` does not document. `Settings`
  ignores them. They can be deleted once nothing references the archive.
