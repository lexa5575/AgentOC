"""
Payment Handler — Template-based with allow-list guard
-------------------------------------------------------

Handles "how do I pay?" questions with Zelle/payment templates.
Only uses template for simple payment-method questions.
Complex questions (amount, address issues, Cash App) → general handler.
"""

import logging
import re

from agents.handlers.template_utils import _get_clean_body, fill_template_reply

logger = logging.getLogger(__name__)

# Allow-list: template ONLY for simple "how to pay" questions
_SIMPLE_PAYMENT_PATTERN = re.compile(
    r"\b(how (can|do|should) i pay|payment method|"
    r"same zelle (account|address|email)( as before)?|"
    r"where (do|should) i (send|pay)|send payment|"
    r"(can|should) i (pay|use) zelle)\b",
    re.IGNORECASE,
)

# Reject-list: complexity signals that push to general even if allow-list matched
_PAYMENT_REJECT_KEYWORDS = {
    "total", "how much", "amount", "price", "old email",
    "new email", "doesn't work", "wrong", "cash app",
    "also", "and what", "remind me",
}


def handle_payment(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle payment questions with template + allow-list guard."""
    from agents.handlers.general import handle_general

    clean_body = _get_clean_body(email_text)

    # Template only if: matches simple pattern AND no complexity signals AND short
    use_template = (
        _SIMPLE_PAYMENT_PATTERN.search(clean_body)
        and not any(kw in clean_body for kw in _PAYMENT_REJECT_KEYWORDS)
        and clean_body.count("?") <= 1
        and len(clean_body.split()) < 30
    )

    if use_template:
        result, found = fill_template_reply(
            classification=classification,
            result=result,
            situation="payment_question",
        )
        if found:
            logger.info(
                "Payment template for %s (0 tokens)",
                result["client_email"],
            )
            return result

    # Everything else → general
    logger.info(
        "Payment fallback to general for %s",
        result["client_email"],
    )
    return handle_general(classification, result, email_text)
