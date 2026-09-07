"""The filter cascade. Import stages from here, not from the submodules."""

from app.services.filters.base import (
    FilterStage,
    accept,
    quarantine,
    reject,
    should_continue,
)
from app.services.filters.era_filter import era_filter
from app.services.filters.topic_filter import is_core_venue, topic_filter
from app.services.filters.type_filter import citations_per_year, type_filter

__all__ = [
    "FilterStage",
    "accept",
    "citations_per_year",
    "era_filter",
    "is_core_venue",
    "quarantine",
    "reject",
    "should_continue",
    "topic_filter",
    "type_filter",
]
