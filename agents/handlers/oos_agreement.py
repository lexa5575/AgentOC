"""OOS Agreement Resolution
---------------------------

Functions for resolving OOS items to confirmed order items:
matching alternatives from text, building replies, resolving
from classifier, and clearing pending OOS state.
"""

import logging

from db.catalog import get_display_name
from db.region_family import CATEGORY_REGION_SUFFIX as _CATEGORY_TO_REGION_SUFFIX

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
        from db.region_family import get_family
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
        from db.region_family import get_family_suffix
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
    from db.region_family import extract_region_from_text
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


def _resolve_changed_order(classifier_items, result, email_text):
    """Resolve changed order items against current order context.

    When customer mentions a product that's a spelling equivalent of an
    existing in-stock item (e.g., "Sienna" when "Siena" is in order),
    use the existing item's resolved name/region.

    For items NOT matching in-stock equivalents, keep as-is (normal resolution).
    Preserves non-replaced in-stock items in the final order.

    Returns list of confirmed items, or None if ambiguous (multiple equivalents).
    """
    import re
    from db.catalog import get_equivalent_norms

    state = result.get("conversation_state") or {}
    pending = (state.get("facts") or {}).get("pending_oos_resolution", {})
    in_stock = pending.get("in_stock_items", [])
    oos_items = pending.get("items", [])

    # Build equivalent lookup: norm → in-stock item
    in_stock_by_equiv = {}
    for item in in_stock:
        bf_lower = item["base_flavor"].lower()
        for norm in get_equivalent_norms(bf_lower):
            in_stock_by_equiv[norm] = item

    confirmed = []
    replaced_flavors = set()

    for oi in classifier_items:
        bf = getattr(oi, "base_flavor", "") or ""
        qty = getattr(oi, "quantity", 0) or 0
        bf_lower = bf.lower()

        # Check if spelling equivalent of existing in-stock item
        matched = None
        for norm in get_equivalent_norms(bf_lower):
            if norm in in_stock_by_equiv:
                matched = in_stock_by_equiv[norm]
                break

        if matched:
            # Unique match guard: only auto-confirm if exactly 1 equivalent found
            equiv_matches = [
                item for item in in_stock
                if item["base_flavor"].lower() in get_equivalent_norms(bf_lower)
            ]
            if len(equiv_matches) > 1:
                logger.warning(
                    "Multiple equivalent in-stock items for '%s' — skipping auto-confirm",
                    bf,
                )
                return None  # Fail-closed → fall through to LLM

            # Qty: use classifier qty only if explicitly stated,
            # otherwise inherit from in-stock item to avoid accidental downgrades
            effective_qty = qty
            if qty <= 1:
                has_explicit_qty = bool(re.search(
                    rf"\b(\d+)\s+(?:cartons?|boxes?|packs?)?\s*(?:of\s+)?{re.escape(bf)}\b",
                    email_text, re.IGNORECASE,
                ))
                if not has_explicit_qty:
                    effective_qty = matched.get("ordered_qty", qty)

            confirmed.append({
                "base_flavor": matched["base_flavor"],
                "product_name": matched.get("product_name", matched["base_flavor"]),
                "quantity": effective_qty,
            })
            replaced_flavors.update(get_equivalent_norms(matched["base_flavor"].lower()))
        else:
            # New product — inherit qty from replaced OOS item if not specified
            if qty <= 0 or qty == 1:
                inherited_qty = _inherit_qty_from_oos(bf, oos_items, email_text)
                if inherited_qty > 0:
                    qty = inherited_qty
            confirmed.append({
                "base_flavor": bf,
                "product_name": getattr(oi, "product_name", bf) or bf,
                "quantity": max(qty, 1),
            })

    # Add remaining in-stock items that were NOT replaced
    for item in in_stock:
        bf_lower = item["base_flavor"].lower()
        if bf_lower not in replaced_flavors:
            confirmed.append({
                "base_flavor": item["base_flavor"],
                "product_name": item.get("product_name", item["base_flavor"]),
                "quantity": item.get("ordered_qty", 1),
            })

    return confirmed if confirmed else None


def _inherit_qty_from_oos(base_flavor, oos_items, email_text=""):
    """Inherit quantity from the OOS item being replaced.

    Primary: parse 'instead of <flavor>' from email, find matching OOS item.
    Fallback: if exactly 1 OOS item, inherit its qty.
    """
    import re
    m = re.search(r"instead\s+of\s+(\w+)", email_text, re.IGNORECASE)
    if m:
        replaced_flavor = m.group(1).lower()
        for item in oos_items:
            if item["base_flavor"].lower() == replaced_flavor:
                return item.get("requested_qty", 0)

    if len(oos_items) == 1:
        return oos_items[0].get("requested_qty", 0)
    return 0


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
