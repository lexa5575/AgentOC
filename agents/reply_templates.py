"""
Reply Templates & Email Processing
------------------------------------

Templates, classification model, and formatting for the Email Agent.
All database operations go through db.memory.
"""

import logging
from typing import Optional

from pydantic import BaseModel, Field

from db.memory import decrement_discount, get_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic model for structured classification (LLM must return this)
# ---------------------------------------------------------------------------
class EmailClassification(BaseModel):
    """Structured classification of an incoming email."""

    needs_reply: bool = Field(description="Whether this email requires a reply")
    situation: str = Field(description=(
        "One of: new_order, tracking, payment_question, "
        "payment_received, discount_request, shipping_timeline, other"
    ))
    client_email: str = Field(description="The REAL client email (not system email)")
    client_name: Optional[str] = Field(default=None, description="Client full name")
    order_id: Optional[str] = Field(default=None, description="Order number")
    price: Optional[str] = Field(default=None, description="Total amount e.g. $220.00")
    customer_street: Optional[str] = Field(default=None, description="Street address")
    customer_city_state_zip: Optional[str] = Field(
        default=None, description="City, State Zip on one line"
    )
    items: Optional[str] = Field(default=None, description="What was ordered")


# ---------------------------------------------------------------------------
# Reply Templates (hardcoded — never change)
# Key format: (situation, payment_type)
# ---------------------------------------------------------------------------
REPLY_TEMPLATES = {
    ("new_order", "prepay"): (
        "Thank you so much for placing an order\n"
        "Your total is {PRICE} - {DISCOUNT}% = {FINAL_PRICE} FREE shipping\n"
        "\n"
        "!!! Zelle ( In memo or comments don't put anything please ! ) use email below\n"
        "\n"
        "{ZELLE_ADDRESS}\n"
        "\n"
        "If paid today, We will ship your order Tonight from USA\n"
        "Your order will be delivered in 2-4 days max.\n"
        "Thank you!"
    ),
    ("new_order", "postpay"): (
        "Hello!\n"
        "Thank you very much for placing an order\n"
        "We will ship your package ASAP\n"
        "Total is {PRICE} - {DISCOUNT}% = {FINAL_PRICE} FREE shipping applied\n"
        "Pay when received as always via Zelle or Cash App\n"
        "ZELLE IS OUR PREFERRED METHOD OF PAYMENT\n"
        "When order is received and you are ready to pay "
        "( In memo or comments don't put anything please ! )\n"
        "\n"
        "Here is your confirmation.\n"
        "Tracking With USPS will be updated on the USPS website "
        "till midnight on the day of the shipping\n"
        "{TRACKING_URL}\n"
        "\n"
        "{CUSTOMER_NAME}\n"
        "{CUSTOMER_STREET}\n"
        "{CUSTOMER_CITY_STATE_ZIP}"
    ),
}


