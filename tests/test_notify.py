import copy

import pytest
from sqlalchemy import select

from app.mailer import ConsoleMailer
from app.models import ShortageEvent, Subscription
from app.notify import new_token, run_notifier, send_verification
from app.sync import sync_records


def make_sub(session, email="user@example.com", match_type="drug_name",
             match_value="amoxicillin", notify_on="new_shortage,updated,resolved,reposted",
             verified=True) -> Subscription:
    sub = Subscription(
        email=email,
        match_type=match_type,
        match_value=match_value,
        notify_on=notify_on,
        verified=verified,
        verify_token=new_token(),
        unsubscribe_token=new_token(),
    )
    session.add(sub)
    session.flush()
    return sub


@pytest.fixture
def mailer():
    return ConsoleMailer()


def resolve_record(records, idx):
    changed = copy.deepcopy(records)
    changed[idx]["status"] = "Resolved"
    changed[idx]["update_type"] = "Resolved"
    return changed


def test_drug_name_match_sends_digest(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session)
    sync_records(session, resolve_record(normalized_records, 1))

    assert run_notifier(session, mailer) == 1
    (msg,) = mailer.sent
    assert msg.to == "user@example.com"
    assert "1 resolved" in msg.subject
    assert "Amoxicillin" in msg.text
    assert "unsubscribe?token=" in msg.text


def test_unverified_subscription_gets_nothing(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session, verified=False)
    sync_records(session, resolve_record(normalized_records, 1))
    assert run_notifier(session, mailer) == 0
    assert mailer.sent == []


def test_event_type_filter(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session, notify_on="new_shortage")  # not interested in resolved
    sync_records(session, resolve_record(normalized_records, 1))
    assert run_notifier(session, mailer) == 0


def test_manufacturer_and_all_matchers(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session, email="mfr@example.com", match_type="manufacturer",
             match_value="beta generics")
    make_sub(session, email="everything@example.com", match_type="all",
             match_value=None)
    make_sub(session, email="other@example.com", match_type="manufacturer",
             match_value="unrelated pharma")
    sync_records(session, resolve_record(normalized_records, 1))

    assert run_notifier(session, mailer) == 2
    assert {m.to for m in mailer.sent} == {"mfr@example.com", "everything@example.com"}


def test_one_digest_per_email_even_with_multiple_matches(
    session, normalized_records, mailer
):
    sync_records(session, normalized_records[:2], emit_events=False)
    make_sub(session, match_type="all", match_value=None)
    # Two new records + one resolution = 3 pending events, one email.
    changed = resolve_record(normalized_records, 1)
    sync_records(session, changed)

    assert run_notifier(session, mailer) == 1
    (msg,) = mailer.sent
    assert "2 new shortage" in msg.subject
    assert "1 resolved" in msg.subject


def test_events_marked_notified_and_not_resent(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session, match_type="all", match_value=None)
    sync_records(session, resolve_record(normalized_records, 1))

    assert run_notifier(session, mailer) == 1
    assert run_notifier(session, mailer) == 0  # nothing pending anymore
    events = session.execute(select(ShortageEvent)).scalars().all()
    assert all(e.notified_at is not None for e in events)


def test_events_consumed_even_without_subscribers(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    sync_records(session, resolve_record(normalized_records, 1))
    assert run_notifier(session, mailer) == 0
    # Late subscriber must not receive back-dated alerts.
    make_sub(session, match_type="all", match_value=None)
    assert run_notifier(session, mailer) == 0


def test_category_matcher(session, normalized_records, mailer):
    sync_records(session, normalized_records, emit_events=False)
    make_sub(session, match_type="category", match_value="oncology")
    changed = copy.deepcopy(normalized_records)
    changed[2]["status"] = "Current"  # cisplatin back in shortage
    sync_records(session, changed)

    assert run_notifier(session, mailer) == 1
    assert "BACK IN SHORTAGE" in mailer.sent[0].text


def test_verification_email(session, mailer):
    sub = make_sub(session, verified=False)
    send_verification(sub, mailer)
    (msg,) = mailer.sent
    assert msg.to == sub.email
    assert f"verify?token={sub.verify_token}" in msg.text
    assert 'drug name "amoxicillin"' in msg.text
