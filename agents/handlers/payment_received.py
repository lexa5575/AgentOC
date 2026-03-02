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

    Falls back to general handler if no matching template exists.
    """
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
