"""
Email Router
------------

Routes classified emails to appropriate handlers based on situation type.

This is the central routing logic — pure Python, zero LLM tokens.
Each situation gets its own specialized handler with narrow, focused instructions.

Routing:
- new_order → handle_new_order (Python templates + OOS)
- price_question → handle_price_question (deterministic quote + LLM fallback)
- tracking → handle_tracking (specialized agent)
- payment_question → handle_payment (specialized agent)
- discount_request → handle_discount (specialized agent)
- shipping_timeline → handle_shipping (specialized agent)
- payment_received → handle_payment_received (Python template)
- oos_followup → handle_oos_followup (specialized agent)
- stock_question → handle_stock_question (stock lookup + LLM fallback)
- other → handle_general (fallback agent)
"""

import logging
from typing import Callable

from agents.handlers.general import handle_general
from agents.handlers.new_order import handle_new_order
from agents.handlers.oos_followup import handle_oos_followup
from agents.handlers.payment_received import handle_payment_received
from agents.handlers.price_question import handle_price_question
from agents.handlers.tracking import handle_tracking
from agents.handlers.payment import handle_payment
from agents.handlers.discount import handle_discount
from agents.handlers.shipping import handle_shipping
from agents.handlers.stock_question import handle_stock_question

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Situation → Handler Mapping
# ---------------------------------------------------------------------------

SITUATION_HANDLERS: dict[str, Callable] = {
    "new_order": handle_new_order,
    "price_question": handle_price_question,
    "tracking": handle_tracking,
    "payment_question": handle_payment,
    "discount_request": handle_discount,
    "shipping_timeline": handle_shipping,
    "payment_received": handle_payment_received,
    "oos_followup": handle_oos_followup,
    "stock_question": handle_stock_question,
    "other": handle_general,
}


# ---------------------------------------------------------------------------
# Main Router Function
# ---------------------------------------------------------------------------

def route_to_handler(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Route to appropriate handler based on classification.situation.
    
    Args:
        classification: EmailClassification object with situation field
        result: Result dict from process_classified_email
        email_text: Original email text
        
    Returns:
        Updated result dict
    """
    # Allow pipeline to override situation (e.g. Tier 4: unresolved products)
    situation = result.get("situation", classification.situation)

    # Get handler for this situation, default to general
    handler = SITUATION_HANDLERS.get(situation, handle_general)
    
    logger.info(
        "Routing email to handler: situation=%s, handler=%s",
        situation, handler.__name__,
    )
    
    # Call handler
    return handler(classification, result, email_text)
