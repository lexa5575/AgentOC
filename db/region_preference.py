"""
Region Preference Resolution
-----------------------------

Narrows cross-family product_ids to a single family based on:
1. Explicit customer region preference (e.g. "EU preferred, ME is ok if no EU").
2. Thread-backed hints from previous messages in the same Gmail thread.

Called AFTER resolve_order_items() and BEFORE check_stock_for_order().

Key contract:
- region_preference set → ALWAYS narrows to one family (never leaves cross-family)
- region_preference None → no-op (existing behavior preserved)
- apply_thread_hint → narrows cross-family items using thread message history
"""

import logging
import re
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


# ---------------------------------------------------------------------------
# Thread-backed canonical narrowing
# ---------------------------------------------------------------------------

# Broad aliases per family (fixed, flavor-independent region phrases)
_BROAD_ALIASES: dict[str, list[str]] = {
    "ME": ["middle east"],
    "EU": ["europe", "european"],
    "JAPAN": ["made in japan", "japanese"],
}


def _normalize_hint_text(text: str) -> str:
    """Lowercase, normalize punctuation to spaces, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_family_hint_phrases(
    base_flavor: str,
    pid_by_family: dict[str, list[int]],
    catalog_entries: list[dict],
) -> dict[str, dict[str, list[str]]]:
    """Build {family: {"full": [...], "broad": [...], "short": [...]}} for matching.

    Tier 1 — full catalog display names (via get_display_name).
    Tier 2 — broad aliases (base_flavor + region phrase).
    Tier 3 — short forms (base_flavor + family suffix).
    """
    id_to_entry = {e["id"]: e for e in catalog_entries}
    result: dict[str, dict[str, list[str]]] = {}

    for family, pids in pid_by_family.items():
        full: list[str] = []
        broad: list[str] = []
        short: list[str] = []

        # Tier 1: full display names from catalog
        for pid in pids:
            entry = id_to_entry.get(pid)
            if entry:
                display = get_display_name(entry["stock_name"], entry["category"])
                full.append(_normalize_hint_text(display))

        # Tier 2: broad aliases (flavor-prefixed only — no standalone)
        bf_lower = base_flavor.lower().strip()
        for alias in _BROAD_ALIASES.get(family, []):
            broad.append(_normalize_hint_text(f"{bf_lower} {alias}"))
            if alias == "japanese":
                broad.append(_normalize_hint_text(f"{alias} {bf_lower}"))

        # Tier 3: short forms (base_flavor + family suffix)
        suffix = _FAMILY_SUFFIX.get(family, "")
        if suffix:
            short.append(_normalize_hint_text(f"{bf_lower}{suffix}"))

        result[family] = {"full": full, "broad": broad, "short": short}

    return result


def apply_thread_hint(
    items: list[dict],
    thread_messages: list[dict] | None = None,
    catalog_entries: list[dict] | None = None,
) -> list[dict]:
    """Narrow cross-family product_ids using thread message history.

    Scans thread messages for region-specific phrases, using tiered matching:
    Tier 1 (full display names) > Tier 2 (broad aliases) > Tier 3 (short forms).
    Within each tier, messages are scanned newest-first.

    Only acts on items with cross-family product_ids and no region_preference.
    """
    if not thread_messages:
        return items

    from tools.email_parser import strip_quoted_text

    if catalog_entries is None:
        catalog_entries = get_catalog_products()

    # Pre-process messages: strip quoted text, normalize, newest-first
    processed_msgs = []
    for msg in reversed(thread_messages):
        body = msg.get("body") or msg.get("text") or ""
        cleaned = strip_quoted_text(body)
        normalized = _normalize_hint_text(cleaned)
        if normalized:
            processed_msgs.append({
                "normalized": normalized,
                "direction": msg.get("direction", "unknown"),
            })

    if not processed_msgs:
        return items

    for item in items:
        if item.get("region_preference"):
            continue
        product_ids = item.get("product_ids") or []
        if len(product_ids) <= 1:
            continue

        pid_by_family = _group_pids_by_family(product_ids, catalog_entries)
        if len(pid_by_family) <= 1:
            continue

        phrases = _build_family_hint_phrases(
            item.get("base_flavor", ""), pid_by_family, catalog_entries,
        )

        matched = _scan_tiers(phrases, processed_msgs, pid_by_family)
        if matched:
            family, tier, direction = matched
            item["product_ids"] = pid_by_family[family]
            _update_region_metadata(item, family, pid_by_family[family], catalog_entries)
            logger.info(
                "apply_thread_hint applied: base_flavor=%s family=%s tier=%d direction=%s",
                item.get("base_flavor"), family, tier, direction,
            )
        else:
            logger.info(
                "apply_thread_hint: no unambiguous family for %s",
                item.get("base_flavor"),
            )

    return items


def _scan_tiers(
    phrases: dict[str, dict[str, list[str]]],
    processed_msgs: list[dict],
    pid_by_family: dict[str, list[int]],
) -> tuple[str, int, str] | None:
    """Scan globally by tier, newest-first within tier.

    Returns (family, tier_number, direction) on first unambiguous match, or None.
    """
    families = list(pid_by_family.keys())
    tier_keys = [("full", 1), ("broad", 2), ("short", 3)]

    for tier_key, tier_num in tier_keys:
        for msg in processed_msgs:
            body = msg["normalized"]
            matched_families = []
            for family in families:
                for phrase in phrases[family][tier_key]:
                    if not phrase:
                        continue
                    pattern = r"\b" + re.escape(phrase) + r"\b"
                    if re.search(pattern, body):
                        matched_families.append(family)
                        break  # one match per family is enough
            if len(matched_families) == 1:
                return matched_families[0], tier_num, msg["direction"]
            # >1 match in same msg at same tier → skip this msg, continue
    return None
