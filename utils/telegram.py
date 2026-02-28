"""
Telegram Notifications
----------------------

Send notifications to Telegram via Bot API.
Uses httpx (already installed as dependency of agno/openai).

Usage:
    from utils.telegram import send_telegram
    send_telegram("Hello from AgentOS!")
"""

import logging
from os import getenv

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = getenv("TELEGRAM_CHAT_ID", "")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to Telegram.

    Args:
        message: Text to send (supports HTML formatting).
        parse_mode: "HTML" or "Markdown". Default "HTML".

    Returns:
        True if sent successfully, False otherwise.
        Never raises — notifications must not break the main flow.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing)")
        return False

    try:
        response = httpx.post(
            API_URL.format(token=TELEGRAM_BOT_TOKEN),
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        if response.status_code == 200:
            logger.info("Telegram notification sent")
            return True
        else:
            logger.error("Telegram API error: %s %s", response.status_code, response.text)
            return False
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False
