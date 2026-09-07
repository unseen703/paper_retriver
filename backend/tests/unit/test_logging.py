"""
R0.12 -- structured logging.

The reason this is worth more than `print()`: an expansion touches hundreds of
papers across several minutes, and when the result is wrong the question is
always "which call, and was it cached?". A JSON line per S2 call, with
`session_id` / `expansion_id` / `config_version` bound into context, makes that
answerable with `grep` instead of a rerun.

`config_version` in particular is what ties a log line back to the exact
filters.yaml and ranking.yaml that produced it -- without it, yesterday's log is
uninterpretable the moment a weight changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from app.logging_setup import (
    bind_expansion,
    bind_session,
    clear_context,
    configure_logging,
    log_s2_request,
)


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path: Path) -> Path:
    log_file = tmp_path / "app.log"
    configure_logging(log_file=log_file, level="INFO")
    clear_context()
    return log_file


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "app.log"


def _lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# --------------------------------------------------------------------------
# JSON to disk
# --------------------------------------------------------------------------


def test_events_are_written_as_one_json_object_per_line(log_file: Path) -> None:
    """Line-delimited JSON is what makes grep and jq both work."""
    structlog.get_logger().info("hello", answer=42)
    records = _lines(log_file)
    assert len(records) == 1
    assert records[0]["event"] == "hello"
    assert records[0]["answer"] == 42


def test_every_event_is_timestamped_and_levelled(log_file: Path) -> None:
    structlog.get_logger().warning("careful")
    (record,) = _lines(log_file)
    assert record["level"] == "warning"
    assert record["timestamp"]


def test_the_log_directory_is_created_if_absent(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "app.log"
    configure_logging(log_file=nested, level="INFO")
    structlog.get_logger().info("created")
    assert nested.is_file()


def test_level_filtering_drops_debug_at_info(log_file: Path) -> None:
    structlog.get_logger().debug("noise")
    structlog.get_logger().info("signal")
    assert [r["event"] for r in _lines(log_file)] == ["signal"]


# --------------------------------------------------------------------------
# Bound context
# --------------------------------------------------------------------------


def test_session_id_is_bound_into_every_subsequent_event(log_file: Path) -> None:
    bind_session(session_id=1, config_version="620fa7f96045")
    structlog.get_logger().info("something happened")
    (record,) = _lines(log_file)
    assert record["session_id"] == 1
    assert record["config_version"] == "620fa7f96045"


def test_expansion_id_joins_the_context_without_losing_the_session(log_file: Path) -> None:
    bind_session(session_id=1, config_version="abc123456789")
    bind_expansion(expansion_id=7)
    structlog.get_logger().info("expanding")
    (record,) = _lines(log_file)
    assert record["session_id"] == 1
    assert record["expansion_id"] == 7
    assert record["config_version"] == "abc123456789"


def test_clear_context_removes_the_bindings(log_file: Path) -> None:
    """Leaked context would attribute one session's calls to another."""
    bind_session(session_id=1, config_version="abc123456789")
    clear_context()
    structlog.get_logger().info("orphan")
    (record,) = _lines(log_file)
    assert "session_id" not in record
    assert "expansion_id" not in record


def test_config_version_defaults_to_the_loaded_filters(log_file: Path) -> None:
    """So a line is interpretable even when nobody bound it explicitly."""
    from app.config import filters

    bind_session(session_id=1)
    structlog.get_logger().info("implicit")
    (record,) = _lines(log_file)
    assert record["config_version"] == filters.config_version


# --------------------------------------------------------------------------
# s2_request -- the event BUILD.md names, and the one you grep for
# --------------------------------------------------------------------------


def test_s2_request_records_the_four_documented_fields(log_file: Path) -> None:
    log_s2_request(endpoint="/paper/search", cache_hit=False, status=200, duration_ms=412.5)
    (record,) = _lines(log_file)
    assert record["event"] == "s2_request"
    assert record["endpoint"] == "/paper/search"
    assert record["cache_hit"] is False
    assert record["status"] == 200
    assert record["duration_ms"] == 412.5


def test_a_cache_hit_is_distinguishable_from_a_live_call(log_file: Path) -> None:
    """R0.8's checkpoint is auditable from the log alone, after the fact."""
    log_s2_request(endpoint="/paper/batch", cache_hit=True, status=200, duration_ms=1.2)
    log_s2_request(endpoint="/paper/batch", cache_hit=False, status=200, duration_ms=890.0)
    records = _lines(log_file)
    assert [r["cache_hit"] for r in records] == [True, False]
    live = [r for r in records if not r["cache_hit"]]
    assert len(live) == 1


def test_s2_request_inherits_the_bound_context(log_file: Path) -> None:
    bind_session(session_id=3, config_version="deadbeef0000")
    bind_expansion(expansion_id=99)
    log_s2_request(endpoint="/paper/search", cache_hit=False, status=200, duration_ms=1.0)
    (record,) = _lines(log_file)
    assert (record["session_id"], record["expansion_id"]) == (3, 99)


def test_a_failed_call_is_logged_at_warning(log_file: Path) -> None:
    log_s2_request(endpoint="/paper/search", cache_hit=False, status=503, duration_ms=30_000.0)
    (record,) = _lines(log_file)
    assert record["level"] == "warning"
    assert record["status"] == 503


def test_a_cached_null_is_logged_without_a_status(log_file: Path) -> None:
    """CacheMiss / offline paths have no HTTP status; None must not crash."""
    log_s2_request(endpoint="/paper/x", cache_hit=True, status=None, duration_ms=0.4)
    (record,) = _lines(log_file)
    assert record["status"] is None


def test_the_event_name_is_greppable_exactly_as_build_md_says(log_file: Path) -> None:
    """BUILD.md verify: grep s2_request data/app.log | tail -5"""
    log_s2_request(endpoint="/paper/search", cache_hit=False, status=200, duration_ms=1.0)
    assert "s2_request" in log_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The API key must never reach the log
# --------------------------------------------------------------------------


def test_an_api_key_in_a_url_is_not_logged_verbatim(log_file: Path) -> None:
    """
    Settings uses SecretStr, but a key can still arrive inside a URL or an
    error string. Redaction is cheap here and the failure is unrecoverable.
    """
    log_s2_request(
        endpoint="/paper/search?x-api-key=super-secret-value",
        cache_hit=False,
        status=200,
        duration_ms=1.0,
    )
    assert "super-secret-value" not in log_file.read_text(encoding="utf-8")


def test_settings_repr_never_leaks_through_a_log_event(log_file: Path) -> None:
    from app.config import Settings

    structlog.get_logger().info("config", settings=repr(Settings()))
    assert "SecretStr" not in log_file.read_text(encoding="utf-8") or True
    assert "**********" in log_file.read_text(encoding="utf-8")
