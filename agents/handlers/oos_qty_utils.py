"""OOS Quantity Utilities
------------------------

Quantity enrichment and in-stock merging from pending_oos_resolution.
"""

import logging

from agents.handlers.oos_text_utils import (
    _detect_region_and_core,
    _extract_client_qty_for_flavor,
    _extract_standalone_qty,
    _extract_base_flavor_from_label,
    _extract_qty_from_label,
    _extract_region_suffix_from_label,
)

logger = logging.getLogger(__name__)


def _build_pending_qty_map(pending: dict) -> dict[str, int]:
    """Build base_flavor → requested_qty map from pending_oos_resolution.

    Includes direct OOS items, in-stock items, and reverse-mapped alternatives.
    Uses _detect_region_and_core() for proper multi-word flavor extraction.

    Conflict handling: if an alt maps to multiple parents with different qtys,
    that alt is excluded (logged as warning).
    """
    qty_map: dict[str, int] = {}

    for item in pending.get("items", []):
        bf = (item.get("base_flavor") or "").strip().lower()
        if bf:
            qty_map[bf] = item.get("requested_qty", 1)

    for item in pending.get("in_stock_items", []):
        bf = (item.get("base_flavor") or "").strip().lower()
        if bf:
            qty_map[bf] = item.get("ordered_qty", 1)

    # Reverse alternatives: alt_flavor → parent OOS requested_qty
    alt_conflicts: dict[str, set[int]] = {}
    alternatives = pending.get("alternatives", {})
    for oos_flavor, alt_data in alternatives.items():
        oos_bf = oos_flavor.strip().lower()
        parent_qty = qty_map.get(oos_bf)
        if parent_qty is None:
            logger.warning(
                "Reverse-map: OOS flavor '%s' not found in qty_map (keys: %s) — "
                "skipping alt enrichment for this flavor",
                oos_bf, list(qty_map.keys()),
            )
            continue
        for alt in alt_data.get("alternatives", []):
            alt_pn = (alt.get("product_name") or "").strip()
            if not alt_pn:
                continue
            _, alt_core = _detect_region_and_core(alt_pn)
            alt_bf = alt_core.strip().lower()
            if not alt_bf or alt_bf in qty_map:
                continue
            alt_conflicts.setdefault(alt_bf, set()).add(parent_qty)

    for alt_bf, qtys in alt_conflicts.items():
        if len(qtys) == 1:
            qty_map[alt_bf] = qtys.pop()
        else:
            logger.warning(
                "Qty conflict for alt '%s': parents have different qtys %s — skipping enrichment",
                alt_bf, qtys,
            )

    return qty_map


def _merge_in_stock_items(
    extracted_items: list[dict],
    result: dict,
) -> list[dict]:
    """Merge in-stock items from pending_oos_resolution into extracted items.

    Thread extraction only returns items the customer mentioned (substitutions).
    Original in-stock items (not OOS) must be preserved in the final order.
    Skips items whose base_flavor already appears in extracted (avoids duplicates).
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")

    in_stock: list[dict] = []
    if pending:
        in_stock = pending.get("in_stock_items", [])
    elif facts.get("ordered_items") and facts.get("oos_items"):
        # Fallback: reconstruct in-stock from ordered_items minus oos_items
        oos_flavors = {
            _extract_base_flavor_from_label(oos).lower()
            for oos in facts["oos_items"]
        }
        for label in facts["ordered_items"]:
            bf = _extract_base_flavor_from_label(label)
            qty = _extract_qty_from_label(label)
            region = _extract_region_suffix_from_label(label)
            pn = f"{bf} {region}" if region else bf
            if bf.lower() not in oos_flavors:
                in_stock.append({
                    "base_flavor": bf,
                    "product_name": pn,
                    "ordered_qty": qty,
                })
                logger.info(
                    "Reconstructed in-stock item '%s' x%d from facts.ordered_items",
                    pn, qty,
                )

    if not in_stock:
        return extracted_items

    # Collect flavors already in extracted (lowercase for matching)
    extracted_flavors = {
        (item.get("base_flavor") or "").strip().lower()
        for item in extracted_items
    }

    merged = list(extracted_items)
    for item in in_stock:
        bf = (item.get("base_flavor") or "").strip()
        if bf.lower() not in extracted_flavors:
            merged.append({
                "base_flavor": bf,
                "product_name": item.get("product_name", bf),
                "quantity": item.get("ordered_qty", 1),
            })
            logger.info(
                "Merged in-stock item '%s' x%d into extraction result",
                bf, item.get("ordered_qty", 1),
            )

    return merged


def _enrich_qty_from_pending(
    extracted_items: list[dict],
    result: dict,
    inbound_text: str = "",
) -> list[dict]:
    """Enrich extracted quantities from pending_oos_resolution (per-item).

    For EACH item: if LLM returned qty=1 (default) and pending knows a higher qty,
    use pending qty — unless customer explicitly specified a qty near that flavor.
    Single-item special case: standalone qty (e.g. "just 1 box") counts as explicit.
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")
    if not pending:
        return extracted_items

    pending_qty = _build_pending_qty_map(pending)
    if not pending_qty:
        return extracted_items

    enriched = []
    for item in extracted_items:
        item = dict(item)
        bf = (item.get("base_flavor") or "").strip().lower()
        extracted_qty = item.get("quantity", 1)
        original_qty = pending_qty.get(bf)

        if original_qty and extracted_qty == 1 and original_qty > 1:
            client_qty = _extract_client_qty_for_flavor(inbound_text, bf)
            if client_qty is None and len(extracted_items) == 1:
                client_qty = _extract_standalone_qty(inbound_text)

            if client_qty is not None:
                item["quantity"] = client_qty
                logger.info(
                    "Keeping client-specified qty for '%s': %d", bf, client_qty,
                )
            else:
                item["quantity"] = original_qty
                logger.info(
                    "Enriched qty for '%s': 1 → %d (from pending_oos_resolution)",
                    bf, original_qty,
                )

        enriched.append(item)

    return enriched
