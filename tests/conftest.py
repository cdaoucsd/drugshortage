import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.db import init_db, make_session_factory
from app.openfda import normalize_result

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads((FIXTURES / "shortages_page.json").read_text())


@pytest.fixture
def normalized_records(fixture_payload) -> list[dict]:
    return [normalize_result(r) for r in fixture_payload["results"]]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
