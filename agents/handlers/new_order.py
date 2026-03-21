"""
New Order Handler
-----------------

Handles new_order situations:
- Prepay orders → Python template
- Postpay orders → Python template
- Out-of-stock → Python template (stable, 0 LLM tokens)
- Fallback to general handler if template is missing
"""

import logging

from agents.handlers.general import handle_general
from agents.handlers.template_utils import fill_template_reply
from agents.reply_templates import (
    fill_out_of_stock_template,
    fill_mixed_availability_template,
)

logger = logging.getLogger(__name__)


def handle_new_order(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle new_order situations with Python templates.

    Args:
        classification: EmailClassification object
        result: Result dict from process_classified_email
        email_text: Original email text (not used for templates)

    Returns:
        Updated result dict with draft_reply filled
    """
    # Case 1: Out-of-stock — use stable Python template
    if result.get("stock_issue"):
        avail = result.get("availability_resolution", {})
        reservable = avail.get("reservable_items", [])

        if reservable:
            # Phase B: mixed availability — decision_required, NOT fulfillment
            result["draft_reply"] = fill_mixed_availability_template(
                reservable_items=reservable,
                unresolved_items=avail.get("unresolved_items", []),
                alternatives_by_flavor=avail.get("alternatives_by_flavor", {}),
                reservable_price=avail.get("reservable_price"),
                client_data=result.get("client_data"),
            )
            logger.info(
                "Mixed availability template filled for %s "
                "(reservable=%d, unresolved=%d, 0 LLM tokens)",
                classification.client_email,
                len(reservable),
                len(avail.get("unresolved_items", [])),
            )
        else:
            # Full OOS: all items unavailable
            stock_issue = result["stock_issue"]
            insufficient_items = stock_issue["stock_check"]["insufficient_items"]
            best_alternatives = stock_issue.get("best_alternatives", {})
            result["draft_reply"] = fill_out_of_stock_template(
                insufficient_items=insufficient_items,
                best_alternatives=best_alternatives,
            )
            logger.info(
                "OOS template filled for %s (0 LLM tokens)",
                classification.client_email,
            )

        result["template_used"] = True
        result["needs_routing"] = False
        return result
    
    # Case 2: Normal order — use prepay/postpay template
    result, template_found = fill_template_reply(
        classification=classification,
        result=result,
        situation="new_order",
    )
    if template_found:
        payment_type = result["client_data"].get("payment_type", "unknown")
        logger.info(
            "New order template filled for %s (payment_type=%s, 0 LLM tokens)",
            classification.client_email,
            payment_type,
        )
        return result

    logger.warning(
        "No new_order template for client=%s, fallback to general handler",
        classification.client_email,
    )
    return handle_general(classification, result, email_text)
