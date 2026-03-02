"""Tests for agents.client_profiler module."""

from types import SimpleNamespace
from unittest.mock import patch

from agents.client_profiler import generate_client_summary


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
