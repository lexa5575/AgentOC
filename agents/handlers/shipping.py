"""
Shipping Handler — Template-based with keyword guard
-----------------------------------------------------

Handles shipping timeline questions:
- Non-standard questions (expedited, by Friday, etc.) → general handler
- Standard FAQ → prepay/postpay template
"""

import logging
import re

from agents.handlers.template_utils import _get_clean_body, fill_template_reply

logger = logging.getLogger(__name__)

_NONSTANDARD_PATTERN = re.compile(
    r"\b(expedited|express|overnight|rush|"
    r"by\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"today|tomorrow|need it by|asap)\b",
    re.IGNORECASE,
)


def handle_shipping(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle shipping timeline questions with template + keyword guard."""
    from agents.handlers.general import handle_general

    # Guard: non-standard questions → LLM (uses cleaned body)
    clean_body = _get_clean_body(email_text)
    if _NONSTANDARD_PATTERN.search(clean_body):
        logger.info(
            "Non-standard shipping question for %s, routing to general",
            result["client_email"],
        )
        return handle_general(classification, result, email_text)

    result, found = fill_template_reply(
        classification=classification,
        result=result,
        situation="shipping_timeline",
    )
    if found:
        logger.info(
            "Shipping template for %s (0 tokens)",
            result["client_email"],
        )
        return result

    return handle_general(classification, result, email_text)
