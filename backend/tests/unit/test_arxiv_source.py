"""
R0.10 -- arXiv metadata sources.

Two backends behind one iterator interface:

* **Kaggle snapshot** -- 5.5GB of JSONL, frozen. Reproducible, which is what
  R4's eval harness needs, but stale the moment it is published.
* **OAI-PMH** -- live, paginated, supports `from=`. Slow for a full harvest,
  fast for a delta.

The pair exists because the corpus here is 2022-2025 heavy. A snapshot alone
leaves the newest papers with `primary_arxiv_category = NULL`, and a NULL
category silently weakens the cs.CV denial precisely on the papers that matter
most -- CAT_PRIMARY_APPLIED never fires, and the paper falls through to the
weaker venue/keyword fallback with nothing appearing broken.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.clients.arxiv import (
    ArxivRecord,
    KaggleSnapshotSource,
    OaiPmhSource,
    parse_kaggle_line,
    parse_oai_page,
)


def _line(**over: object) -> str:
    base: dict[str, object] = {
        "id": "1706.03762",
        "title": "Attention Is All You Need",
        "categories": "cs.CL cs.LG",
        "update_date": "2017-12-06",
        "abstract": "...",
    }
    base.update(over)
    return json.dumps(base)


# --------------------------------------------------------------------------
# Kaggle record parsing
# --------------------------------------------------------------------------


def test_primary_category_is_the_first_listed() -> None:
    """arXiv's convention: the first entry in `categories` is primary."""
    rec = parse_kaggle_line(_line(categories="cs.CL cs.LG cs.AI"))
    assert rec is not None
    assert rec.primary_category == "cs.CL"
    assert rec.categories == ("cs.CL", "cs.LG", "cs.AI")


def test_a_single_category_still_parses() -> None:
    rec = parse_kaggle_line(_line(categories="hep-ph"))
    assert rec is not None
    assert rec.primary_category == "hep-ph"
    assert rec.categories == ("hep-ph",)


def test_cross_listed_cs_lg_keeps_cs_lg_primary() -> None:
    """
    Batch Normalization is cs.LG cross-listed cs.CV. It MUST survive the deny
    list -- denial keys off the primary, not off membership.
    """
    rec = parse_kaggle_line(_line(categories="cs.LG cs.CV"))
    assert rec is not None
    assert rec.primary_category == "cs.LG"
    assert "cs.CV" in rec.categories


def test_update_date_is_carried_through() -> None:
    rec = parse_kaggle_line(_line(update_date="2017-12-06"))
    assert rec is not None
    assert rec.updated == "2017-12-06"


def test_extra_whitespace_in_categories_is_tolerated() -> None:
    rec = parse_kaggle_line(_line(categories="  cs.CL   cs.LG  "))
    assert rec is not None
    assert rec.categories == ("cs.CL", "cs.LG")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not json at all",
        '{"id": "1234"}',  # no categories
        '{"categories": "cs.LG"}',  # no id
        '{"id": "", "categories": "cs.LG"}',
        '{"id": "1234", "categories": ""}',
    ],
)
def test_a_malformed_line_is_skipped_not_fatal(bad: str) -> None:
    """5.5M lines: one bad record must not end a two-hour load."""
    assert parse_kaggle_line(bad) is None


# --------------------------------------------------------------------------
# Kaggle source: reads .zip and plain .json alike
# --------------------------------------------------------------------------


@pytest.fixture
def snapshot_zip(tmp_path: Path) -> Path:
    path = tmp_path / "archive.zip"
    body = "\n".join(_line(id=f"170{i}.0000{i}", categories="cs.LG cs.CV") for i in range(5))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("arxiv-metadata-oai-snapshot.json", body)
    return path


def test_reads_jsonl_from_inside_a_zip(snapshot_zip: Path) -> None:
    """No 5.5GB extraction step: stream straight out of the archive."""
    records = list(KaggleSnapshotSource(snapshot_zip).records())
    assert len(records) == 5
    assert all(r.primary_category == "cs.LG" for r in records)


