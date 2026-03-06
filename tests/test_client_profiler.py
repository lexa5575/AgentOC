"""Tests for agents.client_profiler module."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.client_profiler import _backfill_order_items, generate_client_summary, maybe_refresh_summary


@patch("agents.client_profiler.get_full_email_history", return_value=[])
def test_generate_client_summary_no_history(mock_history):
    """Returns None and skips LLM when no history exists."""
    with patch("agents.client_profiler.profiler_agent.run") as run_mock:
        result = generate_client_summary("nobody@example.com")

    assert result is None
    run_mock.assert_not_called()
    mock_history.assert_called_once()


@patch("agents.client_profiler.get_full_email_history")
@patch("agents.client_profiler.format_email_history", return_value="=== CONVERSATION HISTORY ===")
def test_generate_client_summary_save_failure(mock_format, mock_history):
    """Returns None if summary generation succeeds but DB update fails."""
    mock_history.return_value = [
        {
            "direction": "inbound",
            "subject": "Hello",
            "body": "Need order",
            "created_at": None,
        }
    ]

    with (
        patch("agents.client_profiler.profiler_agent.run", return_value=SimpleNamespace(content="Loyal customer")) as run_mock,
        patch("agents.client_profiler.update_client_summary", return_value=False) as update_mock,
    ):
        result = generate_client_summary("missing@example.com")

    assert result is None
    run_mock.assert_called_once()
    update_mock.assert_called_once_with("missing@example.com", "Loyal customer")


@patch("agents.client_profiler.get_full_email_history")
@patch("agents.client_profiler.format_email_history", return_value="=== CONVERSATION HISTORY ===")
def test_generate_client_summary_success(mock_format, mock_history):
    """Returns summary when generation and DB save both succeed."""
    mock_history.return_value = [
        {
            "direction": "outbound",
            "subject": "Re: Hello",
            "body": "Thank you!",
            "created_at": None,
        }
    ]

    with (
        patch(
            "agents.client_profiler.profiler_agent.run",
            return_value=SimpleNamespace(content="  Frequent buyer  "),
        ) as run_mock,
        patch("agents.client_profiler.update_client_summary", return_value=True) as update_mock,
    ):
        result = generate_client_summary("ok@example.com")

    assert result == "Frequent buyer"
    run_mock.assert_called_once()
    update_mock.assert_called_once_with("ok@example.com", "Frequent buyer")


@patch("agents.client_profiler.get_full_email_history")
@patch("agents.client_profiler.format_email_history", return_value="=== CONVERSATION HISTORY ===")
def test_generate_client_summary_empty_llm_output(mock_format, mock_history):
    """Returns None and does not save when profiler produced empty text."""
    mock_history.return_value = [
        {
            "direction": "outbound",
            "subject": "Re: Hello",
            "body": "Thank you!",
            "created_at": None,
        }
    ]

    with (
        patch("agents.client_profiler.profiler_agent.run", return_value=SimpleNamespace(content="   ")) as run_mock,
        patch("agents.client_profiler.update_client_summary") as update_mock,
    ):
        result = generate_client_summary("ok@example.com")

    assert result is None
    run_mock.assert_called_once()
    update_mock.assert_not_called()


# ---------------------------------------------------------------------------
# maybe_refresh_summary tests (Phase 2)
# ---------------------------------------------------------------------------

@patch("agents.client_profiler.get_client_profile")
@patch("agents.client_profiler.generate_client_summary")
def test_maybe_refresh_fresh_summary_skipped(mock_generate, mock_profile):
    """Summary updated <24h ago → skip refresh, 0 LLM calls."""
    mock_profile.return_value = {
        "email": "fresh@example.com",
        "summary_updated_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }

    result = maybe_refresh_summary("fresh@example.com")

    assert result is None
    mock_generate.assert_not_called()


@patch("agents.client_profiler.get_client_profile")
@patch("agents.client_profiler.generate_client_summary", return_value="Updated summary")
def test_maybe_refresh_stale_summary_refreshed(mock_generate, mock_profile):
    """Summary updated >24h ago → call generate_client_summary."""
    mock_profile.return_value = {
        "email": "stale@example.com",
        "summary_updated_at": datetime.now(timezone.utc) - timedelta(hours=25),
    }

    result = maybe_refresh_summary("stale@example.com")

    assert result == "Updated summary"
    mock_generate.assert_called_once_with("stale@example.com", gmail_account="default")


@patch("agents.client_profiler.get_client_profile")
@patch("agents.client_profiler.generate_client_summary", return_value="First summary")
def test_maybe_refresh_never_generated(mock_generate, mock_profile):
    """summary_updated_at is None → call generate_client_summary."""
    mock_profile.return_value = {
        "email": "new@example.com",
        "summary_updated_at": None,
    }

    result = maybe_refresh_summary("new@example.com")

    assert result == "First summary"
    mock_generate.assert_called_once_with("new@example.com", gmail_account="default")


@patch("agents.client_profiler.get_client_profile", return_value=None)
@patch("agents.client_profiler.generate_client_summary")
def test_maybe_refresh_client_not_found(mock_generate, mock_profile):
    """Client not in DB → skip, no LLM call."""
    result = maybe_refresh_summary("ghost@example.com")

    assert result is None
    mock_generate.assert_not_called()


# ---------------------------------------------------------------------------
# _backfill_order_items tests
# ---------------------------------------------------------------------------

@patch("db.stock.save_order_items", return_value=2)
@patch("tools.email_parser.try_parse_order")
@patch("tools.gmail.GmailClient")
def test_backfill_order_items_success(mock_gmail_cls, mock_parse, mock_save):
    """Backfill finds order notifications, parses them, and saves to DB."""
    # Gmail returns one order notification
    mock_gmail_instance = MagicMock()
    mock_gmail_cls.return_value = mock_gmail_instance
    mock_gmail_instance.search_order_notifications.return_value = [
        {
            "from": "noreply@shipmecarton.com",
            "subject": "Shipmecarton - Order #23476",
            "body": "Order ID: 23476\nPayment amount: $440\nAmber x 4",
        }
    ]

    # Parser returns a valid classification with order items
    mock_item = MagicMock()
    mock_item.product_name = "Tera Amber made in Armenia"
    mock_item.base_flavor = "Amber"
    mock_item.quantity = 4
    mock_parsed = MagicMock()
    mock_parsed.order_id = "23476"
    mock_parsed.order_items = [mock_item]
    mock_parsed.client_email = "client@example.com"   # must match for safety check
    mock_parse.return_value = mock_parsed

    result = _backfill_order_items("client@example.com")

    assert result == 2
    mock_gmail_instance.search_order_notifications.assert_called_once_with(
        "client@example.com", max_results=30
    )
    mock_parse.assert_called_once()
    # Verify fake_email constructed with Reply-To header
    call_args = mock_parse.call_args[0][0]
    assert "Reply-To: client@example.com" in call_args
    assert "From: noreply@shipmecarton.com" in call_args
    mock_save.assert_called_once_with(
        client_email="client@example.com",
        order_id="23476",
        order_items=[
            {"product_name": "Tera Amber made in Armenia", "base_flavor": "Amber", "quantity": 4}
        ],
    )


@patch("tools.gmail.GmailClient")
def test_backfill_order_items_no_gmail(mock_gmail_cls):
    """Gmail not configured → graceful failure, returns 0."""
    mock_gmail_cls.side_effect = RuntimeError(
        "Gmail not configured. Set GMAIL_CLIENT_ID, ..."
    )

    result = _backfill_order_items("client@example.com")

    assert result == 0


@patch("db.stock.get_client_flavor_history", return_value=[{"base_flavor": "Green", "order_count": 2}])
@patch("tools.gmail.GmailClient")
def test_backfill_skips_when_history_exists(mock_gmail_cls, mock_history):
    """Guard: if ClientOrderItem already exists, skip Gmail scan entirely."""
    result = _backfill_order_items("existing@example.com")

    assert result == 0
    mock_gmail_cls.assert_not_called()


@patch("db.stock.save_order_items")
@patch("tools.email_parser.try_parse_order")
@patch("tools.gmail.GmailClient")
def test_backfill_skips_email_mismatch(mock_gmail_cls, mock_parse, mock_save):
    """Safety check: message whose parsed client_email differs is skipped."""
    mock_gmail_instance = MagicMock()
    mock_gmail_cls.return_value = mock_gmail_instance
    mock_gmail_instance.search_order_notifications.return_value = [
        {"from": "noreply@shipmecarton.com", "subject": "Order #999", "body": "..."},
    ]

    mock_parsed = MagicMock()
    mock_parsed.order_items = [MagicMock()]
    mock_parsed.client_email = "someone_else@example.com"   # mismatch!
    mock_parse.return_value = mock_parsed

    result = _backfill_order_items("target@example.com")

    assert result == 0
    mock_save.assert_not_called()
