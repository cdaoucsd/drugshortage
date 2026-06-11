import httpx
import pytest

import app.api as api_module
from app import external
from app.external import (
    companies_match,
    core_drug_name,
    fetch_alternatives,
    fetch_recalls,
)


@pytest.fixture(autouse=True)
def fresh_cache():
    external.clear_cache()
    yield
    external.clear_cache()


def test_core_drug_name_strips_form_words():
    assert core_drug_name("Amoxicillin Oral Powder for Suspension") == "amoxicillin"
    assert core_drug_name("Cisplatin Injection") == "cisplatin"
    assert (
        core_drug_name("Lisdexamfetamine Dimesylate Capsules") == "lisdexamfetamine"
    )
    assert core_drug_name("Dextrose 5% Injection") == "dextrose"


def test_companies_match_normalizes_suffixes():
    assert companies_match("Acme Pharma Inc.", "ACME PHARMACEUTICALS LLC")
    assert companies_match("Beta Generics LLC", "Beta Generics")
    assert not companies_match("Acme Pharma Inc.", "Gamma Oncology Co.")
    assert not companies_match(None, "Acme")


def mock_client(payload, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


NDC_PAYLOAD = {
    "results": [
        {"labeler_name": "Acme Pharma Inc.", "dosage_form": "POWDER"},
        {"labeler_name": "Healthy Generics Corp", "dosage_form": "TABLET"},
        {"labeler_name": "Healthy Generics Corp", "dosage_form": "POWDER"},
        {"labeler_name": "Fresh Meds LLC", "dosage_form": "CAPSULE"},
    ]
}


def test_fetch_alternatives_excludes_shortage_companies():
    out = fetch_alternatives(
        "Amoxicillin Oral Powder for Suspension",
        ["ACME PHARMA"],
        client=mock_client(NDC_PAYLOAD),
    )
    assert out["available"] is True
    assert out["search_term"] == "amoxicillin"
    labelers = [a["labeler"] for a in out["alternatives"]]
    assert labelers == ["Healthy Generics Corp", "Fresh Meds LLC"]
    assert out["alternatives"][0]["products"] == 2
    assert out["alternatives"][0]["dosage_forms"] == ["POWDER", "TABLET"]


def test_fetch_alternatives_handles_no_matches():
    out = fetch_alternatives("Unobtainium", [], client=mock_client({}, status=404))
    assert out == {"available": True, "search_term": "unobtainium", "alternatives": []}


def test_fetch_alternatives_degrades_on_error():
    def handler(request):
        raise httpx.ConnectError("blocked")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = fetch_alternatives("Amoxicillin", ["x"], client=client)
    assert out["available"] is False


def test_fetch_recalls_formats_dates():
    payload = {
        "results": [
            {
                "recalling_firm": "Acme Pharma Inc.",
                "product_description": "Amoxicillin 250mg",
                "reason_for_recall": "Subpotent",
                "classification": "Class II",
                "status": "Ongoing",
                "recall_initiation_date": "20260102",
            }
        ]
    }
    out = fetch_recalls("Amoxicillin Oral Powder", client=mock_client(payload))
    assert out["available"] is True
    assert out["recalls"][0]["initiated"] == "2026-01-02"
    assert out["recalls"][0]["classification"] == "Class II"


def test_lookups_are_cached():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=NDC_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_alternatives("Amoxicillin", [], client=client)
    fetch_alternatives("Amoxicillin", [], client=client)
    assert calls["n"] == 1


def test_alternatives_endpoint(monkeypatch, normalized_records):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api import app, get_session
    from app.db import init_db
    from app.sync import sync_records

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        sync_records(session, normalized_records, emit_events=False)
        session.commit()

    def override():
        with factory() as s:
            yield s

    captured = {}

    def fake_alternatives(name, companies, client=None):
        captured["name"] = name
        captured["companies"] = sorted(companies)
        return {"available": True, "search_term": "amoxicillin", "alternatives": []}

    monkeypatch.setattr(api_module, "fetch_alternatives", fake_alternatives)
    app.dependency_overrides[get_session] = override
    try:
        client = TestClient(app)
        resp = client.get(
            "/api/drugs/Amoxicillin Oral Powder for Suspension/alternatives"
        )
        assert resp.status_code == 200
        # Both fixture manufacturers of this drug are Current -> both excluded.
        assert captured["companies"] == ["Acme Pharma Inc.", "Beta Generics LLC"]

        assert client.get("/api/drugs/Nope/alternatives").status_code == 404

        detail = client.get(
            "/api/drugs/Amoxicillin Oral Powder for Suspension"
        ).json()
        assert "ashp.org" in detail["ashp_link"]
        assert "amoxicillin" in detail["ashp_link"]
    finally:
        app.dependency_overrides.clear()
