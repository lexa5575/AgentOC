"""
Price Question Handler
----------------------

Handles price_question situations: customer asks "how much would X cost?"

Deterministic flow (0 LLM tokens when items resolve cleanly):
1. Extract items from classification.order_items or conversation_state
2. check_stock_for_order → verify availability
3. calculate_order_price → deterministic price from CATEGORY_PRICES
4. Build quote reply in Python

Fallback to LLM (handle_general) when:
- No items to price (customer asked vaguely)
- Items can't be matched to stock categories (ambiguous)
"""

import logging

from agents.handlers.general import handle_general
from db.memory import (
    check_stock_for_order,
    calculate_order_price,
    get_stock_summary,
    resolve_order_items,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quote reply builder (deterministic, 0 tokens)
# ---------------------------------------------------------------------------

def _build_quote_reply(
    items: list[dict],
    total_price: float,
    oos_items: list[dict] | None = None,
) -> str:
    """Build a deterministic price quote reply.

    Args:
        items: List of in-stock items with product_name and quantity
        total_price: Calculated total price
        oos_items: Optional list of out-of-stock items
    """
    lines = [
        "Hi!",
        "Thank you for your inquiry!",
    ]

    if oos_items:
        # Partial quote — some items OOS
        lines.append(
            "Here is the pricing for the items we currently have in stock:"
        )
        lines.append(f"Total: ${total_price:.2f} (free shipping)")
        lines.append("")
        oos_names = ", ".join(i["base_flavor"] for i in oos_items)
        lines.append(
            f"Unfortunately, the following items are currently out of stock: "
            f"{oos_names}."
        )
        lines.append(
            "Please let us know if you'd like to proceed with the available "
            "items, or if you'd like us to suggest alternatives."
        )
    else:
        # Full quote — all items in stock
        lines.append(f"The total for your order would be ${total_price:.2f} (free shipping).")
        lines.append("Would you like to go ahead and place the order?")

    lines.append("Thank you!")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Item extraction
# ---------------------------------------------------------------------------

def _extract_items(classification, result: dict) -> list[dict] | None:
    """Extract items to price from classification or conversation state.

    Priority:
    1. classification.order_items (classifier extracted from current email)
    2. conversation_state.facts.confirmed_order_items (from previous exchange)
    3. None (no items found)
    """
    # Priority 1: classifier extracted items
    if classification.order_items:
        return [
            {
                "product_name": oi.product_name,
                "base_flavor": oi.base_flavor,
                "quantity": oi.quantity,
            }
            for oi in classification.order_items
        ]

    # Priority 2: confirmed items from state
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    confirmed = facts.get("confirmed_order_items")
    if confirmed:
        return [
            {
                "product_name": item.get("product_name", item["base_flavor"]),
                "base_flavor": item["base_flavor"],
                "quantity": item["quantity"],
            }
            for item in confirmed
        ]

    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle_price_question(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle price_question situations with deterministic pricing.

    Flow:
    - Items found + all in stock + price resolves → template quote (0 tokens)
    - Items found + partial OOS → partial quote + OOS notice (0 tokens)
    - Items found + ambiguous price → price_alert + LLM fallback
    - No items → LLM fallback
    """
    items = _extract_items(classification, result)

    # No items to price → LLM fallback
    if not items:
        logger.info(
            "Price question: no items for %s — LLM fallback",
            classification.client_email,
        )
        return handle_general(classification, result, email_text)

    # Guard: skip if stock table is empty (sync hasn't run yet)
    summary = get_stock_summary()
    if summary["total"] == 0:
        logger.warning(
            "Price question: stock table empty for %s — LLM fallback",
            classification.client_email,
        )
        return handle_general(classification, result, email_text)

    # Resolve misspelled product names (fuzzy matching)
    items, resolve_alerts = resolve_order_items(items)
    if resolve_alerts:
        result["resolve_alerts"] = resolve_alerts
        logger.warning(
            "Price question: product name resolution alerts for %s: %s",
            classification.client_email, resolve_alerts,
        )

    # Check stock availability
    stock_result = check_stock_for_order(items)

    # Separate in-stock and OOS items
    in_stock_items = stock_result["items"]
    oos_items = stock_result["insufficient_items"]

    if not in_stock_items:
        # Everything OOS — can't give any price
        logger.info(
            "Price question: all items OOS for %s — LLM fallback",
            classification.client_email,
        )
        return handle_general(classification, result, email_text)

    # Calculate price for in-stock items
    calculated_price = calculate_order_price(in_stock_items)

    if calculated_price is None:
        # Ambiguous categories — can't determine price reliably
        result["price_alert"] = {
            "type": "unmatched",
            "items": [item["base_flavor"] for item in in_stock_items],
        }
        logger.warning(
            "Price question: ambiguous categories for %s — price_alert + LLM fallback",
            classification.client_email,
        )
        return handle_general(classification, result, email_text)

    # Success: build deterministic quote reply
    result["calculated_price"] = calculated_price
    result["draft_reply"] = _build_quote_reply(
        items=in_stock_items,
        total_price=calculated_price,
        oos_items=oos_items if oos_items else None,
    )
    result["template_used"] = True
    result["needs_routing"] = False

    logger.info(
        "Price question: quote $%.2f for %s (%d items, %d OOS, 0 tokens)",
        calculated_price,
        classification.client_email,
        len(in_stock_items),
        len(oos_items),
    )
    return result
