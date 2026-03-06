"""Payment Received Handler."""

import logging

from agents.handlers.general import handle_general
from agents.handlers.template_utils import fill_template_reply

logger = logging.getLogger(__name__)


def handle_payment_received(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle payment_received with Python template.

    For prepay clients with pending OOS resolution: the client may not have
    received proper Zelle payment instructions yet (e.g. OOS handler fell
    through to LLM instead of template). In this case, send oos_agrees
    template (with Zelle address) instead of tracking.

    Falls back to general handler if no matching template exists.
    """
    # Guard: prepay client with unresolved OOS — send Zelle address, not tracking
    if result.get("client_found"):
        client = result["client_data"]
        state = result.get("conversation_state") or {}
        facts = state.get("facts") or {}

        if (
            client.get("payment_type") == "prepay"
            and facts.get("pending_oos_resolution")
        ):
            result, template_found = fill_template_reply(
                classification=classification,
                result=result,
                situation="oos_agrees",
            )
            if template_found:
                logger.info(
                    "Payment received but pending OOS for prepay %s "
                    "— sent oos_agrees template with Zelle address (0 tokens)",
                    classification.client_email,
                )
                return result

    result, template_found = fill_template_reply(
        classification=classification,
        result=result,
        situation="payment_received",
    )
    if template_found:
        logger.info(
            "Payment received template for %s (0 LLM tokens)",
            classification.client_email,
        )
        return result

    logger.warning(
        "No payment_received template for client=%s, fallback to general handler",
        classification.client_email,
    )
    return handle_general(classification, result, email_text)
