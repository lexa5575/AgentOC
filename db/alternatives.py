"""
OOS Alternative Selection
--------------------------

Select the best in-stock alternatives for out-of-stock flavors using
client order history and LLM ranking.
"""

import logging

from db.models import ProductCatalog, StockItem, get_session
from db.stock import get_product_type, _get_allowed_categories
from db.order_items import get_client_flavor_history
from db.warehouse_config import get_active_warehouses

logger = logging.getLogger(__name__)


def _get_available_items(
    allowed_cats: set[str],
    warehouse: str | None = None,
    exclude_product_ids: list[int] | None = None,
) -> list[dict]:
    """Return available stock items filtered by category, warehouse, and exclusion.

    Args:
        allowed_cats: Set of allowed stock categories.
        warehouse: Optional warehouse filter.
        exclude_product_ids: Exclude by product_id (exact match).
    """
    session = get_session()
    try:
        active = get_active_warehouses()
        if not active:
            return []  # fail-closed
        q = session.query(StockItem, ProductCatalog.flavor_family).outerjoin(
            ProductCatalog, StockItem.product_id == ProductCatalog.id,
        ).filter(
            StockItem.category.in_(allowed_cats),
            StockItem.quantity > 0,
        )
        if exclude_product_ids:
            q = q.filter(~StockItem.product_id.in_(exclude_product_ids))
        if warehouse:
            if warehouse not in active:
                return []  # disabled warehouse
            q = q.filter(StockItem.warehouse == warehouse)
        else:
            q = q.filter(StockItem.warehouse.in_(active))
        results = []
        for item, flavor_family in q.order_by(StockItem.quantity.desc()).all():
            d = item.to_dict()
            d["flavor_family"] = flavor_family
            results.append(d)
        return results
    finally:
        session.close()


