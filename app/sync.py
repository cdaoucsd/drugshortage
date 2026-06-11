"""Sync engine: upsert normalized openFDA records and emit change events.

Event semantics (consumed by the future notifier and the history timeline):

- ``new_shortage``  record key never seen before
- ``resolved``      status flipped Current -> Resolved (sets resolved_date)
- ``reposted``      status flipped Resolved -> Current (clears resolved_date)
- ``updated``       any other tracked field changed

Seeding the database for the first time would otherwise emit thousands of
``new_shortage`` events, so ``sync_records(..., emit_events=False)`` suppresses
event creation for that initial import.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShortageEvent, ShortageRecord, utcnow

# Fields whose changes produce an `updated` event (status changes are handled
# separately as resolved/reposted). update_date alone isn't meaningful to
# users, so it's tracked together with update_type.
TRACKED_FIELDS = (
    "status",
    "update_date",
    "update_type",
    "shortage_reason",
    "availability",
    "related_info",
    "resolved_note",
    "therapeutic_category",
    "contact_info",
    "discontinued_date",
)

KEY_FIELDS = ("generic_name", "company_name", "presentation", "package_ndc")


def record_key(rec: dict[str, Any]) -> str:
    """Stable identity hash; openFDA shortage records have no native ID."""
    parts = [(rec.get(f) or "").strip().lower() for f in KEY_FIELDS]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    resolved: int = 0
    reposted: int = 0
    unchanged: int = 0
    events: list[ShortageEvent] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"created={self.created} updated={self.updated} "
            f"resolved={self.resolved} reposted={self.reposted} "
            f"unchanged={self.unchanged}"
        )


def _apply_fields(row: ShortageRecord, rec: dict[str, Any]) -> None:
    for f in (
        "generic_name",
        "proprietary_name",
        "company_name",
        "status",
        "initial_posting_date",
        "update_date",
        "update_type",
        "discontinued_date",
        "shortage_reason",
        "availability",
        "related_info",
        "resolved_note",
        "therapeutic_category",
        "strength",
        "dosage_form",
        "presentation",
        "package_ndc",
        "contact_info",
    ):
        setattr(row, f, rec.get(f))
    row.raw_json = json.dumps(rec.get("raw", {}), separators=(",", ":"))


def _derive_resolved_date(rec: dict[str, Any]) -> date | None:
    if (rec.get("status") or "").lower() == "resolved":
        return rec.get("update_date") or date.today()
    return None


def _add_event(
    session: Session,
    result: SyncResult,
    row: ShortageRecord,
    event_type: str,
    diff: dict[str, Any] | None = None,
) -> None:
    event = ShortageEvent(
        record=row,
        event_type=event_type,
        diff_json=json.dumps(diff, separators=(",", ":")) if diff else None,
    )
    session.add(event)
    result.events.append(event)


def sync_records(
    session: Session,
    records: list[dict[str, Any]],
    emit_events: bool = True,
) -> SyncResult:
    """Upsert normalized records (from OpenFDAClient) and emit change events.

    Records present in the DB but absent from `records` are left untouched;
    their stale last_seen_at distinguishes them from live data.
    """
    result = SyncResult()
    now = utcnow()

    existing: dict[str, ShortageRecord] = {
        row.record_key: row
        for row in session.execute(select(ShortageRecord)).scalars()
    }
    seen_keys: set[str] = set()

    for rec in records:
        key = record_key(rec)
        if key in seen_keys:
            # Duplicate identity within one feed pull; first occurrence wins.
            continue
        seen_keys.add(key)

        row = existing.get(key)
        if row is None:
            row = ShortageRecord(record_key=key, first_seen_at=now, last_seen_at=now)
            _apply_fields(row, rec)
            row.resolved_date = _derive_resolved_date(rec)
            session.add(row)
            result.created += 1
            if emit_events:
                _add_event(session, result, row, "new_shortage")
            continue

        row.last_seen_at = now
        old_status = (row.status or "").lower()
        new_status = (rec.get("status") or "").lower()
        diff = {
            f: {"old": _jsonable(getattr(row, f)), "new": _jsonable(rec.get(f))}
            for f in TRACKED_FIELDS
            if getattr(row, f) != rec.get(f)
        }

        if not diff:
            result.unchanged += 1
            continue

        _apply_fields(row, rec)

        if old_status != "resolved" and new_status == "resolved":
            row.resolved_date = _derive_resolved_date(rec)
            result.resolved += 1
            if emit_events:
                _add_event(session, result, row, "resolved", diff)
        elif old_status == "resolved" and new_status != "resolved":
            row.resolved_date = None
            result.reposted += 1
            if emit_events:
                _add_event(session, result, row, "reposted", diff)
        else:
            result.updated += 1
            if emit_events:
                _add_event(session, result, row, "updated", diff)

    session.flush()
    return result
