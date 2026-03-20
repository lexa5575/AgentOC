"""OOS Agreement Resolution
---------------------------

Functions for resolving OOS items to confirmed order items:
matching alternatives from text, building replies, resolving
from classifier, and clearing pending OOS state.
"""

import logging

from db.catalog import get_display_name
from db.region_family import (
    CATEGORY_REGION_SUFFIX as _CATEGORY_TO_REGION_SUFFIX,
    extract_region_from_text,
    get_family,
    get_family_suffix,
)

logger = logging.getLogger(__name__)


def _match_alternative_from_text(
    email_text: str,
    alternatives: list[dict],
    customer_region: str | None = None,
) -> dict | None:
    """Try to find which alternative the customer mentioned in their email.

    Returns the match only if exactly 1 product_name found in text.
    When multiple name-matches exist and customer_region is known,
    filters by region family for disambiguation.
    """
    email_lower = email_text.lower()
    matches = []
    for alt in alternatives:
        name = alt.get("product_name", "")
        if name and name.lower() in email_lower:
            matches.append(alt)

    if len(matches) == 1:
        return matches[0]

    # Multiple name-matches + explicit region → filter by family
    if len(matches) > 1 and customer_region:
        region_filtered = [
            alt for alt in matches
            if get_family(alt.get("category", "")) == customer_region
        ]
        if len(region_filtered) == 1:
            return region_filtered[0]

    return None


def _build_confirmed_item(
    alt: dict,
    requested_qty: int,
    email_text: str,
    customer_region: str | None,
) -> dict:
    """Build a confirmed item dict with correct region suffix and preference.

    If customer explicitly stated a region in their reply, uses that region
    (primary signal via region_preference, compatibility suffix in product_name).
    Otherwise falls back to the suggestion's category.
    """
    alt_name = alt["product_name"]

    if customer_region:
        # Customer explicitly stated a region — override suggestion's category
        suffix = get_family_suffix(customer_region)
        product_name = f"{alt_name} {suffix}" if suffix else alt_name
        region_pref = [customer_region]
    else:
        # No explicit region from customer — use suggestion's category
        alt_cat = alt.get("category", "")
        region_suffix = _CATEGORY_TO_REGION_SUFFIX.get(alt_cat)
        product_name = f"{alt_name} {region_suffix}" if region_suffix else alt_name
        region_pref = None

    item = {
        "base_flavor": alt_name,
        "product_name": product_name,
        "quantity": requested_qty,
    }
    if region_pref:
        item["region_preference"] = region_pref
    return item


def _resolve_oos_agreement(
    result: dict,
    email_text: str,
) -> tuple[list[dict] | None, str]:
    """Try to resolve OOS items to confirmed items for the order.

    Respects customer's explicit region when it differs from the suggested
    alternative's region (e.g. customer says "made in Europe" for a ME suggestion).

    Returns:
        (confirmed_items, "ok") — all items resolved
        (None, "clarify") — ambiguous: >1 alternative, email doesn't name one
        (None, "no_data") — no pending_oos_resolution in state
        (None, "no_alternatives") — full OOS with 0 alternatives
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")
    if not pending:
        return None, "no_data"

    # Detect customer's explicit region from their reply text
    customer_region = extract_region_from_text(email_text)

    confirmed = []

    # In-stock items from original order — keep as-is
    for item in pending.get("in_stock_items", []):
        confirmed.append({
            "base_flavor": item["base_flavor"],
            "product_name": item.get("product_name", item["base_flavor"]),
            "quantity": item["ordered_qty"],
        })

    # Resolve each OOS item
    for item in pending.get("items", []):
        available = item.get("available_qty", 0)
        if available > 0:
            # Partial OOS — reduce qty to what's available
            confirmed.append({
                "base_flavor": item["base_flavor"],
                "product_name": item.get("product_name", item["base_flavor"]),
                "quantity": available,
            })
        else:
            # Full OOS — need alternative
            flavor = item["base_flavor"]
            alt_data = pending.get("alternatives", {}).get(flavor, {})
            alts = alt_data.get("alternatives", [])

            if not alts:
                return None, "no_alternatives"

            if len(alts) == 1:
                # Only one alternative — auto-pick
                confirmed.append(_build_confirmed_item(
                    alts[0], item["requested_qty"], email_text, customer_region,
                ))
            else:
                # Multiple alternatives — try to match from email text
                matched = _match_alternative_from_text(
                    email_text, alts, customer_region=customer_region,
                )
                if matched:
                    confirmed.append(_build_confirmed_item(
                        matched, item["requested_qty"], email_text, customer_region,
                    ))
                else:
                    return None, "clarify"

    return confirmed, "ok"


def _build_clarification_reply(pending_oos: dict) -> str:
    """Build a 0-token clarification reply listing alternatives for ambiguous items."""
    lines = [
        "Hi!",
        "Thank you for getting back to us!",
        "We want to make sure we send you exactly what you'd like.",
        "Could you please confirm which option you'd prefer?",
    ]

    for item in pending_oos.get("items", []):
        if item.get("available_qty", 0) == 0:
            flavor = item["base_flavor"]
            alt_data = pending_oos.get("alternatives", {}).get(flavor, {})
            alts = alt_data.get("alternatives", [])
            if len(alts) > 1:
                lines.append(f"\nFor {flavor}:")
                for i, alt in enumerate(alts, 1):
                    lines.append(f"  {i}. {alt['product_name']}")

    lines.append("\nPlease let us know and we'll update your order right away!")
    lines.append("Thank you!")
    return "\n".join(lines)


def _resolve_from_classifier(classification) -> list[dict] | None:
    """Extract confirmed items from classifier's order_items.

    Fallback when pending_oos_resolution is missing — the classifier
    can see conversation history and extract what the customer agreed to.
    """
    order_items = getattr(classification, "order_items", None) or []
    if not order_items:
        return None

    confirmed = []
    for oi in order_items:
        bf = getattr(oi, "base_flavor", None)
        pn = getattr(oi, "product_name", None)
        qty = getattr(oi, "quantity", 1)
        if bf or pn:
            confirmed.append({
                "base_flavor": bf or pn,
                "product_name": pn or bf,
                "quantity": qty or 1,
            })

    return confirmed if confirmed else None


def _build_order_summary(stock_items: list[dict]) -> str:
    """Build order summary string like '2 x Terea Tropical Japan, 1 x Terea Black Japan'.

    Prefers resolved display_name (region-aware from resolver) over
    entries[0].category fallback to avoid wrong region display.
    """
    parts = []
    for item in stock_items:
        display = item.get("display_name")
        if not display:
            cat = ""
            entries = item.get("stock_entries") or []
            if entries:
                cat = entries[0].get("category", "")
            name = item.get("product_name") or item.get("base_flavor", "")
            display = get_display_name(name, cat)
        parts.append(f"{item['ordered_qty']} x {display}")
    return ", ".join(parts)


def _clear_pending_oos(result: dict) -> None:
    """Remove pending_oos_resolution from state facts (persisted by email_agent outbound save)."""
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    facts.pop("pending_oos_resolution", None)


def _normalize_order_id(classification) -> str | None:
    """Normalize order_id: strip whitespace, empty → None (plan §4)."""
    raw = getattr(classification, "order_id", None)
    return (raw or "").strip() or None