def select_best_alternatives(
    client_email: str,
    base_flavor: str,
    warehouse: str | None = None,
    max_options: int = 3,
    client_summary: str = "",
    excluded_products: set[str] | None = None,
    original_product_name: str | None = None,
    region_preference: list[str] | None = None,
    strict_region: bool = False,
) -> dict:
    """Select up to N best alternatives for an out-of-stock flavor using LLM.

    The LLM receives the exact list of available stock and selects the best
    matches based on client history, profile, and flavor semantics.
    Falls back to top-N by quantity if LLM returns nothing valid.

    Args:
        client_email: Client email for order history lookup.
        base_flavor: The out-of-stock flavor to find alternatives for.
        warehouse: Optional warehouse filter.
        max_options: Maximum number of alternatives to return.
        client_summary: Client's llm_summary text (pass client_data.get("llm_summary", "")).
        excluded_products: Product names already suggested for other OOS flavors in
            the same order. Prevents identical alternatives across multiple OOS flavors.
        original_product_name: Full product name from order (e.g. "Tera AMBER made
            in Europe"). Used for region detection in Priority 0. Falls back to
            base_flavor if not provided.
        region_preference: Ordered list of preferred region families (e.g. ["JAPAN"]).
            When set, filters available items to preferred region categories first.
        strict_region: If True and region_preference is set, only return alternatives
            from preferred region categories. If no items available → return empty.

    Returns:
        {"alternatives": [...], "reason": str, "order_count": None}
    """
    product_type = get_product_type(base_flavor)
    allowed_cats = _get_allowed_categories(product_type)
    _excluded = excluded_products or set()

    if max_options < 1:
        max_options = 1

    # 1. Fetch available stock (correct categories, qty > 0, not the OOS flavor)
    # Resolve with original_product_name so region filter excludes only
    # the OOS region's product_ids (e.g. Amber EU id=52), NOT all Amber ids.
    from db.product_resolver import resolve_product_to_catalog
    oos_resolve = resolve_product_to_catalog(
        base_flavor,
        original_product_name=original_product_name,
    )
    oos_product_ids = oos_resolve.product_ids if oos_resolve.product_ids else None

    available = _get_available_items(
        allowed_cats, warehouse,
        exclude_product_ids=oos_product_ids,
    )

    # Region preference filtering: narrow available items to preferred categories
    preferred_categories: set[str] | None = None
    if region_preference:
        from db.region_family import REGION_FAMILIES
        preferred_categories = set()
        for region in region_preference:
            cats = REGION_FAMILIES.get(region)
            if cats:
                preferred_categories |= cats
        if preferred_categories:
            region_filtered = [it for it in available if it["category"] in preferred_categories]
            if region_filtered:
                available = region_filtered
                logger.info(
                    "Region preference %s: filtered to %d items in categories %s",
                    region_preference, len(region_filtered), preferred_categories,
                )
            elif strict_region:
                logger.info(
                    "Region preference %s strict: no items in preferred categories → empty",
                    region_preference,
                )
                return {"alternatives": [], "reason": "region_unavailable", "order_count": None}
            else:
                logger.info(
                    "Region preference %s soft: no items in preferred categories → fallback to all",
                    region_preference,
                )

    if not available:
        return {"alternatives": [], "reason": "none_available", "order_count": None}

    # 2. Fetch client order history
    history = get_client_flavor_history(client_email, product_type=product_type)

    # 2b. Priority 0: same flavor, different region (e.g. "Amber ME" for "Amber EU")
    # The resolver normalizes base_flavor early (e.g. "AMBER" → "Amber"),
    # so we use original_product_name (e.g. "Tera AMBER made in Europe") to
    # detect whether a region was specified. If yes, the same flavor from
    # another region is the best alternative. Even without a region suffix,
    # we still check — the same base_flavor may exist in other categories
    # that were excluded by the region filter during stock check.
    from db.catalog import get_equivalent_norms
    from db.product_resolver import _normalize as _resolver_normalize, _extract_region_categories
    region_source = original_product_name or base_flavor
    oos_region_cats = _extract_region_categories(region_source)
    normalized_oos = _resolver_normalize(base_flavor)
    oos_equivalents = get_equivalent_norms(normalized_oos.lower())

    # Phase 8.2: when resolver couldn't find product_ids, exclude OOS flavor
    # in Python (no SQL ILIKE). Normalizes both sides for brand-prefix safety.
    if not oos_product_ids:
        available = [
            item for item in available
            if _resolver_normalize(item["product_name"]).lower() not in oos_equivalents
        ]
        if not available:
            return {"alternatives": [], "reason": "none_available", "order_count": None}

    same_flavor_items: list[dict] = []
    same_flavor_names: set[str] = set()
    # If region was detected, find the same flavor in OTHER regions
    # Uses spelling equivalents (e.g. "sienna" matches "siena")
    if oos_region_cats:
        from db.region_family import get_family

        # Collect all same-flavor items from other regions
        _raw_same_flavor: list[dict] = []
        for item in available:
            if (
                _resolver_normalize(item["product_name"]).lower() in oos_equivalents
                and item["category"] not in oos_region_cats
            ):
                _raw_same_flavor.append(item)

        # Dedup by family: ARMENIA Silver + KZ_TEREA Silver are both ME →
        # show only one "Terea Silver ME", pick the entry with highest stock.
        _best_by_family: dict[str | None, dict] = {}
        for item in _raw_same_flavor:
            family = get_family(item.get("category", ""))
            existing = _best_by_family.get(family)
            if existing is None or item.get("quantity", 0) > existing.get("quantity", 0):
                _best_by_family[family] = item
        same_flavor_items = list(_best_by_family.values())

        # strict_region: drop Priority 0 items outside preferred categories
        if strict_region and preferred_categories:
            same_flavor_items = [
                it for it in same_flavor_items
                if it["category"] in preferred_categories
            ]

        for item in same_flavor_items:
            same_flavor_names.add(item["product_name"])
        if same_flavor_items:
            logger.info(
                "Priority 0: same flavor '%s' found in other regions: %s",
                normalized_oos,
                [(it["category"], it["quantity"]) for it in same_flavor_items],
            )

    # 3. Ask LLM to pick remaining alternatives — fallback on any failure
    llm_slots = max(1, max_options - len(same_flavor_items))
    llm_excluded = _excluded | same_flavor_names

    # Look up OOS item's flavor_family from catalog
    oos_flavor_family = None
    if oos_product_ids:
        session = get_session()
        try:
            cat_entry = session.query(ProductCatalog.flavor_family).filter(
                ProductCatalog.id.in_(oos_product_ids),
                ProductCatalog.flavor_family.isnot(None),
            ).first()
            if cat_entry:
                oos_flavor_family = cat_entry[0]
        finally:
            session.close()

    try:
        from agents.alternatives import get_llm_alternatives
        llm_items = get_llm_alternatives(
            oos_flavor=base_flavor,
            available_items=available,
            order_history=history,
            client_summary=client_summary,
            max_options=llm_slots,
            excluded_products=llm_excluded,
            oos_flavor_family=oos_flavor_family,
            region_preference=region_preference,
            strict_region=strict_region,
        )
    except Exception as exc:
        logger.warning("LLM alternatives unavailable for '%s': %s", base_flavor, exc)
        llm_items = []

    # Post-filter: strict_region drops any LLM item outside preferred categories
    if strict_region and preferred_categories and llm_items:
        llm_items = [it for it in llm_items if it["category"] in preferred_categories]

    # 4. Build result: same_flavor first, then LLM picks
    selected: list[dict] = []
    seen_names: set[str] = set()
    for item in same_flavor_items:
        selected.append({"alternative": item, "reason": "same_flavor", "order_count": None})
        seen_names.add(item["product_name"])
    for item in llm_items:
        if item["product_name"] not in (_excluded | seen_names):
            selected.append({"alternative": item, "reason": "llm", "order_count": None})
            seen_names.add(item["product_name"])

    # 5. Fallback: fill remaining slots with top items by quantity
    # Priority: same flavor_family + same region family as OOS item
    # This prevents suggesting Japan ($115) alternatives for ME/EU ($110) products
    from db.region_family import get_family as _get_family

    # Determine the OOS item's region families for fallback filtering
    oos_region_families: set[str] = set()
    if oos_product_ids:
        from db.catalog import get_catalog_products as _get_catalog
        id_to_cat = {e["id"]: e["category"] for e in _get_catalog()}
        for pid in oos_product_ids:
            cat = id_to_cat.get(pid)
            if cat:
                fam = _get_family(cat)
                if fam:
                    oos_region_families.add(fam)
    # If region_preference is set, use that instead
    if region_preference:
        oos_region_families = set(region_preference)

    # Closeness map: when no same-family available, these are acceptable
    _CLOSE_FAMILIES = {
        "tobacco": {"fruit"},          # both non-menthol
        "fruit": {"tobacco"},
        "menthol": {"menthol_fruit"},   # both minty
        "menthol_fruit": {"menthol"},
        "capsule": set(),              # no close family
    }

    def _is_same_family(item: dict) -> bool:
        return not oos_flavor_family or item.get("flavor_family") == oos_flavor_family

    def _is_close_family(item: dict) -> bool:
        if not oos_flavor_family:
            return True
        close = _CLOSE_FAMILIES.get(oos_flavor_family, set())
        return item.get("flavor_family") in close

    def _fill_pass(predicate, reason="fallback"):
        for item in available:
            if len(selected) >= max_options:
                break
            if item["product_name"] not in (_excluded | seen_names) and predicate(item):
                region_fam = _get_family(item.get("category", ""))
                region_match = not oos_region_families or region_fam in oos_region_families
                if region_match:
                    selected.append({"alternative": item, "reason": reason, "order_count": None})
                    seen_names.add(item["product_name"])

    # Pass 1: same flavor family + same region family
    _fill_pass(_is_same_family)

    if len(selected) < max_options:
        # Pass 2: same flavor family, any region
        for item in available:
            if len(selected) >= max_options:
                break
            if item["product_name"] not in (_excluded | seen_names) and _is_same_family(item):
                selected.append({"alternative": item, "reason": "fallback", "order_count": None})
                seen_names.add(item["product_name"])

    if len(selected) < max_options:
        # Pass 3: close flavor family, any region
        for item in available:
            if len(selected) >= max_options:
                break
            if item["product_name"] not in (_excluded | seen_names) and _is_close_family(item):
                selected.append({"alternative": item, "reason": "fallback_close", "order_count": None})
                seen_names.add(item["product_name"])

    if len(selected) < max_options:
        # Pass 4 (last resort): any family, any region
        for item in available:
            if len(selected) >= max_options:
                break
            if item["product_name"] not in (_excluded | seen_names):
                selected.append({"alternative": item, "reason": "fallback_any", "order_count": None})
                seen_names.add(item["product_name"])

    if not selected:
        return {"alternatives": [], "reason": "none_available", "order_count": None}

    return {
        "alternatives": selected[:max_options],
        "reason": selected[0]["reason"],
        "order_count": None,
    }
