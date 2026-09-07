"""
arXiv category metadata, from either of two sources behind one interface.

`KaggleSnapshotSource` streams the Cornell JSONL dump (5.5GB uncompressed,
read straight out of the .zip so there is no extraction step). Frozen, so it is
reproducible -- which is what R4's eval harness needs -- but stale from the day
it is published.

`OaiPmhSource` harvests arXiv's OAI-PMH endpoint with `from=<date>`. Slow for a
full crawl, fast for a delta.

Both exist because the corpus here is 2022-2025 heavy, and a snapshot alone
leaves the newest papers with `primary_arxiv_category = NULL`. That is not a
cosmetic gap: CAT_PRIMARY_APPLIED is what rejects cs.CV, so a NULL category
means a recent cs.CV paper is never denied at that stage and falls through to
the weaker venue/keyword fallback -- with nothing appearing broken. Snapshot for
the bulk, OAI for the tail.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

OAI_ENDPOINT = "http://export.arxiv.org/oai2"
# arXiv asks for one request per three seconds from harvesters.
OAI_RATE = 1 / 3


@dataclass(frozen=True, slots=True)
class ArxivRecord:
    arxiv_id: str
    primary_category: str
    categories: tuple[str, ...]
    updated: str | None = None


def _split_categories(raw: str | None) -> tuple[str, ...]:
    """arXiv stores categories space-separated, primary first."""
    return tuple((raw or "").split())


# ---------------------------------------------------------------------------
# Kaggle snapshot
# ---------------------------------------------------------------------------


def parse_kaggle_line(line: str) -> ArxivRecord | None:
    """
    One JSONL line -> a record, or None if it is unusable.

    Never raises. At 2.7M lines, one malformed record must not end a load that
    has already been running for minutes.
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw: dict[str, Any] = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    arxiv_id = (raw.get("id") or "").strip()
    categories = _split_categories(raw.get("categories"))
    if not arxiv_id or not categories:
        return None

    return ArxivRecord(
        arxiv_id=arxiv_id,
        primary_category=categories[0],
        categories=categories,
        updated=raw.get("update_date"),
    )


class KaggleSnapshotSource:
    """Streams the Cornell dump. Accepts the .zip or the extracted .json."""

    def __init__(self, path: Path, member: str = "arxiv-metadata-oai-snapshot.json") -> None:
        self.path = Path(path)
        self.member = member
        self.skipped = 0
        # The newest update_date seen. This is the watermark the OAI delta
        # starts from, so the two sources meet without a gap or an overlap.
        self.max_updated: str | None = None

    def _lines(self) -> Iterator[str]:
        if not self.path.is_file():
            raise FileNotFoundError(f"arXiv snapshot not found: {self.path}")
        if self.path.suffix == ".zip":
            with zipfile.ZipFile(self.path) as archive:
                name = self.member if self.member in archive.namelist() else archive.namelist()[0]
                with archive.open(name) as handle:
                    for raw in handle:
                        yield raw.decode("utf-8", errors="replace")
        else:
            with self.path.open(encoding="utf-8", errors="replace") as handle:
                yield from handle

    def records(self, limit: int | None = None) -> Iterator[ArxivRecord]:
        emitted = 0
        for line in self._lines():
            if limit is not None and emitted >= limit:
                return
            record = parse_kaggle_line(line)
            if record is None:
                if line.strip():
                    self.skipped += 1
                continue
            if record.updated and (self.max_updated is None or record.updated > self.max_updated):
                self.max_updated = record.updated
            emitted += 1
            yield record


# ---------------------------------------------------------------------------
# OAI-PMH
# ---------------------------------------------------------------------------

_NS = re.compile(r"\{[^}]*\}")


def _tag(element: ElementTree.Element) -> str:
    return _NS.sub("", element.tag)


def _find(parent: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in parent.iter():
        if _tag(child) == name:
            return child
    return None


def parse_oai_page(xml: str) -> tuple[list[ArxivRecord], str | None]:
    """
    One ListRecords response -> (records, next token).

    Namespaces are stripped rather than declared: arXiv serves the OAI envelope
    and the arXiv metadata block in different namespaces, and matching on local
    names keeps this working if either is versioned.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        logger.warning("unparseable OAI page: %s", exc)
        return [], None

    records: list[ArxivRecord] = []
    for node in root.iter():
        if _tag(node) != "record":
            continue
        meta = _find(node, "arXiv")
        if meta is None:
            continue
        id_el = _find(meta, "id")
        cat_el = _find(meta, "categories")
        if id_el is None or not (id_el.text or "").strip():
            continue
        categories = _split_categories(cat_el.text if cat_el is not None else None)
        if not categories:
            continue
        updated_el = _find(meta, "updated")
        created_el = _find(meta, "created")
        # arXiv omits <updated> on records that were never revised.
        updated = None
        for candidate in (updated_el, created_el):
            if candidate is not None and (candidate.text or "").strip():
                updated = candidate.text.strip()
                break
        records.append(
            ArxivRecord(
                arxiv_id=id_el.text.strip(),
                primary_category=categories[0],
                categories=categories,
                updated=updated,
            )
        )

    token: str | None = None
    for node in root.iter():
        if _tag(node) == "resumptionToken":
            # An EMPTY element is the terminator. Treating it as a live token
            # makes the harvest loop forever on the last page.
            token = (node.text or "").strip() or None
            break

    return records, token


class OaiPmhSource:
    """
    Incremental harvest. Defaults to `set=cs`, because harvesting all of arXiv
    when only cs.* is wanted costs hours for nothing.
    """

    def __init__(self, since: str | None = None, oai_set: str = "cs") -> None:
        self.since = since
        self.set = oai_set

    def params(self, token: str | None = None) -> dict[str, str]:
        # OAI-PMH forbids repeating from/set/metadataPrefix alongside a
        # resumptionToken -- doing so is a badArgument error, not a warning.
        if token:
            return {"verb": "ListRecords", "resumptionToken": token}
        params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": self.set}
        if self.since:
            params["from"] = self.since
        return params


__all__ = [
    "OAI_ENDPOINT",
    "OAI_RATE",
    "ArxivRecord",
    "KaggleSnapshotSource",
    "OaiPmhSource",
    "parse_kaggle_line",
    "parse_oai_page",
]
