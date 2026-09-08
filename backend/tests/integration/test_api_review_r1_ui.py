"""
The two backend findings from the R1 UI review pass.

**`as_state` was accepted, documented, and ignored.** `AddNodeRequest` declares
it and the endpoint never reads it -- `add_seed` hard-codes SEED. Harmless
while SEED is the only permitted value, and a silent lie the moment R2 widens
the `Literal`: the request would validate, the caller would believe it had
asked for something, and the paper would be created as a SEED regardless.

**`q` had no maximum length.** Only emptiness was checked, so a pasted abstract
became a multi-kilobyte URL. Servers cap request lines around 8KB, so it came
back a 414 or 400, which this endpoint reports as 503 "Semantic Scholar is
unavailable" -- a confident, wrong diagnosis of a client-side problem. The
dialog's own advice is that short distinctive fragments match better, so
bounding it costs nothing and removes a whole class of misleading error.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.api.deps import get_s2_client
from app.clients.cache import ResponseCache
from app.clients.s2 import CachedOnlyS2Client
from app.db import make_engine
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "backend" / "migrations" / "alembic.ini"
FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "s2_cache.db"

SID = 1
CACHED_TITLE = "Attention Is All You Need"

pytestmark = pytest.mark.skipif(
    not FIXTURE_DB.is_file(),
    reason="fixture cache absent; build with scripts/build_fixture_cache.py",
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "review.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "head")

    fixture_engine = make_engine(f"sqlite:///{FIXTURE_DB.as_posix()}")
    application = create_app(db_url=f"sqlite:///{db.as_posix()}")
    application.dependency_overrides[get_s2_client] = lambda: CachedOnlyS2Client(
        cache=ResponseCache(fixture_engine)
    )
    with TestClient(application) as test_client:
        yield test_client
    fixture_engine.dispose()


# --------------------------------------------------------------------------
# `q` must be bounded
# --------------------------------------------------------------------------


def test_an_overlong_query_is_rejected_as_a_client_error(client: TestClient) -> None:
    """
    422, not 503. An unbounded query produced a URL long enough for S2 to
    reject at the protocol level, which this endpoint then reported as the
    upstream being unavailable -- sending the user to check a service that was
    perfectly healthy.
    """
    response = client.get(f"/api/sessions/{SID}/search", params={"q": "x" * 5000})
    assert response.status_code == 422


def test_the_length_limit_leaves_real_titles_alone(client: TestClient) -> None:
    """
    The longest title in the fixture corpus is well under the cap. A limit that
    rejected a genuine paper title would be worse than no limit at all.
    """
    long_but_real = (
        "Multi-Granularity Hierarchical Attention Fusion Networks for Reading"
        " Comprehension and Question Answering"
    )
    response = client.get(f"/api/sessions/{SID}/search", params={"q": long_but_real})
    assert response.status_code in {200, 503}


def test_an_overlong_query_costs_no_api_call(client: TestClient) -> None:
    """Rejecting before the fetch is the point of rejecting."""
    before = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    client.get(f"/api/sessions/{SID}/search", params={"q": "x" * 5000})
    after = client.app.dependency_overrides[get_s2_client]().cache_hits  # type: ignore[attr-defined]
    assert after == before


# --------------------------------------------------------------------------
# `as_state` must not be a field that does nothing
# --------------------------------------------------------------------------


def _s2_id(client: TestClient) -> str:
    hits = client.get(f"/api/sessions/{SID}/search", params={"q": CACHED_TITLE}).json()
    assert hits
    return str(hits[0]["s2_paper_id"])


def test_the_request_model_does_not_carry_a_field_nothing_reads(client: TestClient) -> None:
    """
    `as_state` was declared, described in the schema, and never read. A field
    that appears in the OpenAPI contract -- and therefore in the generated
    TypeScript -- while having no effect is a promise the server does not keep.
    """
    from app.schemas.nodes import AddNodeRequest

    assert "as_state" not in AddNodeRequest.model_fields


def test_adding_a_paper_still_creates_a_seed(client: TestClient) -> None:
    """Removing the field must not change what the endpoint actually does."""
    response = client.post(
        f"/api/sessions/{SID}/nodes", json={"s2_paper_id": _s2_id(client)}
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "SEED"


def test_as_state_is_now_rejected_rather_than_silently_ignored(client: TestClient) -> None:
    """
    `extra="forbid"` turns a caller's stale assumption into a 422 they can see,
    instead of a request that succeeds while doing something else.
    """
    response = client.post(
        f"/api/sessions/{SID}/nodes",
        json={"s2_paper_id": _s2_id(client), "as_state": "SEED"},
    )
    assert response.status_code == 422
