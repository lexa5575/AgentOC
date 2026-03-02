"""Tests for db.email_history module."""

from db.email_history import (
    email_already_processed,
    get_email_history,
    get_gmail_state,
    save_email,
    set_gmail_state,
)


def test_save_and_get_email():
    save_email("client@example.com", "inbound", "Order 123", "Body text", "new_order")
    history = get_email_history("client@example.com")
    assert len(history) == 1
    assert history[0]["subject"] == "Order 123"
    assert history[0]["direction"] == "inbound"
    assert history[0]["situation"] == "new_order"


def test_email_case_insensitive():
    save_email("CLIENT@Example.com", "inbound", "Test", "Body", "other")
    history = get_email_history("client@example.com")
    assert len(history) == 1


def test_email_history_recent_first():
    for i in range(5):
        save_email("multi@example.com", "inbound", f"Msg {i}", f"Body {i}", "other")
    history = get_email_history("multi@example.com")
    assert len(history) == 5
    # Chronological order (oldest first)
    assert history[0]["subject"] == "Msg 0"
    assert history[-1]["subject"] == "Msg 4"


def test_email_history_priority_selection(db_session):
    """With >10 emails, high-priority earlier messages are preferred over low-priority."""
    from datetime import datetime, timedelta
    from db.models import EmailHistory

    session = db_session()
    base = datetime(2025, 1, 1)

    # 10 older emails: 5 orders (high priority) + 5 tracking (low priority)
    for i in range(5):
        session.add(EmailHistory(
            client_email="prio@example.com", direction="inbound",
            subject=f"Order {i}", body="body", situation="new_order",
            created_at=base + timedelta(hours=i),
        ))
    for i in range(5):
        session.add(EmailHistory(
            client_email="prio@example.com", direction="inbound",
            subject=f"Track {i}", body="body", situation="tracking",
            created_at=base + timedelta(hours=5 + i),
        ))
    # 3 most recent
    for i in range(3):
        session.add(EmailHistory(
            client_email="prio@example.com", direction="inbound",
            subject=f"Recent {i}", body="body", situation="tracking",
            created_at=base + timedelta(hours=10 + i),
        ))
    session.commit()
    session.close()

    history = get_email_history("prio@example.com", max_total=10)
    assert len(history) == 10
    subjects = [h["subject"] for h in history]
    # The 3 most recent are always included
    for i in range(3):
        assert f"Recent {i}" in subjects
    # High-priority orders should beat low-priority tracking
    order_count = sum(1 for s in subjects if s.startswith("Order"))
    assert order_count >= 4


def test_email_history_empty():
    assert get_email_history("nobody@example.com") == []


def test_email_already_processed():
    save_email("dup@example.com", "inbound", "Test", "Body", "other", gmail_message_id="msg123")
    assert email_already_processed("msg123") is True
    assert email_already_processed("msg999") is False


def test_gmail_state():
    assert get_gmail_state() is None

    set_gmail_state("12345")
    assert get_gmail_state() == "12345"

    set_gmail_state("67890")
    assert get_gmail_state() == "67890"
