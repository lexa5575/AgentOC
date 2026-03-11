"""
Region Preference Resolution
-----------------------------

Narrows cross-family product_ids to a single family based on explicit
customer region preference (e.g. "EU preferred, ME is ok if no EU").

Called AFTER resolve_order_items() and BEFORE check_stock_for_order().

Key contract:
- region_preference set → ALWAYS narrows to one family (never leaves cross-family)
- region_preference None → no-op (existing behavior preserved)
"""

import logging
from collections import defaultdict

from db.catalog import get_catalog_products, get_display_name
from db.region_family import REGION_FAMILIES, get_family
from db.stock import search_stock_by_ids

logger = logging.getLogger(__name__)

# Customer-facing suffix per family (local constant, no cross-module dependency)
_FAMILY_SUFFIX: dict[str, str] = {"EU": " EU", "ME": " ME", "JAPAN": " Japan"}


def apply_region_preference(
    items: list[dict],
    catalog_entries: list[dict] | None = None,
) -> list[dict]:
    """Narrow product_ids to one family based on region preference + stock.

    When region_preference is set, ALWAYS narrows to a single family:
    - If preferred family has stock → use it
    - If no preferred family has stock → use first preferred (→ OOS flow)
    - NEVER leaves cross-family product_ids when preference is explicit

    After narrowing, updates original_product_name, product_name, display_name
    to reflect chosen region.

    When region_preference is None → no-op (existing behavior preserved).
    """
    for item in items:
        pref = item.get("region_preference")
        if not pref:
            continue

        product_ids = item.get("product_ids") or []
        if len(product_ids) <= 1:
            # Already resolved to single product — don't touch metadata.
            # The resolver already set correct names for this single pid.
            # Using pref[0] here could mismatch if resolver picked a different
            # family than the preference suggests.
            continue

        # Lazy-load catalog if needed
        if catalog_entries is None:
            catalog_entries = get_catalog_products()

        # Group product_ids by family
        pid_by_family = _group_pids_by_family(product_ids, catalog_entries)

        # Check if already single-family
        if len(pid_by_family) <= 1:
            family = next(iter(pid_by_family)) if pid_by_family else pref[0]
            _update_region_metadata(
                item,
                chosen_family=family,
                chosen_pids=product_ids,
                catalog_entries=catalog_entries,
            )
            continue

        strict = item.get("strict_region", False)
        quantity = item.get("quantity", 1)

        chosen_family, chosen_pids = _select_family(
            pref, strict, pid_by_family, quantity,
        )

        item["product_ids"] = chosen_pids
        _update_region_metadata(item, chosen_family, chosen_pids, catalog_entries)

        logger.info(
            "Region preference applied: base_flavor=%s, pref=%s, strict=%s → family=%s, pids=%s",
            item.get("base_flavor"), pref, strict, chosen_family, chosen_pids,
        )

    return items


def _group_pids_by_family(
    product_ids: list[int],
    catalog_entries: list[dict],
) -> dict[str, list[int]]:
    """Group product_ids by region family using catalog categories."""
    id_to_cat = {e["id"]: e["category"] for e in catalog_entries}
    result: dict[str, list[int]] = defaultdict(list)
    for pid in product_ids:
        cat = id_to_cat.get(pid)
        if cat:
            family = get_family(cat)
            if family:
                result[family].append(pid)
    return dict(result)


def _select_family(
    pref: list[str],
    strict: bool,
    pid_by_family: dict[str, list[int]],
    quantity: int,
) -> tuple[str, list[int]]:
    """Select the best family based on preference + stock availability.

    Returns (chosen_family, chosen_pids).
    """
    if strict:
        # Strict: only first preferred family, regardless of stock
        family = pref[0]
        return family, pid_by_family.get(family, [])

    # Soft: try each preferred family in order, pick first with stock
    for family in pref:
        family_pids = pid_by_family.get(family, [])
        if not family_pids:
            continue
        if _family_has_warehouse_stock(family_pids, quantity):
            return family, family_pids

    # No preferred family has stock → fall back to first preferred
    # (→ downstream OOS flow, NOT ambiguous)
    first = pref[0]
    return first, pid_by_family.get(first, [])


def _family_has_warehouse_stock(family_pids: list[int], quantity: int) -> bool:
    """Check if any single warehouse covers quantity for these product_ids.

    Uses search_stock_by_ids() from db/stock.py — lightweight query.
    Groups stock by warehouse and checks if any warehouse has enough total.
    """
    stock_items = search_stock_by_ids(family_pids)
    if not stock_items:
        return False

    # Group by warehouse, sum quantities
    wh_totals: dict[str, int] = defaultdict(int)
    for si in stock_items:
        wh_totals[si["warehouse"]] += si.get("quantity", 0)

    return any(total >= quantity for total in wh_totals.values())


def _update_region_metadata(
    item: dict,
    chosen_family: str,
    chosen_pids: list[int],
    catalog_entries: list[dict] | None,
) -> None:
    """Deterministically set region-aware names based on chosen_family.

    ALWAYS overwrites original_product_name, product_name, display_name
    to the canonical form for chosen_family. No suffix-checking — pure
    deterministic normalization.
    """
    base = item.get("base_flavor", "")
    suffix = _FAMILY_SUFFIX.get(chosen_family, "")

    # ALWAYS overwrite to canonical region-aware form
    item["original_product_name"] = f"{base}{suffix}".strip()

    # Synthesize fallback from existing product_name (preserves brand prefix,
    # e.g. "ONE Green" stays "ONE Green ME", not "Terea Green ME")
    existing_name = item.get("product_name", "") or base
    # Strip old region suffixes before appending new one
    _fallback_name = existing_name
    for _s in _FAMILY_SUFFIX.values():
        if _fallback_name.endswith(_s):
            _fallback_name = _fallback_name[: -len(_s)].rstrip()
            break
    fallback = f"{_fallback_name}{suffix}".strip()

    # Update display_name + product_name from catalog or synthesize
    if chosen_pids and catalog_entries:
        pid_set = set(chosen_pids)
        matched = [e for e in catalog_entries if e["id"] in pid_set]
        if matched:
            display = get_display_name(
                matched[0]["stock_name"], matched[0]["category"],
            )
            item["display_name"] = display
            item["product_name"] = display
        else:
            item["product_name"] = fallback
            item["display_name"] = fallback
    else:
        item["product_name"] = fallback
        item["display_name"] = fallback
