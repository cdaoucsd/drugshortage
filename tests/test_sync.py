import copy
import json
from datetime import date

from sqlalchemy import select

from app.models import ShortageEvent, ShortageRecord
from app.sync import record_key, sync_records


def get_row(session, company: str) -> ShortageRecord:
    return session.execute(
        select(ShortageRecord).where(ShortageRecord.company_name == company)
    ).scalar_one()


def test_initial_seed_creates_records_without_events(session, normalized_records):
    result = sync_records(session, normalized_records, emit_events=False)
    assert result.created == 4
    assert result.events == []
    assert session.execute(select(ShortageEvent)).scalars().all() == []

    row = get_row(session, "Acme Pharma Inc.")
    assert row.status == "Current"
    assert row.initial_posting_date == date(2025, 10, 28)
    assert row.resolved_date is None
    assert json.loads(row.raw_json)["generic_name"].startswith("Amoxicillin")


def test_seed_derives_resolved_date_for_already_resolved(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    row = get_row(session, "Gamma Oncology Co.")
    assert row.status == "Resolved"
    assert row.resolved_date == date(2025, 12, 15)


def test_new_record_emits_new_shortage_event(session, normalized_records):
    sync_records(session, normalized_records[:3], emit_events=False)
    result = sync_records(session, normalized_records)
    assert result.created == 1
    assert result.unchanged == 3
    assert [e.event_type for e in result.events] == ["new_shortage"]
    assert result.events[0].record.company_name == "Delta Therapeutics"


def test_resync_identical_data_is_noop(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    result = sync_records(session, normalized_records)
    assert result.unchanged == 4
    assert result.events == []


def test_field_change_emits_updated_with_diff(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    changed = copy.deepcopy(normalized_records)
    changed[0]["availability"] = "Available"
    changed[0]["update_date"] = date(2026, 6, 10)

    result = sync_records(session, changed)
    assert result.updated == 1
    (event,) = result.events
    assert event.event_type == "updated"
    diff = json.loads(event.diff_json)
    assert diff["availability"] == {
        "old": "Product on intermittent back order",
        "new": "Available",
    }
    assert diff["update_date"]["new"] == "2026-06-10"
    assert get_row(session, "Acme Pharma Inc.").availability == "Available"


def test_status_flip_emits_resolved_and_sets_date(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    changed = copy.deepcopy(normalized_records)
    changed[1]["status"] = "Resolved"
    changed[1]["update_type"] = "Resolved"
    changed[1]["update_date"] = date(2026, 6, 11)

    result = sync_records(session, changed)
    assert result.resolved == 1
    assert [e.event_type for e in result.events] == ["resolved"]
    row = get_row(session, "Beta Generics LLC")
    assert row.resolved_date == date(2026, 6, 11)


def test_resolved_back_to_current_emits_reposted(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    changed = copy.deepcopy(normalized_records)
    changed[2]["status"] = "Current"
    changed[2]["update_type"] = "Revised"

    result = sync_records(session, changed)
    assert result.reposted == 1
    assert [e.event_type for e in result.events] == ["reposted"]
    row = get_row(session, "Gamma Oncology Co.")
    assert row.status == "Current"
    assert row.resolved_date is None


def test_duplicate_keys_in_feed_first_wins(session, normalized_records):
    dupe = copy.deepcopy(normalized_records[0])
    dupe["availability"] = "conflicting copy"
    result = sync_records(session, normalized_records + [dupe], emit_events=False)
    assert result.created == 4
    assert get_row(session, "Acme Pharma Inc.").availability != "conflicting copy"


def test_record_key_stability():
    rec = {
        "generic_name": "Amoxicillin",
        "company_name": "Acme Pharma Inc.",
        "presentation": "250 mg/5 mL bottle",
        "package_ndc": "0001-0001-01",
    }
    shouting = {k: v.upper() for k, v in rec.items()}
    assert record_key(rec) == record_key(shouting)
    assert record_key(rec) != record_key({**rec, "company_name": "Other Co"})


def test_missing_record_left_untouched(session, normalized_records):
    sync_records(session, normalized_records, emit_events=False)
    result = sync_records(session, normalized_records[:2])
    assert result.unchanged == 2
    assert result.events == []
    # Record absent from the feed keeps its data and history.
    assert get_row(session, "Gamma Oncology Co.").status == "Resolved"
