"""
Stock Question Handler
----------------------

Handles questions about product availability:
- "Do you have Tropical?"
- "Is Silver in stock?"
- "Any European available? And do you have Japan regular?"

Flow:
1. Extract ALL asked-about flavors from order_items (classifier) or conversation state
2. For each flavor: search_stock(flavor) → real stock data
3. Build composite reply covering all queried products (0 LLM tokens when all in stock)
4. If any OOS → LLM reply with stock info + alternatives
"""

import logging

from db.stock import (
    CATEGORY_PRICES,
    search_stock,
    search_stock_by_ids,
    select_best_alternatives,
    get_product_type,
    resolve_warehouse,
)
from db.catalog import get_base_display_name, get_display_name
from db.region_family import CATEGORY_REGION_SUFFIX
from db.product_resolver import resolve_product_to_catalog
from agents.context import build_context, format_context_for_prompt
from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent (used only when any product is NOT in stock — for alternatives reply)
# ---------------------------------------------------------------------------

_oos_instructions = """\
You are James, answering a product availability question for shipmecarton.com.

## Style rules (STRICT)
- Write like a casual text message: short, warm, no formality
- 2-5 sentences MAX. Never write a long paragraph.
- Start with "Hi {name}," if name is known, otherwise start directly
- Always end with exactly "Thank you!" — nothing after it
- No bullet points, no bold, no lists

## Content rules
- ALWAYS use the STOCK INFO section — never make up availability
- For products that ARE available: list them clearly with price
- For products that are NOT available: say so and mention alternatives if provided
- Ask if any of the available products or alternatives work for the customer
- Never invent product names or prices not listed in STOCK INFO

## Region in product names — MANDATORY
- ALWAYS include the region suffix (ME, EU, or Japan) when mentioning a product
- Say "Terea Silver ME", NOT just "Terea Silver"
- This is critical — without region, the system can't process the order correctly
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

_GENERAL_REGIONS = [
    ("Middle East (ME)", ["ARMENIA", "KZ_TEREA"], 110),
    ("European (EU)", ["TEREA_EUROPE"], 110),
    ("Japanese", ["TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"], 115),
]


def _handle_general_availability(
    result: dict,
    client_name: str | None,
    warehouse: str | None,
) -> dict:
    """Deterministic reply listing available products by region (0 LLM tokens)."""
    from db.stock import get_available_by_category

    greeting = f"Hi {client_name}," if client_name else "Hi,"
    parts = [f"{greeting} here's what we currently have in stock:"]

    any_available = False
    for region_label, categories, price in _GENERAL_REGIONS:
        names = set()
        for cat in categories:
            for item in get_available_by_category(cat, warehouse=warehouse):
                dn = get_display_name(item["product_name"], item["category"])
                names.add(dn)
        if names:
            any_available = True
            parts.append(f"- {region_label} — ${price}/box ({len(names)} flavors)")

    if not any_available:
        parts.append("Unfortunately nothing is in stock at the moment.")
    else:
        parts.append("\nWhich region are you interested in? We'll send you the full list! Thank you!")
    result["draft_reply"] = "\n".join(parts)
    result["template_used"] = True
    result["needs_routing"] = False

    logger.info(
        "Stock question: general availability reply for %s (0 tokens)",
        result["client_email"],
    )
    return result


def _extract_flavors(classification, result: dict) -> list[str]:
    """Extract ALL products being asked about.

    Priority:
    1. classification.order_items (classifier parsed the email)
    2. conversation_state facts → confirmed/pending order_items
    """
    # 1. Classifier extracted items
    order_items = getattr(classification, "order_items", None) or []
    if order_items:
        flavors = []
        for item in order_items:
            f = getattr(item, "base_flavor", None) or getattr(item, "product_name", None)
            if f and f not in flavors:
                flavors.append(f)
        return flavors

    # 2. Conversation state (single item fallback)
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    for key in ("confirmed_order_items", "pending_order_items", "order_items"):
        items = facts.get(key) or []
        if items:
            first = items[0]
            if isinstance(first, str):
                return [first]
            f = first.get("base_flavor") or first.get("product_name")
            if f:
                return [f]

    return []


def _price_for_items(stock_items: list[dict]) -> float | None:
    """Return per-box price for a set of stock items, or None if ambiguous."""
    categories = {it["category"] for it in stock_items}
    prices = {CATEGORY_PRICES[c] for c in categories if c in CATEGORY_PRICES}
    if len(prices) == 1:
        return prices.pop()
    return None


_WAREHOUSE_DISPLAY = {
    "LA_MAKS": "California",
    "CHICAGO_MAX": "Chicago",
    "MIAMI_MAKS": "Miami",
}


def _is_region_query(flavor: str) -> bool:
    """Check if flavor is a region name (Japan, EU, Armenia, etc.)."""
    from db.stock import _REGION_CATEGORY_MAP
    return flavor.lower().strip() in _REGION_CATEGORY_MAP


def _build_in_stock_reply(
    client_name: str | None,
    flavor: str,
    stock_items: list[dict],
    price: float | None,
    *,
    is_region: bool = False,
    warehouse: str | None = None,
) -> str:
    """Deterministic reply when product IS in stock (0 LLM tokens)."""
    greeting = f"Hi {client_name}," if client_name else "Hi,"
    location = _WAREHOUSE_DISPLAY.get(warehouse, "") if warehouse else ""
    loc_suffix = f" from our {location} warehouse" if location else ""

    # Region query (e.g. "Japan") → list all available products
    distinct_names = sorted({it["product_name"] for it in stock_items})
    if is_region and len(distinct_names) > 1:
        display_names = []
        for it in stock_items:
            dn = get_display_name(it["product_name"], it["category"])
            if dn not in display_names:
                display_names.append(dn)
        product_list = ", ".join(display_names)
        price_str = f" ${price:.0f} per box." if price is not None else ""
        return (
            f"{greeting} we have these {flavor} products in stock{loc_suffix}:{price_str}\n"
            f"{product_list}\n"
            f"Let us know which one you'd like! Thank you!"
        )

    # Single product query — include region to disambiguate (ME vs EU vs Japan)
    regions = sorted({CATEGORY_REGION_SUFFIX.get(it["category"], "") for it in stock_items} - {""})
    if len(regions) > 1:
        # Multiple regions available — list each with region suffix
        region_list = ", ".join(f"{flavor} {r}" for r in regions)
        price_str = f" ${price:.0f} per box." if price is not None else ""
        return (
            f"{greeting} yes, we have {flavor} in stock{loc_suffix}!{price_str} "
            f"Available regions: {region_list}. "
            f"Which one would you like? Thank you!"
        )
    elif len(regions) == 1:
        # Single region — include it in the name
        flavor_with_region = f"{flavor} {regions[0]}"
        price_str = f" It's ${price:.0f} per box." if price is not None else ""
        return (
            f"{greeting} yes, we have {flavor_with_region} in stock{loc_suffix}!{price_str} "
            f"Let us know how many boxes you'd like and we'll get it ready for you. "
            f"Thank you!"
        )
    else:
        price_str = f" It's ${price:.0f} per box." if price is not None else ""
        return (
            f"{greeting} yes, we have {flavor} in stock{loc_suffix}!{price_str} "
            f"Let us know how many boxes you'd like and we'll get it ready for you. "
            f"Thank you!"
        )


def _build_multi_stock_reply(
    client_name: str | None,
    sections: list[dict],
    *,
    warehouse: str | None = None,
) -> str:
    """Deterministic reply for multiple stock queries, all in stock (0 LLM tokens).

    Each section: {"flavor": str, "display_name": str, "available": list, "price": float|None, "is_region": bool}
    """
    greeting = f"Hi {client_name}," if client_name else "Hi,"
    location = _WAREHOUSE_DISPLAY.get(warehouse, "") if warehouse else ""
    loc_suffix = f" from our {location} warehouse" if location else ""

    parts = [f"{greeting} here's what we have in stock{loc_suffix}:"]

    for sec in sections:
        flavor = sec["display_name"]
        available = sec["available"]
        price = sec["price"]
        is_region = sec["is_region"]
        price_str = f" ${price:.0f} per box" if price is not None else ""

        distinct_names = sorted({it["product_name"] for it in available})
        if is_region and len(distinct_names) > 1:
            display_names = []
            for it in available:
                dn = get_display_name(it["product_name"], it["category"])
                if dn not in display_names:
                    display_names.append(dn)
            product_list = ", ".join(display_names)
            parts.append(f"\n{flavor}:{price_str}\n{product_list}")
        else:
            parts.append(f"\n{flavor}{price_str}")

    parts.append("\nLet us know which ones you'd like! Thank you!")
    return "\n".join(parts)


def _lookup_flavor(flavor: str, warehouse: str | None) -> dict:
    """Look up stock for a single flavor. Returns structured result."""
    catalog_result = resolve_product_to_catalog(flavor)
    display_name = catalog_result.display_name or get_base_display_name(flavor)

    if catalog_result.product_ids:
        stock_items = search_stock_by_ids(catalog_result.product_ids, warehouse=warehouse)
    else:
        stock_items = search_stock(flavor, warehouse=warehouse)
    available = [it for it in stock_items if it["quantity"] > 0]

    return {
        "flavor": flavor,
        "display_name": display_name,
        "available": available,
        "price": _price_for_items(available) if available else None,
        "is_region": _is_region_query(flavor),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle_stock_question(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle product availability questions.

    Supports multiple products/regions in a single query.
    Returns deterministic reply (0 tokens) when all products are in stock.
    Falls back to LLM reply when any product is OOS.
    """
    flavors = _extract_flavors(classification, result)

    if not flavors:
        logger.info(
            "Stock question: general availability query for %s",
            result["client_email"],
        )
        warehouse = resolve_warehouse(email_text)
        client_name = result.get("client_name") or (
            result.get("client_data") or {}
        ).get("name")
        return _handle_general_availability(result, client_name, warehouse)

    # Optional warehouse filter (e.g. "from CA" → LA_MAKS)
    warehouse = resolve_warehouse(email_text)
    if warehouse:
        logger.info("Stock question: warehouse filter=%s for %s", warehouse, result["client_email"])

    client_name = result.get("client_name") or (
        result.get("client_data") or {}
    ).get("name")

    # Look up stock for each flavor
    lookups = [_lookup_flavor(f, warehouse) for f in flavors]

    in_stock_sections = [lk for lk in lookups if lk["available"]]
    oos_sections = [lk for lk in lookups if not lk["available"]]

    # -------------------------------------------------------------------
    # Case 1: Single flavor (backward-compatible path)
    # -------------------------------------------------------------------
    if len(flavors) == 1:
        lk = lookups[0]
        if lk["available"]:
            result["draft_reply"] = _build_in_stock_reply(
                client_name, lk["display_name"], lk["available"], lk["price"],
                is_region=lk["is_region"], warehouse=warehouse,
            )
            result["template_used"] = True
            result["needs_routing"] = False
            logger.info(
                "Stock question: %s IN STOCK for %s, price=%s (0 tokens)",
                lk["flavor"], result["client_email"], lk["price"],
            )
            return result
        else:
            # OOS — single item, use LLM with alternatives
            return _handle_oos_reply(
                classification, result, email_text,
                oos_sections, client_name, warehouse,
            )

    # -------------------------------------------------------------------
    # Case 2: Multiple flavors, all in stock → deterministic composite
    # -------------------------------------------------------------------
    if not oos_sections:
        result["draft_reply"] = _build_multi_stock_reply(
            client_name, in_stock_sections, warehouse=warehouse,
        )
        result["template_used"] = True
        result["needs_routing"] = False
        logger.info(
            "Stock question: %d items ALL IN STOCK for %s (0 tokens)",
            len(flavors), result["client_email"],
        )
        return result

    # -------------------------------------------------------------------
    # Case 3: Multiple flavors, mixed in-stock/OOS → LLM reply
    # -------------------------------------------------------------------
    return _handle_mixed_reply(
        classification, result, email_text,
        in_stock_sections, oos_sections,
        client_name, warehouse,
    )


