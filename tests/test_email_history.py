"""Tests for db.email_history module."""

from db.email_history import (
    email_already_processed,
    get_email_history,
    get_gmail_state,
    get_thread_history,
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


def test_save_and_get_with_thread_id():
    """Test saving and retrieving emails with gmail_thread_id."""
    thread_id = "thread_abc123"
    
    save_email(
        "thread_test@example.com",
        "inbound",
        "Thread Test",
        "Message 1",
        "new_order",
        gmail_message_id="msg_001",
        gmail_thread_id=thread_id,
    )
    save_email(
        "thread_test@example.com",
        "outbound",
        "Re: Thread Test",
        "Reply 1",
        "new_order",
        gmail_thread_id=thread_id,
    )
    
    # Test get_email_history includes thread_id
    history = get_email_history("thread_test@example.com")
    assert len(history) == 2
    assert history[0]["gmail_thread_id"] == thread_id
    assert history[1]["gmail_thread_id"] == thread_id


def test_get_thread_history():
    """Test fetching emails by thread_id."""
    thread_id = "thread_xyz789"
    other_thread = "thread_other"
    
    # Save emails in different threads
    save_email(
        "thread_hist@example.com",
        "inbound",
        "Thread A - Msg 1",
        "Body",
        "other",
        gmail_thread_id=thread_id,
    )
    save_email(
        "thread_hist@example.com",
        "outbound",
        "Re: Thread A - Msg 1",
        "Reply",
        "other",
        gmail_thread_id=thread_id,
    )
    save_email(
        "thread_hist@example.com",
        "inbound",
        "Thread B - Different",
        "Other body",
        "tracking",
        gmail_thread_id=other_thread,
    )
    
    # Get only the specific thread
    thread_history = get_thread_history(thread_id)
    assert len(thread_history) == 2
    assert all(h["gmail_thread_id"] == thread_id for h in thread_history)
    assert thread_history[0]["subject"] == "Thread A - Msg 1"  # oldest first
    
    # Other thread
    other_history = get_thread_history(other_thread)
    assert len(other_history) == 1
    assert other_history[0]["gmail_thread_id"] == other_thread


def test_get_thread_history_empty():
    """Test get_thread_history returns empty list for unknown thread."""
    assert get_thread_history("nonexistent_thread_id") == []


def test_get_thread_history_limit_returns_latest_window():
    """With limit, get_thread_history should return the latest N messages (still oldest-first)."""
    thread_id = "thread_limit_latest"

    for i in range(6):
        save_email(
            "thread_limit@example.com",
            "inbound",
            f"Msg {i}",
            f"Body {i}",
            "other",
            gmail_thread_id=thread_id,
        )

    limited = get_thread_history(thread_id, limit=3)
    assert [m["subject"] for m in limited] == ["Msg 3", "Msg 4", "Msg 5"]


# ---------------------------------------------------------------------------
# get_full_thread_history tests (Phase 1)
# ---------------------------------------------------------------------------
from unittest.mock import patch

import db.email_history as _eh_module
from db.email_history import get_full_thread_history


def test_full_thread_history_sufficient_local():
    """When >=2 local records exist, Gmail is NOT called."""
    thread_id = "thread_local_ok"
    save_email("a@example.com", "inbound", "Order", "Body", "new_order", gmail_thread_id=thread_id)
    save_email("a@example.com", "outbound", "Re: Order", "Reply", "new_order", gmail_thread_id=thread_id)

    with patch.object(_eh_module, "_fetch_gmail_thread_by_id") as mock_gmail:
        result = get_full_thread_history(thread_id)

    mock_gmail.assert_not_called()
    assert len(result) == 2
    assert result[0]["direction"] == "inbound"
    assert result[1]["direction"] == "outbound"


def test_full_thread_history_supplements_from_gmail():
    """When <2 local records, supplements from Gmail API."""
    thread_id = "thread_sparse"
    save_email("b@example.com", "inbound", "Hello", "Body", "other", gmail_thread_id=thread_id)

    from datetime import datetime, timezone as tz
    gmail_msgs = [
        {
            "client_email": "b@example.com",
            "direction": "outbound",
            "subject": "Re: Hello",
            "body": "Our reply",
            "situation": "unknown",
            "created_at": datetime(2025, 6, 1, 12, 0, tzinfo=tz.utc),
        },
    ]

    with patch.object(_eh_module, "_fetch_gmail_thread_by_id", return_value=gmail_msgs):
        result = get_full_thread_history(thread_id)

    assert len(result) == 2
    subjects = [r["subject"] for r in result]
    assert "Hello" in subjects
    assert "Re: Hello" in subjects


def test_full_thread_history_deduplicates():
    """Duplicate (subject, direction) pairs from Gmail are not added."""
    thread_id = "thread_dedup"
    save_email("c@example.com", "inbound", "Order 99", "Body", "new_order", gmail_thread_id=thread_id)

    from datetime import datetime, timezone as tz
    gmail_msgs = [
        {
            "client_email": "c@example.com",
            "direction": "inbound",
            "subject": "Order 99",  # same subject+direction as local
            "body": "Body from Gmail",
            "situation": "unknown",
            "created_at": datetime(2025, 6, 1, 10, 0, tzinfo=tz.utc),
        },
        {
            "client_email": "c@example.com",
            "direction": "outbound",
            "subject": "Re: Order 99",  # new — should be added
            "body": "Our reply",
            "situation": "unknown",
            "created_at": datetime(2025, 6, 1, 11, 0, tzinfo=tz.utc),
        },
    ]

    with patch.object(_eh_module, "_fetch_gmail_thread_by_id", return_value=gmail_msgs):
        result = get_full_thread_history(thread_id)

    assert len(result) == 2  # 1 local + 1 new from Gmail (duplicate skipped)
    directions = [r["direction"] for r in result]
    assert "inbound" in directions
    assert "outbound" in directions
