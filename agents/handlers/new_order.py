"""
New Order Handler
-----------------

Handles new_order situations:
- Prepay orders → Python template
- Postpay orders → Python template
- Out-of-stock → Python template (stable, 0 LLM tokens)
- Mixed availability → decision_required template (Phase B)
- Optional OOS → normal order + P.S. (Phase C)
- Fallback to general handler if template is missing
"""

import logging

from agents.handlers.general import handle_general
from agents.handlers.template_utils import fill_template_reply, to_gmail_html
from agents.reply_templates import (
    fill_out_of_stock_template,
    fill_mixed_availability_template,
    fill_optional_oos_only_template,
    _build_optional_oos_ps,
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
    # Case 0: All-optional OOS, nothing reservable (Phase C, branch 3)
    if result.get("all_oos_optional"):
        avail = result.get("availability_resolution", {})
        result["draft_reply"] = fill_optional_oos_only_template(
            optional_items=avail.get("optional_unresolved_items", []),
            alternatives_by_flavor=avail.get("alternatives_by_flavor", {}),
        )
        result["template_used"] = True
        result["needs_routing"] = False
        logger.info(
            "All-optional OOS template filled for %s (0 LLM tokens)",
            classification.client_email,
        )
        return result

    # Case 1: Out-of-stock (required items) — use unified OOS template
    # Both full OOS and mixed availability use the same template style:
    # "Unfortunately we ran out of X" + alternatives + website link.
    if result.get("stock_issue"):
        stock_issue = result["stock_issue"]
        insufficient_items = stock_issue["stock_check"]["insufficient_items"]
        best_alternatives = stock_issue.get("best_alternatives", {})
        result["draft_reply"] = fill_out_of_stock_template(
            insufficient_items=insufficient_items,
            best_alternatives=best_alternatives,
        )
        avail = result.get("availability_resolution", {})
        reservable = avail.get("reservable_items", [])
        if reservable:
            logger.info(
                "OOS template (mixed) filled for %s "
                "(reservable=%d, unresolved=%d, 0 LLM tokens)",
                classification.client_email,
                len(reservable),
                len(avail.get("unresolved_items", [])),
            )
        else:
            logger.info(
                "OOS hybrid template filled for %s",
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

        # Phase C: append P.S. for optional OOS items (branch 1)
        if result.get("optional_oos_items"):
            ps = _build_optional_oos_ps(result["optional_oos_items"])
            result["draft_reply"] += f"\n\n{ps}"
            result["draft_reply_html"] = to_gmail_html(
                result["draft_reply"], result.get("order_summary", ""),
            )
            logger.info(
                "Added optional OOS P.S. for %s",
                classification.client_email,
            )

        return result

    logger.warning(
        "No new_order template for client=%s, fallback to general handler",
        classification.client_email,
    )
    return handle_general(classification, result, email_text)
