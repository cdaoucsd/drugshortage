"""Notification engine.

After each sync, ``run_notifier`` takes every event not yet processed
(``notified_at IS NULL``), matches it against verified subscriptions, and
sends one digest email per subscriber per run — never one email per event.
Events are stamped ``notified_at`` whether or not anyone matched, so a
subscriber added later doesn't get back-dated alerts.
"""

import os
import secrets
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.mailer import Mailer, Message
from app.models import ShortageEvent, ShortageRecord, Subscription, utcnow

EVENT_TYPES = ("new_shortage", "updated", "resolved", "reposted")
MATCH_TYPES = ("drug_name", "manufacturer", "category", "all")

EVENT_LABELS = {
    "new_shortage": "NEW SHORTAGE",
    "updated": "UPDATED",
    "resolved": "RESOLVED",
    "reposted": "BACK IN SHORTAGE",
}


def base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def subscription_matches(sub: Subscription, event: ShortageEvent) -> bool:
    if event.event_type not in sub.notify_on_types():
        return False
    record = event.record
    if sub.match_type == "all":
        return True
    needle = (sub.match_value or "").strip().lower()
    if not needle:
        return False
    if sub.match_type == "drug_name":
        hay = f"{record.generic_name or ''} {record.proprietary_name or ''}"
    elif sub.match_type == "manufacturer":
        hay = record.company_name or ""
    elif sub.match_type == "category":
        hay = record.therapeutic_category or ""
    else:
        return False
    return needle in hay.lower()


def _event_line(event: ShortageEvent) -> str:
    record = event.record
    label = EVENT_LABELS.get(event.event_type, event.event_type.upper())
    parts = [f"[{label}] {record.generic_name or 'Unknown drug'}"]
    if record.company_name:
        parts.append(f"({record.company_name})")
    if record.presentation:
        parts.append(f"- {record.presentation}")
    line = " ".join(parts)
    if event.event_type == "resolved" and record.resolved_date:
        line += f"\n    resolved on {record.resolved_date.isoformat()}"
    elif event.event_type == "new_shortage":
        if record.initial_posting_date:
            line += f"\n    posted {record.initial_posting_date.isoformat()}"
        if record.shortage_reason:
            line += f"\n    reason: {record.shortage_reason}"
    return line


def build_digest(
    email: str, items: list[tuple[Subscription, ShortageEvent]]
) -> Message:
    events = {e.id: e for _, e in items}.values()
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[event.event_type] += 1
    summary = ", ".join(
        f"{n} {EVENT_LABELS[t].lower()}"
        for t, n in sorted(counts.items())
        if t in EVENT_LABELS
    )

    lines = [_event_line(e) for e in sorted(events, key=lambda e: e.event_type)]
    unsub_tokens = {sub.unsubscribe_token for sub, _ in items}
    footer = "\n".join(
        f"Unsubscribe: {base_url()}/api/subscriptions/unsubscribe?token={t}"
        for t in sorted(unsub_tokens)
    )
    body = (
        "FDA drug shortage changes matching your subscriptions:\n\n"
        + "\n\n".join(lines)
        + f"\n\nDetails: {base_url()}/\n\n---\n{footer}\n"
    )
    return Message(
        to=email, subject=f"Drug shortage alert: {summary}", text=body
    )


def send_verification(sub: Subscription, mailer: Mailer) -> None:
    target = (
        "all shortages"
        if sub.match_type == "all"
        else f'{sub.match_type.replace("_", " ")} "{sub.match_value}"'
    )
    link = f"{base_url()}/api/subscriptions/verify?token={sub.verify_token}"
    mailer.send(
        Message(
            to=sub.email,
            subject="Confirm your drug shortage alerts",
            text=(
                f"You asked for drug shortage alerts for {target}.\n\n"
                f"Confirm this subscription:\n{link}\n\n"
                "If you didn't request this, ignore this email and nothing "
                "will be sent.\n"
            ),
        )
    )


def run_notifier(session: Session, mailer: Mailer) -> int:
    """Process pending events; returns the number of digest emails sent."""
    pending = (
        session.execute(
            select(ShortageEvent)
            .options(selectinload(ShortageEvent.record))
            .where(ShortageEvent.notified_at.is_(None))
            .order_by(ShortageEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    if not pending:
        return 0

    subs = (
        session.execute(
            select(Subscription).where(Subscription.verified.is_(True))
        )
        .scalars()
        .all()
    )

    by_email: dict[str, list[tuple[Subscription, ShortageEvent]]] = defaultdict(list)
    for event in pending:
        for sub in subs:
            if subscription_matches(sub, event):
                by_email[sub.email].append((sub, event))

    sent = 0
    for email, items in sorted(by_email.items()):
        mailer.send(build_digest(email, items))
        sent += 1

    now = utcnow()
    for event in pending:
        event.notified_at = now
    session.flush()
    return sent
