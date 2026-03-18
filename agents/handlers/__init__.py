"""
Email Handlers
--------------

Specialized handlers for different email situations.
Each handler is responsible for generating a reply for a specific situation type.

Handlers:
- new_order: Orders with prepay/postpay + out-of-stock situations
- tracking: Questions about delivery status and tracking numbers
- payment: Questions about how/where to pay
- payment_received: Payment confirmation acknowledgments
- discount: Requests for discounts or better prices
- shipping: Questions about shipping timelines
- oos_followup: Customer responses to out-of-stock emails
- stock_question: "Do you have X?" availability questions
- general: Fallback for all other situations
"""

from agents.handlers.general import general_agent, handle_general
from agents.handlers.new_order import handle_new_order
from agents.handlers.oos_followup import oos_followup_agent, handle_oos_followup
from agents.handlers.tracking import handle_tracking
from agents.handlers.payment import handle_payment
from agents.handlers.payment_received import handle_payment_received
from agents.handlers.discount import handle_discount
from agents.handlers.shipping import handle_shipping
from agents.handlers.stock_question import handle_stock_question

__all__ = [
    # Agents (only LLM-based handlers still have agents)
    "general_agent",
    "oos_followup_agent",
    # Handler functions
    "handle_new_order",
    "handle_general",
    "handle_oos_followup",
    "handle_tracking",
    "handle_payment",
    "handle_payment_received",
    "handle_discount",
    "handle_shipping",
    "handle_stock_question",
]
