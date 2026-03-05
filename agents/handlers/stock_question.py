"""
Stock Question Handler
----------------------

Handles questions about product availability:
- "Do you have Tropical?"
- "Is Silver in stock?"
- "Do you carry Blue?"

Flow:
1. Extract the asked-about flavor from order_items (classifier) or conversation state
2. search_stock(flavor) → real stock data
3. If in stock  → deterministic reply with price (0 LLM tokens)
4. If not       → select_best_alternatives() + LLM reply with alternatives
"""

import logging

from db.stock import (
    CATEGORY_PRICES,
    search_stock,
    search_stock_by_ids,
    select_best_alternatives,
    get_product_type,
)
from db.catalog import get_base_display_name
from db.product_resolver import resolve_product_to_catalog
from agents.context import build_context, format_context_for_prompt
from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent (used only when product is NOT in stock — for alternatives reply)
# ---------------------------------------------------------------------------

_oos_instructions = """\
You are James, answering a product availability question for shipmecarton.com.

## Style rules (STRICT)
- Write like a casual text message: short, warm, no formality
- 2-4 sentences MAX. Never write a long paragraph.
- Start with "Hi {name}," if name is known, otherwise start directly
- Always end with exactly "Thank you!" — nothing after it
- No bullet points, no bold, no lists

## Content rules
- ALWAYS use the STOCK INFO section — never make up availability
- The product is NOT available — say so clearly but warmly
- Mention the alternatives by name (from STOCK INFO) and their price
- Ask if any of those alternatives work for the customer
- Never invent product names or prices not listed in STOCK INFO
"""

_oos_agent = Agent(
    id="stock-question-oos",
    name="Stock Question OOS Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=_oos_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_flavor(classification, result: dict) -> str | None:
    """Extract the product being asked about.

    Priority:
    1. classification.order_items (classifier parsed the email)
    2. conversation_state facts → confirmed/pending order_items
    """
    # 1. Classifier extracted items
    order_items = getattr(classification, "order_items", None) or []
    if order_items:
        item = order_items[0]
        return getattr(item, "base_flavor", None) or getattr(item, "product_name", None)

    # 2. Conversation state
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    for key in ("confirmed_order_items", "pending_order_items", "order_items"):
        items = facts.get(key) or []
        if items:
            first = items[0]
            if isinstance(first, str):
                return first
            return first.get("base_flavor") or first.get("product_name")

    return None


def _price_for_items(stock_items: list[dict]) -> float | None:
    """Return per-box price for a set of stock items, or None if ambiguous."""
    categories = {it["category"] for it in stock_items}
    prices = {CATEGORY_PRICES[c] for c in categories if c in CATEGORY_PRICES}
    if len(prices) == 1:
        return prices.pop()
    return None


def _build_in_stock_reply(
    client_name: str | None,
    flavor: str,
    stock_items: list[dict],
    price: float | None,
) -> str:
    """Deterministic reply when product IS in stock (0 LLM tokens)."""
    greeting = f"Hi {client_name}," if client_name else "Hi,"
    price_str = f" It's ${price:.0f} per box." if price is not None else ""
    return (
        f"{greeting} yes, we have {flavor} in stock!{price_str} "
        f"Let us know how many boxes you'd like and we'll get it ready for you. "
        f"Thank you!"
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle_stock_question(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle product availability questions.

    Returns deterministic reply (0 tokens) when product is in stock.
    Falls back to LLM reply with alternatives when product is OOS.
    """
    flavor = _extract_flavor(classification, result)

    if not flavor:
        # Can't determine what was asked — fall back to general handler
        logger.warning(
            "Stock question: could not extract flavor for %s — general fallback",
            result["client_email"],
        )
        from agents.handlers.general import handle_general
        return handle_general(classification, result, email_text)

    # Resolve via catalog for exact lookup, fallback to substring search
    catalog_result = resolve_product_to_catalog(flavor)
    display_name = catalog_result.display_name or get_base_display_name(flavor)

    if catalog_result.product_ids:
        stock_items = search_stock_by_ids(catalog_result.product_ids)
    else:
        stock_items = search_stock(flavor)
    available = [it for it in stock_items if it["quantity"] > 0]

    client_name = result.get("client_name") or (
        result.get("client_data") or {}
    ).get("name")

    # -----------------------------------------------------------------------
    # Case 1: Product is in stock → deterministic reply
    # -----------------------------------------------------------------------
    if available:
        price = _price_for_items(available)
        result["draft_reply"] = _build_in_stock_reply(client_name, display_name, available, price)
        result["template_used"] = True
        result["needs_routing"] = False

        logger.info(
            "Stock question: %s IN STOCK for %s, price=%s (0 tokens)",
            flavor, result["client_email"], price,
        )
        return result

    # -----------------------------------------------------------------------
    # Case 2: Product not in stock → suggest alternatives via LLM
    # -----------------------------------------------------------------------
    logger.info(
        "Stock question: %s OOS for %s — fetching alternatives",
        flavor, result["client_email"],
    )

    client_summary = (result.get("client_data") or {}).get("llm_summary", "")
    alts_result = select_best_alternatives(
        client_email=result["client_email"],
        base_flavor=flavor,
        client_summary=client_summary,
    )
    alternatives = alts_result.get("alternatives", [])

    # Format alternatives + prices for the LLM prompt
    if alternatives:
        alt_lines = []
        for a in alternatives:
            item = a["alternative"]
            p = _price_for_items([item])
            price_str = f" (${p:.0f}/box)" if p is not None else ""
            alt_lines.append(f"- {item['product_name']}{price_str}")
        alt_block = "\n".join(alt_lines)
        stock_info = (
            f"STOCK INFO:\n"
            f"{flavor} is NOT available.\n\n"
            f"Available alternatives:\n{alt_block}"
        )
    else:
        stock_info = f"STOCK INFO:\n{flavor} is NOT available. No similar alternatives currently in stock."

    ctx = build_context(classification, result, email_text)
    prompt = (
        format_context_for_prompt(ctx)
        + f"\n\n=== {stock_info}\n\n"
        + "Write a reply explaining that the product is unavailable and offering the alternatives above:"
    )

    response = _oos_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False

    logger.info(
        "Stock question: OOS reply with %d alternatives for %s",
        len(alternatives), result["client_email"],
    )
    return result
