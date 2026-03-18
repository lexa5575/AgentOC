"""
Tracking Handler — Template-based
----------------------------------

Handles tracking-related questions with factual branching:
- Branch 1: tracking_number known → return it
- Branch 2: shipped but no tracking → pending template with recheck date
- Branch 3: not shipped / no data → fallback to general handler (LLM)
"""

import logging

from agents.handlers.template_utils import (
    _calc_recheck_date,
    fill_template_reply,
)

logger = logging.getLogger(__name__)


def handle_tracking(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle tracking questions with factual branching."""
    from agents.handlers.general import handle_general

    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    tracking_number = facts.get("tracking_number")
    status = state.get("status")

    # Branch 1: tracking_number known → give it directly
    if tracking_number:
        result["draft_reply"] = (
            f"Your tracking number is {tracking_number}.\n"
            f"You can track it at usps.com\n"
            f"Thank you!"
        )
        result["template_used"] = True
        result["needs_routing"] = False
        logger.info(
            "Tracking template (with number) for %s (0 tokens)",
            result["client_email"],
        )
        return result

    # Branch 2: shipped but no tracking yet → pending template
    shipped = status == "shipped" or facts.get("shipped_at")
    if shipped:
        recheck = _calc_recheck_date(
            result.get("gmail_thread_id"),
            facts=facts,
            payment_type=(result.get("client_data") or {}).get("payment_type"),
        )
        if recheck:
            result, found = fill_template_reply(
                classification=classification,
                result=result,
                situation="tracking",
            )
            if found:
                logger.info(
                    "Tracking pending template for %s (0 tokens, recheck=%s)",
                    result["client_email"], recheck,
                )
                return result

    # Branch 3: not shipped, no recheck date, or no data → general
    logger.info(
        "Tracking fallback to general for %s (status=%s, shipped_at=%s)",
        result["client_email"], status, facts.get("shipped_at"),
    )
    return handle_general(classification, result, email_text)
