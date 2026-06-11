"""FastAPI application: JSON API plus the static search UI.

Endpoints:
- GET /api/drugs                grouped-by-drug list (search + filters + paging)
- GET /api/drugs/{generic_name} one drug: all company/presentation records + events
- GET /api/shortages            flat record-level search
- GET /api/stats                dashboard counts
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_engine, init_db, make_session_factory
from app.models import ShortageEvent, ShortageRecord

app = FastAPI(title="Drug Shortage Tracker", version="0.1.0")

_session_factory = None


def get_session():
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        init_db(engine)
        _session_factory = make_session_factory(engine)
    with _session_factory() as session:
        yield session


# ---------------------------------------------------------------- schemas


class RecordOut(BaseModel):
    id: int
    generic_name: str | None
    proprietary_name: str | None
    company_name: str | None
    status: str | None
    initial_posting_date: date | None
    update_date: date | None
    update_type: str | None
    resolved_date: date | None
    shortage_reason: str | None
    availability: str | None
    related_info: str | None
    resolved_note: str | None
    therapeutic_category: str | None
    strength: str | None
    dosage_form: str | None
    presentation: str | None
    package_ndc: str | None
    contact_info: str | None

    model_config = {"from_attributes": True}


class EventOut(BaseModel):
    id: int
    record_id: int
    company_name: str | None = None
    event_type: str
    occurred_at: datetime
    diff: dict[str, Any] | None = None


class DrugSummary(BaseModel):
    generic_name: str
    status: str  # Current if any record is current, else Resolved
    current_count: int
    record_count: int
    company_count: int
    therapeutic_category: str | None
    first_posted: date | None
    last_updated: date | None


class DrugListOut(BaseModel):
    total: int
    page: int
    per_page: int
    results: list[DrugSummary]


class DrugDetailOut(BaseModel):
    generic_name: str
    status: str
    records: list[RecordOut]
    events: list[EventOut]


class RecordListOut(BaseModel):
    total: int
    page: int
    per_page: int
    results: list[RecordOut]


# ---------------------------------------------------------------- filtering


def apply_filters(
    stmt: Select,
    q: str | None = None,
    status: str | None = None,
    company: str | None = None,
    category: str | None = None,
    posted_from: date | None = None,
    posted_to: date | None = None,
    resolved_from: date | None = None,
    resolved_to: date | None = None,
) -> Select:
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                ShortageRecord.generic_name.ilike(like),
                ShortageRecord.proprietary_name.ilike(like),
                ShortageRecord.company_name.ilike(like),
            )
        )
    if status:
        stmt = stmt.where(ShortageRecord.status.ilike(status))
    if company:
        stmt = stmt.where(ShortageRecord.company_name.ilike(f"%{company}%"))
    if category:
        stmt = stmt.where(ShortageRecord.therapeutic_category.ilike(f"%{category}%"))
    if posted_from:
        stmt = stmt.where(ShortageRecord.initial_posting_date >= posted_from)
    if posted_to:
        stmt = stmt.where(ShortageRecord.initial_posting_date <= posted_to)
    if resolved_from:
        stmt = stmt.where(ShortageRecord.resolved_date >= resolved_from)
    if resolved_to:
        stmt = stmt.where(ShortageRecord.resolved_date <= resolved_to)
    return stmt


FilterParams = {
    "q": Query(None, description="match drug name, brand name, or manufacturer"),
    "status": Query(None, description="Current or Resolved"),
    "company": Query(None),
    "category": Query(None),
    "posted_from": Query(None),
    "posted_to": Query(None),
    "resolved_from": Query(None),
    "resolved_to": Query(None),
}


# ---------------------------------------------------------------- endpoints


@app.get("/api/drugs", response_model=DrugListOut)
def list_drugs(
    session: Session = Depends(get_session),
    q: str | None = FilterParams["q"],
    status: str | None = FilterParams["status"],
    company: str | None = FilterParams["company"],
    category: str | None = FilterParams["category"],
    posted_from: date | None = FilterParams["posted_from"],
    posted_to: date | None = FilterParams["posted_to"],
    resolved_from: date | None = FilterParams["resolved_from"],
    resolved_to: date | None = FilterParams["resolved_to"],
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    current_count = func.sum(
        case((ShortageRecord.status.ilike("current"), 1), else_=0)
    ).label("current_count")
    stmt = (
        select(
            ShortageRecord.generic_name,
            current_count,
            func.count().label("record_count"),
            func.count(func.distinct(ShortageRecord.company_name)).label(
                "company_count"
            ),
            func.max(ShortageRecord.therapeutic_category).label("therapeutic_category"),
            func.min(ShortageRecord.initial_posting_date).label("first_posted"),
            func.max(ShortageRecord.update_date).label("last_updated"),
        )
        .where(ShortageRecord.generic_name.is_not(None))
        .group_by(ShortageRecord.generic_name)
    )
    stmt = apply_filters(
        stmt, q, status, company, category,
        posted_from, posted_to, resolved_from, resolved_to,
    )

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = session.execute(
        stmt.order_by(
            current_count.desc(),
            func.max(ShortageRecord.update_date).desc(),
            ShortageRecord.generic_name,
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    results = [
        DrugSummary(
            generic_name=r.generic_name,
            status="Current" if r.current_count else "Resolved",
            current_count=r.current_count,
            record_count=r.record_count,
            company_count=r.company_count,
            therapeutic_category=r.therapeutic_category,
            first_posted=r.first_posted,
            last_updated=r.last_updated,
        )
        for r in rows
    ]
    return DrugListOut(total=total, page=page, per_page=per_page, results=results)


@app.get("/api/drugs/{generic_name}", response_model=DrugDetailOut)
def drug_detail(generic_name: str, session: Session = Depends(get_session)):
    records = (
        session.execute(
            select(ShortageRecord)
            .options(selectinload(ShortageRecord.events))
            .where(ShortageRecord.generic_name == generic_name)
            .order_by(ShortageRecord.company_name, ShortageRecord.presentation)
        )
        .scalars()
        .all()
    )
    if not records:
        raise HTTPException(404, f"no shortage records for {generic_name!r}")

    events = sorted(
        (
            EventOut(
                id=e.id,
                record_id=e.record_id,
                company_name=rec.company_name,
                event_type=e.event_type,
                occurred_at=e.occurred_at,
                diff=json.loads(e.diff_json) if e.diff_json else None,
            )
            for rec in records
            for e in rec.events
        ),
        key=lambda e: e.occurred_at,
        reverse=True,
    )
    status = (
        "Current"
        if any((r.status or "").lower() == "current" for r in records)
        else "Resolved"
    )
    return DrugDetailOut(
        generic_name=generic_name,
        status=status,
        records=[RecordOut.model_validate(r) for r in records],
        events=events,
    )


@app.get("/api/shortages", response_model=RecordListOut)
def list_records(
    session: Session = Depends(get_session),
    q: str | None = FilterParams["q"],
    status: str | None = FilterParams["status"],
    company: str | None = FilterParams["company"],
    category: str | None = FilterParams["category"],
    posted_from: date | None = FilterParams["posted_from"],
    posted_to: date | None = FilterParams["posted_to"],
    resolved_from: date | None = FilterParams["resolved_from"],
    resolved_to: date | None = FilterParams["resolved_to"],
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    stmt = apply_filters(
        select(ShortageRecord), q, status, company, category,
        posted_from, posted_to, resolved_from, resolved_to,
    )
    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = (
        session.execute(
            stmt.order_by(
                ShortageRecord.update_date.desc(), ShortageRecord.generic_name
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    return RecordListOut(
        total=total,
        page=page,
        per_page=per_page,
        results=[RecordOut.model_validate(r) for r in rows],
    )


@app.get("/api/stats")
def stats(session: Session = Depends(get_session)):
    by_status = dict(
        session.execute(
            select(func.lower(ShortageRecord.status), func.count()).group_by(
                func.lower(ShortageRecord.status)
            )
        ).all()
    )
    drug_count = session.execute(
        select(func.count(func.distinct(ShortageRecord.generic_name))).where(
            ShortageRecord.status.ilike("current")
        )
    ).scalar_one()
    last_sync = session.execute(
        select(func.max(ShortageRecord.last_seen_at))
    ).scalar_one()
    recent = dict(
        session.execute(
            select(ShortageEvent.event_type, func.count())
            .where(
                ShortageEvent.occurred_at
                >= func.datetime("now", "-7 days")
            )
            .group_by(ShortageEvent.event_type)
        ).all()
    )
    return {
        "current_records": by_status.get("current", 0),
        "resolved_records": by_status.get("resolved", 0),
        "current_drugs": drug_count,
        "last_sync": last_sync,
        "events_last_7_days": recent,
    }


# Static frontend; mounted last so /api/* wins.
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
