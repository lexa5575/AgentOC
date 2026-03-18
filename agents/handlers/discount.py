"""
Discount Handler — Template-based
----------------------------------

Handles discount requests:
- Mixed-intent guard (total, bulk, custom) → general
- Has discount → show percent and orders left
- No discount → polite decline with promotions mention
"""

import logging

from agents.handlers.template_utils import _get_clean_body, fill_template_reply

logger = logging.getLogger(__name__)

_DISCOUNT_MIXED_KEYWORDS = {
    "total", "how much", "price", "remind me",
    "bulk", "custom", "negotiate",
}


def handle_discount(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle discount requests with template + mixed-intent guard."""
    from agents.handlers.general import handle_general

    # Guard: mixed-intent or bulk/custom negotiation → general
    clean_body = _get_clean_body(email_text)
    if any(kw in clean_body for kw in _DISCOUNT_MIXED_KEYWORDS):
        logger.info(
            "Discount mixed-intent for %s, routing to general",
            result["client_email"],
        )
        return handle_general(classification, result, email_text)

    client = result.get("client_data") or {}
    has_discount = (
        client.get("discount_percent", 0) > 0
        and client.get("discount_orders_left", 0) > 0
    )
    discount_key = "has_discount" if has_discount else "no_discount"

    result, found = fill_template_reply(
        classification=classification,
        result=result,
        situation="discount_request",
        override_payment_type=discount_key,
    )
    if found:
        logger.info(
            "Discount template (%s) for %s (0 tokens)",
            discount_key, result["client_email"],
        )
        return result

    return handle_general(classification, result, email_text)
