"""
Gmail Poller
------------

Orchestrator: polls Gmail for new messages, processes each through
the email agent, and sends results to Telegram (human-in-the-loop).

Does NOT send replies to customers — only to the operator via Telegram.

Usage:
    from tools.gmail_poller import poll_gmail
    count = poll_gmail()  # returns number of processed messages
"""

import logging
from datetime import datetime, timezone
from os import getenv

from agents.email_agent import classify_and_process
from db.memory import (
    email_already_processed,
    get_gmail_state,
    set_gmail_state,
)
from tools.gmail import GmailClient
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

_gmail_client: GmailClient | None = None


def _get_client() -> GmailClient:
    """Lazy singleton for GmailClient."""
    global _gmail_client
    if _gmail_client is None:
        _gmail_client = GmailClient()
    return _gmail_client


def _format_email_text(msg: dict) -> str:
    """Format a Gmail message dict into the text format expected by classify_and_process."""
    parts = []

    from_addr = msg.get("from", "")
    from_raw = msg.get("from_raw", from_addr)
    parts.append(f"From: {from_raw}")

    if msg.get("reply_to"):
        parts.append(f"Reply-To: {msg['reply_to']}")

    parts.append(f"Subject: {msg.get('subject', '')}")
    parts.append(f"Body: {msg.get('body', '')}")

    return "\n".join(parts)


def _send_telegram_result(msg: dict, result: str) -> None:
    """Send formatted processing result to Telegram."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Truncate result for Telegram (max 4096 chars)
    if len(result) > 3500:
        result = result[:3500] + "\n... (truncated)"

    # Escape HTML special chars in dynamic content
    subject = (msg.get("subject", "") or "").replace("<", "&lt;").replace(">", "&gt;")
    from_addr = (msg.get("from", "") or "").replace("<", "&lt;").replace(">", "&gt;")
    result_escaped = result.replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"\U0001f4e8 <b>Новое письмо обработано!</b>\n\n"
        f"<b>От:</b> {from_addr}\n"
        f"<b>Тема:</b> {subject}\n\n"
        f"<b>Результат обработки:</b>\n"
        f"<pre>{result_escaped}</pre>\n\n"
        f"\u23f0 {now}"
    )

    send_telegram(text)


def poll_gmail() -> int:
    """Poll Gmail for new messages, process each, send to Telegram.

    Returns number of processed messages.
    """
    # Check if Gmail is configured
    if not getenv("GMAIL_REFRESH_TOKEN", ""):
        logger.debug("Gmail not configured, skipping poll")
        return 0

    client = _get_client()
    processed = 0

    try:
        # Get last known history_id
        history_id = get_gmail_state()

        if not history_id:
            # First run — save current position without processing old emails
            current = client.get_current_history_id()
            set_gmail_state(current)
            logger.info("Gmail poller initialized: history_id=%s", current)
            send_telegram(
                "\u2705 <b>Gmail poller запущен!</b>\n\n"
                "Начинаю отслеживать новые письма."
            )
            return 0

        # Fetch new messages since last check
        new_messages = client.get_new_messages(after_history_id=history_id)

        if not new_messages:
            logger.debug("No new Gmail messages")
            return 0

        logger.info("Found %d new Gmail messages", len(new_messages))

        latest_history_id = history_id

        for msg_info in new_messages:
            msg_id = msg_info["msg_id"]

            # Deduplication check
            if email_already_processed(msg_id):
                logger.debug("Skipping already processed message: %s", msg_id)
                continue

            try:
                # Fetch full message
                msg = client.get_message(msg_id)
                logger.info(
                    "Processing Gmail message: from=%s, subject=%s",
                    msg["from"], msg["subject"],
                )

                # Format for classify_and_process
                email_text = _format_email_text(msg)

                # Process through email agent pipeline
                result = classify_and_process(email_text, gmail_message_id=msg_id)

                # Send result to Telegram
                _send_telegram_result(msg, result)

                processed += 1

            except Exception as e:
                logger.error("Failed to process message %s: %s", msg_id, e, exc_info=True)
                send_telegram(
                    f"\U0001f6a8 <b>Ошибка обработки Gmail!</b>\n\n"
                    f"Message ID: {msg_id}\n"
                    f"Ошибка: {e}"
                )

            # Update history_id after each message
            if msg_info.get("history_id"):
                latest_history_id = msg_info["history_id"]

        # Save latest history_id
        if latest_history_id != history_id:
            set_gmail_state(latest_history_id)

        if processed:
            logger.info("Gmail poll complete: %d messages processed", processed)

    except Exception as e:
        logger.error("Gmail poll failed: %s", e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Gmail poller error!</b>\n\n"
            f"Ошибка: {e}\n\n"
            f"Проверь логи контейнера."
        )

    return processed
