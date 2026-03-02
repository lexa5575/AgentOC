"""
Reply Templates & Email Processing
------------------------------------

Templates, classification model, and formatting for the Email Agent.
All database operations go through db.memory.
"""

import logging
from typing import Optional

from pydantic import BaseModel, Field

from db.memory import (
    check_stock_for_order,
    get_client,
    get_stock_summary,
    select_best_alternatives,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models for structured classification (LLM must return this)
# ---------------------------------------------------------------------------
class OrderItem(BaseModel):
    """Single item extracted from an order notification."""

    product_name: str = Field(description="Full product name as on order, e.g. 'Tera Green made in Middle East'")
    base_flavor: str = Field(description="Base flavor/color only, e.g. 'Green', 'Turquoise', 'Silver'")
    quantity: int = Field(default=1, description="Number of units ordered")


class EmailClassification(BaseModel):
    """Structured classification of an incoming email."""

    needs_reply: bool = Field(description="Whether this email requires a reply")
    situation: str = Field(description=(
        "One of: new_order, tracking, payment_question, "
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
    ("payment_received", "prepay"): (
        "Hello {CUSTOMER_NAME}\n"
        "How are you?\n"
        "Thank you very much for a prompt payment!\n"
        "Nice doing business with you!!!\n"
        "\n"
        "We will ship your order today!\n"
        "Here is the USPS tracking number:\n"
        "{TRACKING_URL}\n"
        "\n"
        "{CUSTOMER_NAME}\n"
        "{CUSTOMER_STREET}\n"
        "{CUSTOMER_CITY_STATE_ZIP}"
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
# Out-of-Stock Template (STABLE — Python fills variables, no LLM)
# ---------------------------------------------------------------------------
def _format_alternative(alt_entry: dict) -> str:
    """Format a single alternative based on its reason.
    
    Args:
        alt_entry: Dict with keys: alternative (stock item dict), reason, order_count
    
    Returns:
        Formatted string like "Turquoise from Armenia" or "Amber (you've ordered before)"
    """
    alt = alt_entry["alternative"]
    reason = alt_entry.get("reason", "fallback")
    product_name = alt["product_name"]
    category = alt.get("category", "")
    
    # Map category to readable region
    region_map = {
        "ARMENIA": "Armenia",
        "KZ_TEREA": "Kazakhstan",
        "TEREA_JAPAN": "Japan",
        "TEREA_EUROPE": "Europe",
        "УНИКАЛЬНАЯ_ТЕРЕА": "Unique collection",
        "ONE": "ONE",
        "STND": "STND",
        "PRIME": "PRIME",
    }
    region = region_map.get(category, category)
    
    if reason == "same_flavor":
        return f"{product_name} from {region}"
    elif reason == "history":
        return f"{product_name} (you've ordered before)"
    else:  # fallback
        return product_name


def fill_out_of_stock_template(
    insufficient_items: list[dict],
    best_alternatives: dict,
) -> str:
    """Fill the Out-of-Stock template with actual data. Zero LLM tokens.
    
    Handles all situations:
    - Full OOS (qty = 0)
    - Partial OOS (qty > 0 but < ordered)
    - Mixed (some full, some partial)
    - No alternatives available
    
    Args:
        insufficient_items: List of items with insufficient stock, each has:
            - base_flavor, ordered_qty, total_available, product_name
        best_alternatives: Dict mapping base_flavor -> {alternatives: [...], reason, ...}
    
    Returns:
        Complete email reply text, ready to send.
    """
    # Step 1: Classify items into full OOS vs partial OOS
    full_oos = []       # total_available == 0
    partial_oos = []    # total_available > 0 but < ordered
    
    for item in insufficient_items:
        if item["total_available"] == 0:
            full_oos.append(item)
        else:
            partial_oos.append(item)
    
    # Step 2: Build the problem description
    problem_parts = []
    
    if full_oos:
        if len(full_oos) == 1:
            problem_parts.append(f"we just ran out of {full_oos[0]['base_flavor']}")
        else:
            flavors = ", ".join([i["base_flavor"] for i in full_oos[:-1]])
            flavors += f" and {full_oos[-1]['base_flavor']}"
            problem_parts.append(f"we just ran out of {flavors}")
    
    if partial_oos:
        for p in partial_oos:
            problem_parts.append(
                f"we only have {p['total_available']} {p['base_flavor']} available "
                f"(you ordered {p['ordered_qty']})"
            )
    
    # Combine problem parts
    if len(problem_parts) == 1:
        problem_text = problem_parts[0]
    elif len(problem_parts) == 2:
        problem_text = f"{problem_parts[0]}, and {problem_parts[1]}"
    else:
        problem_text = ", ".join(problem_parts[:-1]) + f", and {problem_parts[-1]}"
    
    # Step 3: Build alternatives section
    has_alternatives = False
    alt_lines = []
    
    for item in insufficient_items:
        flavor = item["base_flavor"]
        decision = best_alternatives.get(flavor, {})
        alts = decision.get("alternatives", [])
        
        if alts:
            has_alternatives = True
            # Format up to 3 alternatives
            formatted_alts = [_format_alternative(a) for a in alts[:3]]
            
            if len(insufficient_items) == 1:
                # Single flavor — no need to specify "For X:"
                alt_lines.append(", ".join(formatted_alts))
            else:
                # Multiple flavors — specify which flavor
                alt_lines.append(f"For {flavor}: {', '.join(formatted_alts)}")
    
    # Step 4: Build the final email
    lines = [
        "Hi!",
        "How are you?",
        f"Unfortunately, {problem_text}",
        "",
        "What can we offer? Please choose one of the options below.",
    ]
    
    if has_alternatives:
        if len(alt_lines) == 1:
            lines.append(f"1. We have {alt_lines[0]}")
        else:
            lines.append("1. We have alternatives:")
            for alt_line in alt_lines:
                lines.append(f"   {alt_line}")
        lines.append("2. Check our website for substitutions and ready to ship sticks.")
    else:
        # No alternatives — only website option
        lines.append("1. Check our website for substitutions and ready to ship sticks.")
    
    lines.extend([
        "",
        "Link for the sticks substitution",
        "https://shipmecarton.com",
        "",
        "Please let us know what you think",
    ])
    
    return "\n".join(lines)


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


def format_thread_for_classifier(history: list[dict]) -> str:
    """Format thread history for the Classifier prompt — full body, no truncation.

    Classifier accuracy depends on seeing the complete conversation context.
    """
    if not history:
        return ""

    lines = ["--- THREAD HISTORY ---"]
    for msg in history:
        ts = msg["created_at"].strftime("%Y-%m-%d") if msg.get("created_at") else "unknown"
        if msg["direction"] == "inbound":
            prefix = f"[CLIENT] {ts} | {msg.get('subject', '')}"
        else:
            prefix = f"[WE SENT] {ts} | {msg.get('subject', '')}"

        lines.append(prefix)
        lines.append(msg.get("body", ""))
        lines.append("---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core processing function (pure Python, no LLM, no tokens)
# ---------------------------------------------------------------------------
def process_classified_email(classification: EmailClassification) -> dict:
    """Process a classified email: classify metadata and prepare router context.

    This function uses ZERO tokens and does not generate text replies.
    It prepares client/stock context and signals whether routing is needed.
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
        "needs_routing": False,
        "stock_issue": None,
    }

    # Look up client via memory layer (always — even if no reply needed)
    client = get_client(classification.client_email)
    if client:
        result["client_found"] = True
        result["client_data"] = client

    # No reply needed — stop here
    if not classification.needs_reply:
        result["draft_reply"] = "(No reply needed)"
        if not client:
            result["client_data"] = {"payment_type": "unknown", "name": "unknown"}
        return result

    # Client not found — can't generate auto-reply
    if not client:
        result["client_data"] = {"payment_type": "unknown", "name": "unknown"}
        result["draft_reply"] = "(Клиент не в базе — авто-ответ не генерируется)"
        return result

    # Stock check for new_order with structured items
    if (
        classification.situation == "new_order"
        and classification.order_items
    ):
        # Guard: skip if stock table is empty (sync hasn't run yet)
        summary = get_stock_summary()
        if summary["total"] > 0:
            items_for_check = [
                {
                    "product_name": oi.product_name,
                    "base_flavor": oi.base_flavor,
                    "quantity": oi.quantity,
                }
                for oi in classification.order_items
            ]
            stock_result = check_stock_for_order(items_for_check)

            if not stock_result["all_in_stock"]:
                # Select up to three alternatives per insufficient item
                best_alternatives = {}
                for insuff in stock_result["insufficient_items"]:
                    best = select_best_alternatives(
                        client_email=classification.client_email,
                        base_flavor=insuff["base_flavor"],
                        max_options=3,
                    )
                    best_alternatives[insuff["base_flavor"]] = best

                result["stock_issue"] = {
                    "stock_check": stock_result,
                    "best_alternatives": best_alternatives,
                }
                result["needs_routing"] = True
                logger.info(
                    "Stock insufficient for %s: %s (alternatives: %s)",
                    classification.client_email,
                    [i["base_flavor"] for i in stock_result["insufficient_items"]],
                    {k: v.get("reason", "none_available") for k, v in best_alternatives.items()},
                )
                return result

    # All reply generation is delegated to specialized handlers via router.
    result["needs_routing"] = True
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

    # Stock check section (if applicable)
    if result.get("stock_issue"):
        lines.append("=" * 50)
        lines.append("STOCK CHECK")
        lines.append("=" * 50)
        stock_check = result["stock_issue"]["stock_check"]
        for item in stock_check["items"]:
            status = "OK" if item["is_sufficient"] else "INSUFFICIENT"
            shortage = ""
            if not item["is_sufficient"] and item["total_available"] > 0:
                shortage = " [PARTIAL]"
            lines.append(
                f"{item['base_flavor']}: ordered {item['ordered_qty']}, "
                f"available {item['total_available']} [{status}]{shortage}"
            )

        # Show alternative decision for each OOS flavor
        best_alts = result["stock_issue"].get("best_alternatives", {})
        if best_alts:
            lines.append("")
            lines.append("ALTERNATIVE DECISION:")
            for flavor, decision in best_alts.items():
                alts = decision.get("alternatives", [])
                if not alts:
                    lines.append(f"  {flavor} → no alternative available")
                    continue

                rendered = []
                for opt in alts:
                    alt = opt["alternative"]
                    reason = opt.get("reason", "fallback")
                    reason_text = reason
                    if reason == "history" and opt.get("order_count"):
                        reason_text = f"history ({opt['order_count']}x ordered before)"
                    rendered.append(
                        f"{alt['category']} / {alt['product_name']} (qty: {alt['quantity']}) [{reason_text}]"
                    )
                lines.append(f"  {flavor} → " + " | ".join(rendered))
        lines.append("")

    lines.append("=" * 50)
    lines.append("DRAFT REPLY")
    lines.append("=" * 50)
    if result["template_used"]:
        lines.append("[Template - exact copy]")
        lines.append("")
        lines.append(result["draft_reply"])
    elif result.get("needs_routing"):
        lines.append("[Router will generate reply]")
    else:
        lines.append(result["draft_reply"])

    return "\n".join(lines)