# ---------------------------------------------------------------------------
# Format email history for LLM prompt
# ---------------------------------------------------------------------------
def format_email_history(history: list[dict]) -> str:
    """Format email history for inclusion in the fallback LLM prompt."""
    if not history:
        return ""

    lines = ["=== CONVERSATION HISTORY ===", ""]
    for msg in history:
        ts = msg["created_at"].strftime("%Y-%m-%d") if msg.get("created_at") else "unknown"
        if msg["direction"] == "inbound":
            prefix = f"[CLIENT WROTE] {ts} | {msg.get('subject', '')}"
        else:
            prefix = f"[WE SENT] {ts} | {msg.get('subject', '')}"

        body = msg.get("body", "")
        if len(body) > 300:
            body = body[:300] + "..."

        lines.append(prefix)
        lines.append(body)
        lines.append("---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core processing function (pure Python, no LLM, no tokens)
# ---------------------------------------------------------------------------
def process_classified_email(classification: EmailClassification) -> dict:
    """Process a classified email: look up client, fill template or request fallback.

    This function uses ZERO tokens — it's pure Python.
    Returns a dict with all info needed to display or send the reply.
    """
    result = {
        "needs_reply": classification.needs_reply,
        "situation": classification.situation,
        "client_email": classification.client_email,
        "client_name": classification.client_name,
        "client_found": False,
        "client_data": None,
        "template_used": False,
        "draft_reply": None,
        "needs_ai_fallback": False,
    }

    # No reply needed — stop here
    if not classification.needs_reply:
        result["draft_reply"] = "(No reply needed)"
        return result

    # Look up client via memory layer
    client = get_client(classification.client_email)
    if client:
        result["client_found"] = True
        result["client_data"] = client
    else:
        result["client_data"] = {"payment_type": "unknown", "name": "unknown"}
        result["needs_ai_fallback"] = True
        return result

    # Try to find a template
    payment_type = client["payment_type"]
    template = REPLY_TEMPLATES.get((classification.situation, payment_type))

    if not template:
        # No template for this situation — need AI fallback
        result["needs_ai_fallback"] = True
        return result

    # Fill the template (pure Python, exact output)
    price = classification.price or ""
    discount = client.get("discount_percent", 0)
    discount_left = client.get("discount_orders_left", 0)
    zelle_address = client.get("zelle_address", "")

    # Parse price
    price_clean = price.replace("$", "").replace(",", "")
    try:
        price_num = float(price_clean)
    except (ValueError, TypeError):
        price_num = 0.0

    # Apply discount
    apply_discount = discount > 0 and discount_left > 0 and price_num > 0
    if apply_discount:
        final_price = f"${price_num * (1 - discount / 100):.2f}"
        discount_str = str(discount)
    else:
        final_price = price
        discount_str = "0"

    # Fill placeholders
    reply = template
    reply = reply.replace("{PRICE}", price)
    reply = reply.replace("{DISCOUNT}", discount_str)
    reply = reply.replace("{FINAL_PRICE}", final_price)
    reply = reply.replace("{ZELLE_ADDRESS}", zelle_address)
    reply = reply.replace("{CUSTOMER_NAME}", classification.client_name or client["name"])
    reply = reply.replace("{CUSTOMER_STREET}", classification.customer_street or "")
    reply = reply.replace("{CUSTOMER_CITY_STATE_ZIP}", classification.customer_city_state_zip or "")
    reply = reply.replace("{TRACKING_URL}", "[tracking URL pending]")

    # Clean up: if no discount, simplify the price line
    if not apply_discount and price:
        reply = reply.replace(f"{price} - 0% = {price}", price)

    # Decrement discount_orders_left via memory layer
    if apply_discount:
        decrement_discount(classification.client_email)
        logger.info(
            "Discount applied for %s: %s%% (%d -> %d orders left)",
            classification.client_email, discount, discount_left, discount_left - 1,
        )

    result["template_used"] = True
    result["draft_reply"] = reply
    return result


def format_result(result: dict) -> str:
    """Format the processing result for display."""
    lines = []
    lines.append("=" * 50)
    lines.append("CLASSIFICATION")
    lines.append("=" * 50)
    lines.append(f"Needs Reply: {result['needs_reply']}")
    lines.append(f"Situation: {result['situation']}")
    lines.append(f"Client Email: {result['client_email']}")
    lines.append(f"Client Name: {result['client_name']}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("CLIENT DATA")
    lines.append("=" * 50)
    if result["client_found"]:
        c = result["client_data"]
        lines.append(f"Status: FOUND")
        lines.append(f"Payment Type: {c['payment_type']}")
        if c.get("zelle_address"):
            lines.append(f"Zelle: {c['zelle_address']}")
        d = c.get("discount_percent", 0)
        dl = c.get("discount_orders_left", 0)
        if d > 0 and dl > 0:
            lines.append(f"Discount: {d}% ({dl} orders left)")
        else:
            lines.append("Discount: none")
    else:
        lines.append("Status: NEW CLIENT (not in database)")
    lines.append("")

    lines.append("=" * 50)
    lines.append("DRAFT REPLY")
    lines.append("=" * 50)
    if result["template_used"]:
        lines.append("[Template - exact copy]")
        lines.append("")
        lines.append(result["draft_reply"])
    elif result["needs_ai_fallback"]:
        lines.append("[AI will generate reply]")
    else:
        lines.append(result["draft_reply"])

    return "\n".join(lines)
