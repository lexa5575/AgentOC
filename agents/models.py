"""
Data models for email classification.

Moved from agents/reply_templates.py (Phase 1 refactor).
"""

from typing import Optional

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    """Single item extracted from an order notification."""

    product_name: str = Field(description="Full product name as on order, e.g. 'Tera Green made in Middle East'")
    base_flavor: str = Field(description="Base flavor/color only, e.g. 'Green', 'Turquoise', 'Silver'")
    quantity: int = Field(default=1, description="Number of units ordered")


class EmailClassification(BaseModel):
    """Structured classification of an incoming email."""

    needs_reply: bool = Field(description="Whether this email requires a reply")
    situation: str = Field(description=(
        "One of: new_order, price_question, tracking, payment_question, "
        "payment_received, discount_request, shipping_timeline, oos_followup, other"
    ))
    client_email: str = Field(description="The REAL client email (not system email)")
    client_name: Optional[str] = Field(default=None, description="Client full name")
    order_id: Optional[str] = Field(default=None, description="Order number")
    price: Optional[str] = Field(default=None, description="Total amount e.g. $220.00")
    customer_street: Optional[str] = Field(default=None, description="Street address")
    customer_city_state_zip: Optional[str] = Field(
        default=None, description="City, State Zip on one line"
    )
    items: Optional[str] = Field(default=None, description="What was ordered (free text)")
    order_items: Optional[list[OrderItem]] = Field(
        default=None, description="Structured list of ordered items with base flavor and quantity"
    )
    # Followup detection fields (Phase 5)
    is_followup: bool = Field(default=False, description="Whether this is a response to our previous message")
    followup_to: Optional[str] = Field(default=None, description="What type of message they're responding to (e.g. 'oos_email', 'payment_info')")
    dialog_intent: Optional[str] = Field(default=None, description="Customer intent (e.g. 'agrees_to_alternative', 'declines_alternative')")
    parser_used: bool = Field(default=False, description="True if parsed by regex (website order), False if by LLM")
