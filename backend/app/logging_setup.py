"""
structlog configuration: line-delimited JSON to `data/app.log`.

An expansion touches hundreds of papers over several minutes, and when the
result looks wrong the question is always the same -- *which call, and was it
cached?* One JSON object per line, with `session_id` / `expansion_id` /
`config_version` bound into context, makes that a `grep` instead of a rerun.

`config_version` matters most. It ties a line back to the exact `filters.yaml`
and `ranking.yaml` that produced it; without it, yesterday's log becomes
uninterpretable the moment a weight changes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import structlog

from app.config import REPO_ROOT, filters

DEFAULT_LOG_FILE = REPO_ROOT / "data" / "app.log"

# Settings holds the key in a SecretStr, but one can still arrive inside a URL
# or an upstream error string. Redaction is cheap; the failure is permanent.
_SECRET_PATTERNS = [
    re.compile(r"(x-api-key=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^&\s\"',}]+", re.IGNORECASE),
]


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in event_dict.items():
        if isinstance(value, str):
            for pattern in _SECRET_PATTERNS:
                value = pattern.sub(r"\1REDACTED", value)
            event_dict[key] = value
    return event_dict


def _add_config_version(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Stamp the filter config version on anything that already carries session
    context, so a line is interpretable even when nobody bound it explicitly.
    """
    if "session_id" in event_dict:
        event_dict.setdefault("config_version", filters.config_version)
    return event_dict


def configure_logging(log_file: Path | None = None, level: str = "INFO") -> None:
    path = Path(log_file) if log_file else DEFAULT_LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    # Replace rather than append: reconfiguring in a test would otherwise
    # duplicate every subsequent line into the previous run's file.
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_config_version,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def bind_session(session_id: int, config_version: str | None = None) -> None:
    structlog.contextvars.bind_contextvars(
        session_id=session_id,
        config_version=config_version or filters.config_version,
    )


def bind_expansion(expansion_id: int) -> None:
    structlog.contextvars.bind_contextvars(expansion_id=expansion_id)


def clear_context() -> None:
    """Leaked context would attribute one session's calls to another."""
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# The event BUILD.md names
# ---------------------------------------------------------------------------


def log_s2_request(
    endpoint: str,
    cache_hit: bool,
    status: int | None,
    duration_ms: float,
) -> None:
    """
    One line per S2 call. `grep s2_request data/app.log` is the audit trail for
    R0.8's cache checkpoint, after the fact and without instrumentation.
    """
    logger = structlog.get_logger()
    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "cache_hit": cache_hit,
        "status": status,
        "duration_ms": duration_ms,
    }
    if status is not None and status >= 400:
        logger.warning("s2_request", **payload)
    else:
        logger.info("s2_request", **payload)


__all__ = [
    "DEFAULT_LOG_FILE",
    "bind_expansion",
    "bind_session",
    "clear_context",
    "configure_logging",
    "log_s2_request",
]
