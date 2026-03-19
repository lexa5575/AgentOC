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
import re

from db.stock import (
    CATEGORY_PRICES,
    search_stock,
    search_stock_by_ids,
    select_best_alternatives,
    get_product_type,
    resolve_warehouse,
)
from db.catalog import get_base_display_name, get_display_name, get_catalog_products
from db.region_family import CATEGORY_REGION_SUFFIX
from db.product_resolver import resolve_product_to_catalog
from agents.context import build_context, format_context_for_prompt
from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# Sentinel value for fail-closed validation when allowed_products is empty
EMPTY_ALLOWED_SENTINEL = ["__empty_allowed__"]

# ---------------------------------------------------------------------------
# Agent (used only when any product is NOT in stock — for alternatives reply)
# ---------------------------------------------------------------------------

_oos_instructions = """\
You are James from shipmecarton.com.

Reply using EXACTLY this template. Do NOT deviate:

Hi {name}, {OOS product} is not available right now. We have {alt1}, {alt2}, {alt3} as alternatives. Would any of these work for you? Thank you!

RULES:
- Fill {OOS product} from the "NOT available" line in STOCK INFO.
- Fill {alt1}, {alt2}, {alt3} from "alternatives" in STOCK INFO. Use 2-3 items max.
- Do NOT mention price unless the customer asked about price.
- Do NOT add extra sentences, explanations, or questions beyond the template.
- Do NOT mention any product not listed in STOCK INFO.
- Always include region suffix: "Amber ME", "Lemon Japan", "Green EU".
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
    """Deterministic reply listing available products by region (0 LLM tokens).

    If client has favorite flavors or order history, personalize the reply
    by listing their preferred products first.
    """
    from db.stock import get_available_by_category

    greeting = f"Hi {client_name}," if client_name else "Hi,"
    client_data = result.get("client_data") or {}

    # Try to personalize from client profile
    favorite_flavors = client_data.get("favorite_flavors") or []
    if not favorite_flavors:
        # Extract from llm_summary if available
        summary = client_data.get("llm_summary", "")
        if summary:
            # Simple extraction: look for product names mentioned in summary
            from db.catalog import get_catalog_products
            catalog = get_catalog_products()
            catalog_names = {p["stock_name"].lower() for p in catalog}
            for word in summary.split():
                cleaned = word.strip(",.()").lower()
                if cleaned in catalog_names:
                    favorite_flavors.append(cleaned.title())

    # If we have favorites, check their availability first
    if favorite_flavors:
        from db.stock import search_stock
        available_favorites = []
        for fav in favorite_flavors[:5]:  # limit to top 5
            items = search_stock(fav, warehouse=warehouse)
            available = [it for it in items if it.get("quantity", 0) > 0]
            if available:
                dn = get_display_name(available[0]["product_name"], available[0]["category"])
                price = _price_for_items(available)
                if dn not in available_favorites:
                    available_favorites.append(dn)

        if available_favorites:
            fav_list = ", ".join(available_favorites[:4])
            price_str = ""
            # Get price from first available
            for fav in favorite_flavors[:1]:
                items = search_stock(fav, warehouse=warehouse)
                avail = [it for it in items if it.get("quantity", 0) > 0]
                if avail:
                    p = _price_for_items(avail)
                    if p:
                        price_str = f" ${p:.0f}/box."

            result["draft_reply"] = (
                f"{greeting} yes, we have availability!\n"
                f"Based on your previous orders — {fav_list} are in stock.{price_str}\n"
                f"Would you like to order any of these, or would you like to see the full list? Thank you!"
            )
            result["template_used"] = True
            result["needs_routing"] = False
            logger.info(
                "Stock question: personalized availability for %s (0 tokens, %d favorites)",
                result["client_email"], len(available_favorites),
            )
            return result

    # Fallback: generic region summary
    parts = [f"{greeting} here's what we currently have in stock:"]

    any_available = False
    for region_label, categories, price in _GENERAL_REGIONS:
        names = set()
        for cat in categories:
            for item in get_available_by_category(cat, warehouse=warehouse):
                dn = get_display_name(item["product_name"], item["category"])
                names.add(dn.lower())  # dedup case-insensitive
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
        seen_lower = set()
        for it in stock_items:
            dn = get_display_name(it["product_name"], it["category"])
            if dn.lower() not in seen_lower:
                seen_lower.add(dn.lower())
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

    # Guard: don't append region if flavor already contains it
    def _flavor_has_region(f: str, r: str) -> bool:
        fl = f.lower()
        return fl.endswith(r.lower()) or f"made in {r.lower()}" in fl

    if len(regions) > 1:
        # Multiple regions available — list each with region suffix
        region_list = ", ".join(
            flavor if _flavor_has_region(flavor, r) else f"{flavor} {r}"
            for r in regions
        )
        price_str = f" ${price:.0f} per box." if price is not None else ""
        return (
            f"{greeting} yes, we have {flavor} in stock{loc_suffix}!{price_str} "
            f"Available regions: {region_list}. "
            f"Which one would you like? Thank you!"
        )
    elif len(regions) == 1:
        # Single region — include it in the name (unless already there)
        flavor_with_region = flavor if _flavor_has_region(flavor, regions[0]) else f"{flavor} {regions[0]}"
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
            seen_lower = set()
            for it in available:
                dn = get_display_name(it["product_name"], it["category"])
                if dn.lower() not in seen_lower:
                    seen_lower.add(dn.lower())
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


def _extract_allowed_products(
    oos_sections: list[dict],
    in_stock_sections: list[dict] | None = None,
) -> set[str]:
    """Extract set of allowed product display names from structured data.

    Only products explicitly present in STOCK INFO (alternatives + available)
    are allowed. This prevents the validator from passing products that exist
    in DB but were not in the prompt.
    """
    allowed = set()
    for sec in oos_sections:
        for alt in sec.get("_alternatives_raw", []):
            item = alt["alternative"]
            dn = get_display_name(item["product_name"], item["category"])
            allowed.add(dn.lower())
    if in_stock_sections:
        for sec in in_stock_sections:
            for it in sec["available"]:
                dn = get_display_name(it["product_name"], it["category"])
                allowed.add(dn.lower())
    # OOS product names — allowed to mention (as unavailable)
    for sec in oos_sections:
        dn = sec["display_name"].lower()
        allowed.add(dn)
    return allowed


def _validate_reply_products(reply: str, allowed_products: set[str]) -> tuple[bool, list[str]]:
    """Validate that LLM reply only mentions allowed products.

    Uses word-boundary matching against full product catalog, longest-first
    to avoid partial matches. Returns (is_valid, list_of_forbidden_products).
    """
    if not allowed_products:
        logger.warning("Empty allowed_products — fail-closed, triggering fallback")
        return (False, EMPTY_ALLOWED_SENTINEL)

    all_catalog = get_catalog_products()
    # Build display names sorted longest-first (avoids partial matches)
    catalog_display = sorted(
        {get_display_name(p["stock_name"], p["category"]).lower() for p in all_catalog},
        key=len, reverse=True,
    )

    # Also build short aliases: "terea amber me" → also check "amber me", "amber"
    allowed_with_aliases = set(allowed_products)
    for ap in allowed_products:
        # Strip "terea " prefix
        if ap.startswith("terea "):
            allowed_with_aliases.add(ap[6:])
        # Strip "made in japan" → add "japan" suffix form
        if "made in japan" in ap:
            short = ap.replace(" made in japan", " japan")
            allowed_with_aliases.add(short)
            if short.startswith("terea "):
                allowed_with_aliases.add(short[6:])

    def _all_forms(dn: str) -> list[str]:
        """Generate all recognizable forms of a display name."""
        forms = [dn]
        if dn.startswith("terea "):
            forms.append(dn[6:])  # "amber me"
            base = dn[6:]
            # Strip region suffix
            for suffix in [" me", " eu", " japan", " made in japan"]:
                if base.endswith(suffix):
                    forms.append(base[:-len(suffix)].strip())  # just "amber"
                    break
        if "made in japan" in dn:
            forms.append(dn.replace(" made in japan", " japan"))
        return forms

    # Build all searchable forms for each catalog product (longest-first)
    # Each form maps back to ALL canonical display names (handles ambiguous short forms)
    form_to_canonicals: dict[str, list[str]] = {}
    for dn in catalog_display:
        for form in _all_forms(dn):
            form_to_canonicals.setdefault(form, [])
            if dn not in form_to_canonicals[form]:
                form_to_canonicals[form].append(dn)

    # Sort forms longest-first for matching
    all_forms_sorted = sorted(form_to_canonicals.keys(), key=len, reverse=True)

    reply_lower = reply.lower()
    forbidden = []
    matched_canonical = set()
    for form in all_forms_sorted:
        pattern = r'(?<!\w)' + re.escape(form) + r'(?!\w)'
        if re.search(pattern, reply_lower):
            canonicals = form_to_canonicals[form]
            for canonical in canonicals:
                if canonical in matched_canonical:
                    continue  # already checked via a longer form
                matched_canonical.add(canonical)
                # Check if ANY form of this product is in allowed set
                if not any(f in allowed_with_aliases for f in _all_forms(canonical)):
                    # Before flagging: if this form is ambiguous (shared with other
                    # canonicals) and ANY sibling canonical is allowed, skip —
                    # the ambiguous short form could refer to the allowed product.
                    if len(canonicals) > 1:
                        sibling_allowed = any(
                            any(f in allowed_with_aliases for f in _all_forms(sib))
                            for sib in canonicals if sib != canonical
                        )
                        if sibling_allowed:
                            continue
                    forbidden.append(canonical)

    return (len(forbidden) == 0, forbidden)


def _build_oos_fallback(client_name, oos_display_names, alt_display_names, price_by_region, warehouse):
    """Deterministic fallback reply when LLM hallucinates in OOS path."""
    greeting = f"Hi {client_name}," if client_name else "Hi,"
    loc = f" from our {_WAREHOUSE_DISPLAY.get(warehouse, '')} warehouse" if warehouse else ""
    oos_str = " and ".join(oos_display_names)
    alt_str = ", ".join(alt_display_names[:3])
    price_parts = [f"${p:.0f}/box for {r}" for r, p in price_by_region.items()]
    price_str = f" ({', '.join(price_parts)})" if price_parts else ""
    return (
        f"{greeting} {oos_str} {'is' if len(oos_display_names)==1 else 'are'} "
        f"not available{loc}. We have {alt_str}{price_str} as alternatives. "
        f"Would any of these work for you? Thank you!"
    )


def _build_mixed_fallback(client_name, in_stock_sections, oos_sections, warehouse):
    """Deterministic fallback reply when LLM hallucinates in mixed path."""
    greeting = f"Hi {client_name}," if client_name else "Hi,"
    loc = f" from our {_WAREHOUSE_DISPLAY.get(warehouse, '')} warehouse" if warehouse else ""
    parts = [f"{greeting} here's what we have{loc}:"]
    # In-stock
    for sec in in_stock_sections:
        price_str = f" ${sec['price']:.0f}/box" if sec.get('price') else ""
        parts.append(f"\n{sec['display_name']} — in stock{price_str}")
    # OOS + alternatives
    for sec in oos_sections:
        parts.append(f"\n{sec['display_name']} — not available")
        alts = sec.get("_alternatives_raw", [])
        if alts:
            alt_names = [get_display_name(a["alternative"]["product_name"], a["alternative"]["category"]) for a in alts[:3]]
            parts.append(f"Alternatives: {', '.join(alt_names)}")
    parts.append("\nLet us know what works for you! Thank you!")
    return "\n".join(parts)


def _handle_oos_reply(
    classification, result: dict, email_text: str,
    oos_sections: list[dict],
    client_name: str | None,
    warehouse: str | None,
) -> dict:
    """Handle reply when all queried products are OOS."""
    client_summary = (result.get("client_data") or {}).get("llm_summary", "")

    # Build lookup: base_flavor → order_item for region preference extraction
    order_items = getattr(classification, "order_items", None) or []
    _oi_by_flavor = {}
    for oi in order_items:
        bf = getattr(oi, "base_flavor", None)
        if bf:
            _oi_by_flavor[bf] = oi

    stock_info_parts = ["STOCK INFO:"]
    for sec in oos_sections:
        flavor = sec["flavor"]
        # Extract region preference from matching order_item
        _oi = _oi_by_flavor.get(flavor)
        _region_pref = getattr(_oi, "region_preference", None) if _oi else None
        _strict = getattr(_oi, "strict_region", False) if _oi else False
        alts_result = select_best_alternatives(
            client_email=result["client_email"],
            base_flavor=flavor,
            client_summary=client_summary,
            warehouse=warehouse,
            region_preference=_region_pref,
            strict_region=_strict,
        )
        alternatives = alts_result.get("alternatives", [])

        # Save structured data for validation
        sec["_alternatives_raw"] = alternatives
        sec["_region_preference"] = _region_pref
        sec["_strict_region"] = _strict

        stock_info_parts.append(f"\n{sec['display_name']} is NOT available.")
        if alternatives:
            alt_names = [get_display_name(a["alternative"]["product_name"], a["alternative"]["category"]) for a in alternatives[:3]]
            stock_info_parts.append("Alternatives: " + ", ".join(alt_names))
        else:
            stock_info_parts.append("No alternatives available.")

    stock_info = "\n".join(stock_info_parts)

    # Compute allowed products from structured data
    allowed_products = _extract_allowed_products(oos_sections)

    prompt = (
        f"=== {stock_info}\n\n"
        f"Customer name: {client_name or 'Customer'}\n"
        f"Reply using the template from your instructions."
    )

    response = _oos_agent.run(prompt)
    reply = response.content

    # Validate LLM reply — check for hallucinated products
    is_valid, forbidden = _validate_reply_products(reply, allowed_products)
    if not is_valid:
        logger.warning(
            "OOS reply validation FAILED for %s — forbidden products: %s. Using fallback.",
            result["client_email"], forbidden,
        )
        # Build deterministic fallback
        oos_display_names = [sec["display_name"] for sec in oos_sections]
        alt_display_names = []
        price_by_region = {}
        for sec in oos_sections:
            for alt in sec.get("_alternatives_raw", []):
                item = alt["alternative"]
                dn = get_display_name(item["product_name"], item["category"])
                if dn not in alt_display_names:
                    alt_display_names.append(dn)
                p = _price_for_items([item])
                if p is not None:
                    region = CATEGORY_REGION_SUFFIX.get(item["category"], "")
                    if region and region not in price_by_region:
                        price_by_region[region] = p

        reply = _build_oos_fallback(
            client_name, oos_display_names, alt_display_names,
            price_by_region, warehouse,
        )
        result["fallback_triggered"] = True
    else:
        result["fallback_triggered"] = False

    result["draft_reply"] = reply
    result["template_used"] = False
    result["needs_routing"] = False

    logger.info(
        "Stock question: OOS reply for %s (fallback=%s)",
        result["client_email"], result["fallback_triggered"],
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

    # Build lookup: base_flavor → order_item for region preference extraction
    order_items = getattr(classification, "order_items", None) or []
    _oi_by_flavor = {}
    for oi in order_items:
        bf = getattr(oi, "base_flavor", None)
        if bf:
            _oi_by_flavor[bf] = oi

    # OOS sections with alternatives
    for sec in oos_sections:
        flavor = sec["flavor"]
        # Extract region preference from matching order_item
        _oi = _oi_by_flavor.get(flavor)
        _region_pref = getattr(_oi, "region_preference", None) if _oi else None
        _strict = getattr(_oi, "strict_region", False) if _oi else False
        alts_result = select_best_alternatives(
            client_email=result["client_email"],
            base_flavor=flavor,
            client_summary=client_summary,
            warehouse=warehouse,
            region_preference=_region_pref,
            strict_region=_strict,
        )
        alternatives = alts_result.get("alternatives", [])

        # Save structured data for validation
        sec["_alternatives_raw"] = alternatives
        sec["_region_preference"] = _region_pref
        sec["_strict_region"] = _strict

        stock_info_parts.append(f"\n{sec['display_name']} — NOT available.")
        if alternatives:
            alt_names = [get_display_name(a["alternative"]["product_name"], a["alternative"]["category"]) for a in alternatives[:3]]
            stock_info_parts.append("Alternatives: " + ", ".join(alt_names))

    stock_info = "\n".join(stock_info_parts)

    # Compute allowed products from structured data
    allowed_products = _extract_allowed_products(oos_sections, in_stock_sections)

    prompt = (
        f"=== {stock_info}\n\n"
        f"Customer name: {client_name or 'Customer'}\n"
        f"Reply using the template from your instructions."
    )

    response = _oos_agent.run(prompt)
    reply = response.content

    # Validate LLM reply — check for hallucinated products
    is_valid, forbidden = _validate_reply_products(reply, allowed_products)
    if not is_valid:
        logger.warning(
            "Mixed reply validation FAILED for %s — forbidden products: %s. Using fallback.",
            result["client_email"], forbidden,
        )
        reply = _build_mixed_fallback(
            client_name, in_stock_sections, oos_sections, warehouse,
        )
        result["fallback_triggered"] = True
    else:
        result["fallback_triggered"] = False

    result["draft_reply"] = reply
    result["template_used"] = False
    result["needs_routing"] = False

    logger.info(
        "Stock question: mixed reply (%d in stock, %d OOS) for %s (fallback=%s)",
        len(in_stock_sections), len(oos_sections), result["client_email"],
        result["fallback_triggered"],
    )
    return result
