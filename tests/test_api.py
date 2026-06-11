import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
from app.api import app, get_session
from app.db import init_db
from app.sync import sync_records


@pytest.fixture
def client(normalized_records):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        sync_records(session, normalized_records, emit_events=False)
        # One change so the events timeline has content.
        changed = copy.deepcopy(normalized_records)
        changed[1]["status"] = "Resolved"
        sync_records(session, changed)
        session.commit()

    def override():
        with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_drugs_grouped(client):
    data = client.get("/api/drugs").json()
    assert data["total"] == 3  # 4 records, 2 share a generic name
    amox = next(
        d for d in data["results"] if d["generic_name"].startswith("Amoxicillin")
    )
    assert amox["company_count"] == 2
    assert amox["status"] == "Current"  # one record current, one resolved
    assert amox["first_posted"] == "2025-10-28"


def test_search_by_drug_name(client):
    data = client.get("/api/drugs", params={"q": "amox"}).json()
    assert data["total"] == 1


def test_search_by_brand_name(client):
    data = client.get("/api/drugs", params={"q": "vyvanse"}).json()
    assert data["total"] == 1
    assert data["results"][0]["generic_name"].startswith("Lisdexamfetamine")


def test_search_by_manufacturer(client):
    data = client.get("/api/drugs", params={"q": "gamma oncology"}).json()
    assert [d["generic_name"] for d in data["results"]] == ["Cisplatin Injection"]


def test_filter_by_status(client):
    resolved = client.get("/api/shortages", params={"status": "resolved"}).json()
    assert resolved["total"] == 2  # cisplatin + the amoxicillin record we flipped
    assert all(r["status"] == "Resolved" for r in resolved["results"])


def test_filter_by_posted_date_range(client):
    data = client.get(
        "/api/shortages",
        params={"posted_from": "2025-11-01", "posted_to": "2025-12-31"},
    ).json()
    assert data["total"] == 2
    names = {r["company_name"] for r in data["results"]}
    assert names == {"Beta Generics LLC", "Delta Therapeutics"}


def test_filter_by_resolved_date(client):
    data = client.get("/api/shortages", params={"resolved_from": "2025-01-01"}).json()
    assert data["total"] == 2
    assert all(r["resolved_date"] for r in data["results"])


def test_pagination(client):
    page1 = client.get("/api/shortages", params={"per_page": 2, "page": 1}).json()
    page2 = client.get("/api/shortages", params={"per_page": 2, "page": 2}).json()
    assert page1["total"] == 4
    assert len(page1["results"]) == 2 and len(page2["results"]) == 2
    ids1 = {r["id"] for r in page1["results"]}
    ids2 = {r["id"] for r in page2["results"]}
    assert ids1.isdisjoint(ids2)


def test_drug_detail_records_and_events(client):
    detail = client.get("/api/drugs/Amoxicillin Oral Powder for Suspension").json()
    assert len(detail["records"]) == 2
    assert detail["status"] == "Current"
    assert [e["event_type"] for e in detail["events"]] == ["resolved"]
    event = detail["events"][0]
    assert event["company_name"] == "Beta Generics LLC"
    assert event["diff"]["status"]["new"] == "Resolved"


def test_drug_detail_404(client):
    assert client.get("/api/drugs/Nonexistent Drug").status_code == 404


def test_stats(client):
    data = client.get("/api/stats").json()
    assert data["current_records"] == 2
    assert data["resolved_records"] == 2
    assert data["current_drugs"] == 2
    assert data["events_last_7_days"] == {"resolved": 1}


def test_static_frontend_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Drug Shortage Tracker" in resp.text