def _handle_oos_reply(
    classification, result: dict, email_text: str,
    oos_sections: list[dict],
    client_name: str | None,
    warehouse: str | None,
) -> dict:
    """Handle reply when all queried products are OOS."""
    client_summary = (result.get("client_data") or {}).get("llm_summary", "")

    stock_info_parts = ["STOCK INFO:"]
    for sec in oos_sections:
        flavor = sec["flavor"]
        alts_result = select_best_alternatives(
            client_email=result["client_email"],
            base_flavor=flavor,
            client_summary=client_summary,
            warehouse=warehouse,
        )
        alternatives = alts_result.get("alternatives", [])

        stock_info_parts.append(f"\n{flavor} is NOT available.")
        if alternatives:
            alt_lines = []
            for a in alternatives:
                item = a["alternative"]
                p = _price_for_items([item])
                price_str = f" (${p:.0f}/box)" if p is not None else ""
                alt_lines.append(f"- {item['product_name']}{price_str}")
            stock_info_parts.append("Available alternatives:\n" + "\n".join(alt_lines))
        else:
            stock_info_parts.append("No similar alternatives currently in stock.")

    stock_info = "\n".join(stock_info_parts)

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
        "Stock question: OOS reply for %s",
        result["client_email"],
    )
    return result


def _handle_mixed_reply(
    classification, result: dict, email_text: str,
    in_stock_sections: list[dict],
    oos_sections: list[dict],
    client_name: str | None,
    warehouse: str | None,
) -> dict:
    """Handle reply when some products are in stock and others are OOS."""
    client_summary = (result.get("client_data") or {}).get("llm_summary", "")

    stock_info_parts = ["STOCK INFO:"]

    # In-stock sections
    for sec in in_stock_sections:
        flavor = sec["display_name"]
        price = sec["price"]
        available = sec["available"]
        price_str = f" (${price:.0f}/box)" if price is not None else ""

        distinct_names = sorted({it["product_name"] for it in available})
        if sec["is_region"] and len(distinct_names) > 1:
            display_names = []
            for it in available:
                dn = get_display_name(it["product_name"], it["category"])
                if dn not in display_names:
                    display_names.append(dn)
            product_list = ", ".join(display_names)
            stock_info_parts.append(f"\n{flavor} — AVAILABLE{price_str}: {product_list}")
        else:
            stock_info_parts.append(f"\n{flavor} — AVAILABLE{price_str}")

    # OOS sections with alternatives
    for sec in oos_sections:
        flavor = sec["flavor"]
        alts_result = select_best_alternatives(
            client_email=result["client_email"],
            base_flavor=flavor,
            client_summary=client_summary,
            warehouse=warehouse,
        )
        alternatives = alts_result.get("alternatives", [])

        stock_info_parts.append(f"\n{flavor} — NOT available.")
        if alternatives:
            alt_lines = []
            for a in alternatives:
                item = a["alternative"]
                p = _price_for_items([item])
                price_str = f" (${p:.0f}/box)" if p is not None else ""
                alt_lines.append(f"- {item['product_name']}{price_str}")
            stock_info_parts.append("Alternatives:\n" + "\n".join(alt_lines))

    stock_info = "\n".join(stock_info_parts)

    ctx = build_context(classification, result, email_text)
    prompt = (
        format_context_for_prompt(ctx)
        + f"\n\n=== {stock_info}\n\n"
        + "Write a reply covering all products the customer asked about. "
        + "List what's available with prices, mention what's not available, and suggest alternatives:"
    )

    response = _oos_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False

    logger.info(
        "Stock question: mixed reply (%d in stock, %d OOS) for %s",
        len(in_stock_sections), len(oos_sections), result["client_email"],
    )
    return result
