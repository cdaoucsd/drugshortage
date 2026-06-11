"""SQLAlchemy models for the drug shortage tracker.

One ShortageRecord per openFDA record, i.e. per (drug, company, presentation).
ShortageEvent is an append-only change log produced by the sync diff; it powers
both notifications and the per-drug history timeline.
"""

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ShortageRecord(Base):
    __tablename__ = "shortage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    # openFDA shortage records carry no stable ID, so identity is a hash of
    # the fields that distinguish one record from another (see sync.record_key).
    record_key: Mapped[str] = mapped_column(String(64), unique=True)

    generic_name: Mapped[str | None] = mapped_column(Text)
    proprietary_name: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str | None] = mapped_column(String(32))  # Current | Resolved
    initial_posting_date: Mapped[date | None] = mapped_column(Date)
    update_date: Mapped[date | None] = mapped_column(Date)
    update_type: Mapped[str | None] = mapped_column(Text)
    resolved_date: Mapped[date | None] = mapped_column(Date)
    discontinued_date: Mapped[date | None] = mapped_column(Date)

    shortage_reason: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str | None] = mapped_column(Text)
    related_info: Mapped[str | None] = mapped_column(Text)
    resolved_note: Mapped[str | None] = mapped_column(Text)
    therapeutic_category: Mapped[str | None] = mapped_column(Text)

    strength: Mapped[str | None] = mapped_column(Text)
    dosage_form: Mapped[str | None] = mapped_column(Text)
    presentation: Mapped[str | None] = mapped_column(Text)
    package_ndc: Mapped[str | None] = mapped_column(Text)
    contact_info: Mapped[str | None] = mapped_column(Text)

    raw_json: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    events: Mapped[list["ShortageEvent"]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_shortage_records_generic_name", "generic_name"),
        Index("ix_shortage_records_company_name", "company_name"),
        Index("ix_shortage_records_status", "status"),
        Index("ix_shortage_records_initial_posting_date", "initial_posting_date"),
        Index("ix_shortage_records_resolved_date", "resolved_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ShortageRecord {self.generic_name!r} / {self.company_name!r} "
            f"[{self.status}]>"
        )


class ShortageEvent(Base):
    __tablename__ = "shortage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(
        ForeignKey("shortage_records.id", ondelete="CASCADE")
    )
    # new_shortage | updated | resolved | reposted
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # JSON: {field: {"old": ..., "new": ...}} for `updated` events
    diff_json: Mapped[str | None] = mapped_column(Text)
    # Set once the notifier has processed this event (digest watermark).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    record: Mapped[ShortageRecord] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_shortage_events_record_id", "record_id"),
        Index("ix_shortage_events_event_type", "event_type"),
        Index("ix_shortage_events_occurred_at", "occurred_at"),
        Index("ix_shortage_events_notified_at", "notified_at"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    # drug_name | manufacturer | category | all
    match_type: Mapped[str] = mapped_column(String(32))
    # substring matched case-insensitively against the relevant field;
    # empty for match_type=all
    match_value: Mapped[str | None] = mapped_column(Text)
    # comma-separated event types to receive, e.g. "new_shortage,resolved"
    notify_on: Mapped[str] = mapped_column(
        Text, default="new_shortage,updated,resolved,reposted"
    )

    verified: Mapped[bool] = mapped_column(default=False)
    verify_token: Mapped[str] = mapped_column(String(64), unique=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_subscriptions_email", "email"),
        Index("ix_subscriptions_verified", "verified"),
    )

    def notify_on_types(self) -> set[str]:
        return {t.strip() for t in (self.notify_on or "").split(",") if t.strip()}
