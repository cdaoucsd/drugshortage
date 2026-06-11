"""FastAPI application: JSON API plus the static search UI.

Endpoints:
- GET  /api/drugs                grouped-by-drug list (search + filters + paging)
- GET  /api/drugs/{generic_name} one drug: all company/presentation records + events
- GET  /api/shortages            flat record-level search
- GET  /api/stats                dashboard counts and trends
- POST /api/subscriptions        create an email alert subscription
- GET  /api/subscriptions/verify | /unsubscribe   token links from emails
- GET  /feed.xml                 RSS feed of recent change events

Set SYNC_INTERVAL_HOURS to run the openFDA sync + notifier in-process on a
schedule; otherwise run `python -m app.cli sync` from cron.
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_engine, init_db, make_session_factory
from app.external import ashp_link, fetch_alternatives, fetch_recalls
from app.mailer import get_mailer
from app.models import ShortageEvent, ShortageRecord, Subscription
from app.notify import (
    EVENT_TYPES,
    MATCH_TYPES,
    new_token,
    run_notifier,
    send_verification,
)


def _sync_and_notify() -> None:
    from app.openfda import OpenFDAClient
    from app.sync import sync_records

    client = OpenFDAClient(api_key=os.environ.get("OPENFDA_API_KEY"))
    try:
        records = client.fetch_all()
    finally:
        client.close()
    factory = _get_session_factory()
    with factory() as session:
        result = sync_records(session, records)
        sent = run_notifier(session, get_mailer())
        session.commit()
    print(f"[scheduler] sync: {result.summary()}; digests sent: {sent}")


async def _scheduler_loop(interval_hours: float) -> None:
    while True:
        try:
            await asyncio.to_thread(_sync_and_notify)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"[scheduler] sync failed: {exc!r}")
        await asyncio.sleep(interval_hours * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    interval = float(os.environ.get("SYNC_INTERVAL_HOURS", "0") or 0)
    task = asyncio.create_task(_scheduler_loop(interval)) if interval > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Drug Shortage Tracker", version="0.1.0", lifespan=lifespan)

_session_factory = None


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        init_db(engine)
        _session_factory = make_session_factory(engine)
    return _session_factory


def get_session():
    factory = _get_session_factory()
    with factory() as session:
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
    ashp_link: str
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
        ashp_link=ashp_link(generic_name),
        records=[RecordOut.model_validate(r) for r in records],
        events=events,
    )


def _shortage_companies(session: Session, generic_name: str) -> list[str]:
    rows = session.execute(
        select(func.distinct(ShortageRecord.company_name)).where(
            ShortageRecord.generic_name == generic_name,
            ShortageRecord.status.ilike("current"),
            ShortageRecord.company_name.is_not(None),
        )
    ).all()
    if not rows:
        # Drug unknown (or fully resolved): distinguish 404 from "no companies".
        exists = session.execute(
            select(ShortageRecord.id)
            .where(ShortageRecord.generic_name == generic_name)
            .limit(1)
        ).first()
        if not exists:
            raise HTTPException(404, f"no shortage records for {generic_name!r}")
    return [r[0] for r in rows]


@app.get("/api/drugs/{generic_name}/alternatives")
def drug_alternatives(generic_name: str, session: Session = Depends(get_session)):
    companies = _shortage_companies(session, generic_name)
    return fetch_alternatives(generic_name, companies)


@app.get("/api/drugs/{generic_name}/recalls")
def drug_recalls(generic_name: str, session: Session = Depends(get_session)):
    _shortage_companies(session, generic_name)  # 404 for unknown drugs
    return fetch_recalls(generic_name)


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


class SubscriptionIn(BaseModel):
    email: str
    match_type: str = "drug_name"
    match_value: str | None = None
    notify_on: list[str] = list(EVENT_TYPES)

    @field_validator("email")
    @classmethod
    def email_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email address")
        return v

    @field_validator("match_type")
    @classmethod
    def known_match_type(cls, v: str) -> str:
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {MATCH_TYPES}")
        return v

    @field_validator("notify_on")
    @classmethod
    def known_event_types(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(EVENT_TYPES)
        if bad or not v:
            raise ValueError(f"notify_on must be a non-empty subset of {EVENT_TYPES}")
        return v


@app.post("/api/subscriptions", status_code=201)
def create_subscription(
    body: SubscriptionIn, session: Session = Depends(get_session)
):
    if body.match_type != "all" and not (body.match_value or "").strip():
        raise HTTPException(422, "match_value required unless match_type is 'all'")
    sub = Subscription(
        email=body.email,
        match_type=body.match_type,
        match_value=(body.match_value or "").strip() or None,
        notify_on=",".join(body.notify_on),
        verify_token=new_token(),
        unsubscribe_token=new_token(),
    )
    session.add(sub)
    session.commit()
    send_verification(sub, get_mailer())
    return {"message": "verification email sent", "id": sub.id}


def _html_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{title}</title>"
        f'<link rel="stylesheet" href="/style.css"></head>'
        f'<body><main style="padding:3rem 2rem"><h2>{title}</h2>'
        f'<p>{body}</p><p><a href="/">← Back to Drug Shortage Tracker</a></p>'
        f"</main></body></html>",
        status_code=status_code,
    )


@app.get("/api/subscriptions/verify", response_class=HTMLResponse)
def verify_subscription(token: str, session: Session = Depends(get_session)):
    sub = session.execute(
        select(Subscription).where(Subscription.verify_token == token)
    ).scalar_one_or_none()
    if sub is None:
        return _html_page("Invalid link", "This verification link is not valid.", 404)
    sub.verified = True
    session.commit()
    return _html_page(
        "Subscription confirmed",
        f"Alerts will be sent to <b>{xml_escape(sub.email)}</b>.",
    )


@app.get("/api/subscriptions/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str, session: Session = Depends(get_session)):
    result = session.execute(
        delete(Subscription).where(Subscription.unsubscribe_token == token)
    )
    session.commit()
    if result.rowcount == 0:
        return _html_page("Invalid link", "This unsubscribe link is not valid.", 404)
    return _html_page("Unsubscribed", "You will receive no further alerts.")


@app.get("/feed.xml")
def rss_feed(session: Session = Depends(get_session)):
    events = (
        session.execute(
            select(ShortageEvent)
            .options(selectinload(ShortageEvent.record))
            .order_by(ShortageEvent.occurred_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    labels = {
        "new_shortage": "New shortage",
        "updated": "Updated",
        "resolved": "Resolved",
        "reposted": "Back in shortage",
    }
    items = []
    for e in events:
        rec = e.record
        title = f"{labels.get(e.event_type, e.event_type)}: {rec.generic_name}"
        if rec.company_name:
            title += f" ({rec.company_name})"
        desc_bits = [
            b
            for b in (rec.presentation, rec.shortage_reason, rec.availability)
            if b
        ]
        items.append(
            "<item>"
            f"<title>{xml_escape(title)}</title>"
            f"<description>{xml_escape(' — '.join(desc_bits))}</description>"
            f"<pubDate>{e.occurred_at.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
            f"<guid isPermaLink=\"false\">shortage-event-{e.id}</guid>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Drug Shortage Tracker — recent changes</title>"
        "<link>/</link>"
        "<description>New, updated, and resolved FDA drug shortages</description>"
        + "".join(items)
        + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")


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
    top_categories = [
        {"category": cat, "drugs": n}
        for cat, n in session.execute(
            select(
                ShortageRecord.therapeutic_category,
                func.count(func.distinct(ShortageRecord.generic_name)),
            )
            .where(
                ShortageRecord.status.ilike("current"),
                ShortageRecord.therapeutic_category.is_not(None),
            )
            .group_by(ShortageRecord.therapeutic_category)
            .order_by(func.count(func.distinct(ShortageRecord.generic_name)).desc())
            .limit(5)
        ).all()
    ]
    longest_current = [
        {"generic_name": name, "posted": posted.isoformat() if posted else None}
        for name, posted in session.execute(
            select(
                ShortageRecord.generic_name,
                func.min(ShortageRecord.initial_posting_date).label("posted"),
            )
            .where(
                ShortageRecord.status.ilike("current"),
                ShortageRecord.initial_posting_date.is_not(None),
            )
            .group_by(ShortageRecord.generic_name)
            .order_by(func.min(ShortageRecord.initial_posting_date))
            .limit(5)
        ).all()
    ]
    return {
        "current_records": by_status.get("current", 0),
        "resolved_records": by_status.get("resolved", 0),
        "current_drugs": drug_count,
        "last_sync": last_sync,
        "events_last_7_days": recent,
        "top_categories": top_categories,
        "longest_current": longest_current,
    }


# Static frontend; mounted last so /api/* wins.
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
