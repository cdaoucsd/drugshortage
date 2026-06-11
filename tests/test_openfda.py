from datetime import date

import httpx

from app.openfda import OpenFDAClient, normalize_result, parse_fda_date


def test_parse_fda_date_formats():
    assert parse_fda_date("2026-06-01") == date(2026, 6, 1)
    assert parse_fda_date("20260601") == date(2026, 6, 1)
    assert parse_fda_date("06/01/2026") == date(2026, 6, 1)
    assert parse_fda_date(None) is None
    assert parse_fda_date("not a date") is None


def test_normalize_canonical_fields(fixture_payload):
    rec = normalize_result(fixture_payload["results"][0])
    assert rec["generic_name"] == "Amoxicillin Oral Powder for Suspension"
    assert rec["company_name"] == "Acme Pharma Inc."
    assert rec["status"] == "Current"
    assert rec["initial_posting_date"] == date(2025, 10, 28)
    assert rec["update_date"] == date(2026, 6, 1)
    assert rec["therapeutic_category"] == "Anti-Infective"
    assert rec["raw"]["openfda"]["rxcui"] == ["308182"]


def test_normalize_alias_fields(fixture_payload):
    # Fourth fixture record uses the alternate field spellings.
    rec = normalize_result(fixture_payload["results"][3])
    assert rec["update_date"] == date(2026, 4, 11)
    assert rec["update_type"] == "Revised"
    assert rec["shortage_reason"] == "Delay in shipping of the drug"
    assert rec["availability"] == "Limited supply available"
    assert rec["related_info"] == "DEA quota related."
    assert rec["initial_posting_date"] == date(2025, 12, 1)  # YYYYMMDD form
    assert rec["therapeutic_category"] == "Central Nervous System; Stimulant"


def test_normalize_missing_fields(fixture_payload):
    rec = normalize_result(fixture_payload["results"][2])
    assert rec["proprietary_name"] is None
    assert rec["availability"] is None
    assert rec["resolved_note"] == "All presentations available."


def _mock_client(pages: list[dict]) -> httpx.Client:
    calls = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(calls))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_iter_all_paginates():
    def page(skip, total, n):
        return {
            "meta": {"results": {"skip": skip, "limit": 1000, "total": total}},
            "results": [{"generic_name": f"Drug {skip + i}"} for i in range(n)],
        }

    client = OpenFDAClient(client=_mock_client([page(0, 1500, 1000), page(1000, 1500, 500)]))
    records = client.fetch_all()
    assert len(records) == 1500
    assert records[0]["generic_name"] == "Drug 0"
    assert records[-1]["generic_name"] == "Drug 1499"


def test_iter_all_stops_on_empty_page():
    empty = {"meta": {}, "results": []}
    client = OpenFDAClient(client=_mock_client([empty]))
    assert client.fetch_all() == []
