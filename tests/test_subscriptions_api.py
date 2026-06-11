import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
from app.api import app, get_session
from app.db import init_db
from app.mailer import ConsoleMailer
from app.models import Subscription
from app.sync import sync_records


@pytest.fixture
def mailer(monkeypatch):
    mailer = ConsoleMailer()
    monkeypatch.setattr(api_module, "get_mailer", lambda: mailer)
    return mailer


@pytest.fixture
def setup(normalized_records, mailer):
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

    app.dependency_overrides[get_session] = override
    yield TestClient(app), factory
    app.dependency_overrides.clear()


def test_create_subscription_sends_verification(setup, mailer):
    client, factory = setup
    resp = client.post(
        "/api/subscriptions",
        json={"email": "User@Example.com", "match_type": "drug_name",
              "match_value": "amoxicillin", "notify_on": ["new_shortage", "resolved"]},
    )
    assert resp.status_code == 201
    (msg,) = mailer.sent
    assert msg.to == "user@example.com"  # normalized to lowercase
    with factory() as s:
        sub = s.execute(select(Subscription)).scalar_one()
    assert sub.verified is False
    assert sub.notify_on == "new_shortage,resolved"


def test_create_subscription_validation(setup):
    client, _ = setup
    assert client.post(
        "/api/subscriptions", json={"email": "not-an-email", "match_type": "all"}
    ).status_code == 422
    assert client.post(
        "/api/subscriptions",
        json={"email": "a@b.co", "match_type": "bogus", "match_value": "x"},
    ).status_code == 422
    # match_value required unless match_type is all
    assert client.post(
        "/api/subscriptions", json={"email": "a@b.co", "match_type": "drug_name"}
    ).status_code == 422
    assert client.post(
        "/api/subscriptions", json={"email": "a@b.co", "match_type": "all"}
    ).status_code == 201


def test_verify_flow(setup):
    client, factory = setup
    client.post(
        "/api/subscriptions",
        json={"email": "a@b.co", "match_type": "all"},
    )
    with factory() as s:
        sub = s.execute(select(Subscription)).scalar_one()

    resp = client.get(f"/api/subscriptions/verify?token={sub.verify_token}")
    assert resp.status_code == 200
    assert "confirmed" in resp.text.lower()
    with factory() as s:
        assert s.get(Subscription, sub.id).verified is True

    assert client.get("/api/subscriptions/verify?token=wrong").status_code == 404


def test_unsubscribe_flow(setup):
    client, factory = setup
    client.post("/api/subscriptions", json={"email": "a@b.co", "match_type": "all"})
    with factory() as s:
        sub = s.execute(select(Subscription)).scalar_one()

    resp = client.get(f"/api/subscriptions/unsubscribe?token={sub.unsubscribe_token}")
    assert resp.status_code == 200
    with factory() as s:
        assert s.execute(select(Subscription)).scalar_one_or_none() is None

    assert client.get("/api/subscriptions/unsubscribe?token=wrong").status_code == 404


def test_rss_feed(setup, normalized_records):
    client, factory = setup
    import copy

    changed = copy.deepcopy(normalized_records)
    changed[1]["status"] = "Resolved"
    with factory() as s:
        sync_records(s, changed)
        s.commit()

    resp = client.get("/feed.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert "Resolved: Amoxicillin" in resp.text
    assert "<rss" in resp.text


def test_stats_trends(setup):
    client, _ = setup
    data = client.get("/api/stats").json()
    cats = {c["category"]: c["drugs"] for c in data["top_categories"]}
    # Only drugs with at least one Current record count; cisplatin is resolved.
    assert cats == {"Anti-Infective": 1, "Central Nervous System; Stimulant": 1}
    # Oldest current shortage first.
    assert data["longest_current"][0]["generic_name"].startswith("Amoxicillin")
    assert data["longest_current"][0]["posted"] == "2025-10-28"