def test_reads_a_plain_jsonl_file(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(_line() + "\n" + _line(id="2005.14165"), encoding="utf-8")
    assert len(list(KaggleSnapshotSource(path).records())) == 2


def test_limit_stops_early(snapshot_zip: Path) -> None:
    """--limit N is what makes a smoke test cheap on a 5.5GB file."""
    assert len(list(KaggleSnapshotSource(snapshot_zip).records(limit=2))) == 2


def test_blank_and_broken_lines_do_not_stop_the_stream(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("\n".join([_line(id="a1"), "", "{broken", _line(id="a2")]), encoding="utf-8")
    assert [r.arxiv_id for r in KaggleSnapshotSource(path).records()] == ["a1", "a2"]


def test_a_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(KaggleSnapshotSource(tmp_path / "nope.zip").records())


def test_source_reports_the_newest_update_date_it_saw(tmp_path: Path) -> None:
    """This is the watermark the OAI delta starts from."""
    path = tmp_path / "snapshot.json"
    path.write_text(
        "\n".join(
            [
                _line(id="a1", update_date="2024-01-15"),
                _line(id="a2", update_date="2025-06-30"),
                _line(id="a3", update_date="2023-11-02"),
            ]
        ),
        encoding="utf-8",
    )
    source = KaggleSnapshotSource(path)
    list(source.records())
    assert source.max_updated == "2025-06-30"


# --------------------------------------------------------------------------
# OAI-PMH parsing
# --------------------------------------------------------------------------

OAI_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2501.00001</identifier>
              <datestamp>2025-01-05</datestamp></header>
      <metadata>
        <arXiv xmlns="http://arxiv.org/OAI/arXiv/">
          <id>2501.00001</id>
          <created>2025-01-02</created>
          <updated>2025-01-05</updated>
          <categories>cs.CL cs.AI</categories>
        </arXiv>
      </metadata>
    </record>
    <record>
      <header><identifier>oai:arXiv.org:2501.00002</identifier>
              <datestamp>2025-01-06</datestamp></header>
      <metadata>
        <arXiv xmlns="http://arxiv.org/OAI/arXiv/">
          <id>2501.00002</id>
          <created>2025-01-03</created>
          <categories>cs.LG</categories>
        </arXiv>
      </metadata>
    </record>
    <resumptionToken cursor="0" completeListSize="2">TOKEN-ABC</resumptionToken>
  </ListRecords>
</OAI-PMH>
"""

OAI_LAST_PAGE = OAI_PAGE.replace(
    '<resumptionToken cursor="0" completeListSize="2">TOKEN-ABC</resumptionToken>',
    '<resumptionToken cursor="1" completeListSize="2"></resumptionToken>',
)


def test_oai_page_yields_records_and_a_token() -> None:
    records, token = parse_oai_page(OAI_PAGE)
    assert [r.arxiv_id for r in records] == ["2501.00001", "2501.00002"]
    assert token == "TOKEN-ABC"


def test_oai_primary_category_is_the_first_listed() -> None:
    records, _ = parse_oai_page(OAI_PAGE)
    assert records[0].primary_category == "cs.CL"
    assert records[0].categories == ("cs.CL", "cs.AI")


def test_oai_falls_back_to_created_when_updated_is_absent() -> None:
    """arXiv omits <updated> on records that were never revised."""
    records, _ = parse_oai_page(OAI_PAGE)
    assert records[0].updated == "2025-01-05"
    assert records[1].updated == "2025-01-03"


def test_an_empty_resumption_token_means_the_harvest_is_done() -> None:
    """An empty element is the terminator; treating it as a token loops forever."""
    _, token = parse_oai_page(OAI_LAST_PAGE)
    assert token is None


def test_a_page_with_no_token_element_terminates() -> None:
    page = OAI_PAGE.replace(
        '<resumptionToken cursor="0" completeListSize="2">TOKEN-ABC</resumptionToken>', ""
    )
    _, token = parse_oai_page(page)
    assert token is None


def test_a_page_with_no_records_is_empty_not_an_error() -> None:
    records, token = parse_oai_page(
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords/></OAI-PMH>'
    )
    assert records == []
    assert token is None


def test_oai_source_defaults_to_the_cs_set() -> None:
    """Harvesting all of arXiv when only cs.* is wanted costs hours for nothing."""
    source = OaiPmhSource(since="2025-01-01")
    assert source.params()["set"] == "cs"
    assert source.params()["from"] == "2025-01-01"
    assert source.params()["metadataPrefix"] == "arXiv"


def test_oai_resumption_requests_send_only_the_token() -> None:
    """OAI-PMH forbids repeating from/set alongside a resumptionToken."""
    source = OaiPmhSource(since="2025-01-01")
    resumed = source.params(token="TOKEN-ABC")
    assert resumed == {"verb": "ListRecords", "resumptionToken": "TOKEN-ABC"}


# --------------------------------------------------------------------------
# The shared record type
# --------------------------------------------------------------------------


def test_record_is_frozen() -> None:
    rec = ArxivRecord(arxiv_id="1", primary_category="cs.LG", categories=("cs.LG",))
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        rec.arxiv_id = "2"  # type: ignore[misc]


def test_both_sources_yield_the_same_type(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(_line(), encoding="utf-8")
    (kaggle,) = list(KaggleSnapshotSource(path).records())
    oai, _ = parse_oai_page(OAI_PAGE)
    assert isinstance(kaggle, ArxivRecord)
    assert isinstance(oai[0], ArxivRecord)
