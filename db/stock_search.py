"""
Stock Search and Availability
------------------------------

Region/warehouse mapping, stock search by name/id/category,
stock summary, and order availability check.

Active warehouse filtering: queries automatically exclude disabled warehouses
(fail-closed via _apply_warehouse_filter).
"""

import logging
import re

from sqlalchemy import or_

from db.catalog import get_equivalent_norms
from db.models import StockItem, get_session
from db.stock import get_product_type, _get_allowed_categories
from db.warehouse_config import get_active_warehouses, is_warehouse_active

logger = logging.getLogger(__name__)


# Region keywords → stock categories (for search_stock when query is a region name)
_REGION_CATEGORY_MAP: dict[str, set[str]] = {
    "japan": {"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"},
    "japanese": {"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"},
    "eu": {"TEREA_EUROPE"},
    "europe": {"TEREA_EUROPE"},
    "european": {"TEREA_EUROPE"},
    "armenia": {"ARMENIA"},
    "armenian": {"ARMENIA"},
    "me": {"ARMENIA", "KZ_TEREA"},
    "middle east": {"ARMENIA", "KZ_TEREA"},
    "kz": {"KZ_TEREA"},
    "kazakhstan": {"KZ_TEREA"},
    "unique": {"УНИКАЛЬНАЯ_ТЕРЕА"},
}

# Location keywords → warehouse names (for filtering by shipping origin)
WAREHOUSE_ALIASES: dict[str, str] = {
    "los angeles": "LA_MAKS",
    "california": "LA_MAKS",
    "illinois": "CHICAGO_MAX",
    "florida": "MIAMI_MAKS",
    "chicago": "CHICAGO_MAX",
    "miami": "MIAMI_MAKS",
    "ca": "LA_MAKS",
    "la": "LA_MAKS",
    "il": "CHICAGO_MAX",
    "fl": "MIAMI_MAKS",
}


def _apply_warehouse_filter(query, warehouse: str | None):
    """Apply active-warehouse filter to a SQLAlchemy query.

    Fail-closed: empty active list or disabled explicit warehouse = no results.
    """
    active = get_active_warehouses()
    if not active:
        return query.filter(False)
    if warehouse:
        if warehouse not in active:
            return query.filter(False)
        return query.filter_by(warehouse=warehouse)
    return query.filter(StockItem.warehouse.in_(active))


def resolve_warehouse(text: str) -> dict:
    """Extract warehouse from free text by matching location keywords.

    Returns:
        {"warehouse": str|None, "disabled": str|None}
        - If resolved and active:   {"warehouse": "LA_MAKS", "disabled": None}
        - If resolved but disabled:  {"warehouse": None, "disabled": "LA_MAKS"}
        - If not resolved:           {"warehouse": None, "disabled": None}
    """
    text_lower = text.lower()
    # Check longest keys first to match "los angeles" before "la"
    for alias in sorted(WAREHOUSE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", text_lower):
            resolved = WAREHOUSE_ALIASES[alias]
            if is_warehouse_active(resolved):
                return {"warehouse": resolved, "disabled": None}
            return {"warehouse": None, "disabled": resolved}
    return {"warehouse": None, "disabled": None}


def search_stock(query: str, warehouse: str | None = None) -> list[dict]:
    """Search stock by substring match (ILIKE %query%).

    Also searches spelling equivalents (e.g. "Sienna" also finds "Siena").
    If the query is a region name (e.g. "Japan"), returns all products
    from that region's categories.
    Used by LLM agents via search_stock_tool — intentionally broad.
    """
    session = get_session()
    try:
        trimmed = query.strip()

        # Check if query is a region name → search by category instead
        region_cats = _REGION_CATEGORY_MAP.get(trimmed.lower())
        if region_cats:
            q = session.query(StockItem).filter(
                StockItem.category.in_(region_cats),
            )
            q = _apply_warehouse_filter(q, warehouse)
            return [item.to_dict() for item in q.order_by(StockItem.product_name).all()]

        # Strip common prefixes that aren't in stock names
        # Stock items are "Green", "Turquoise" — not "Terea Green"
        trimmed = re.sub(r"(?i)^terea\s+", "", trimmed)
        trimmed = re.sub(r"(?i)\s+(made\s+in\s+)?(middle\s+east|europe|japan|eu|me|jp)\s*$", "", trimmed)
        trimmed = trimmed.strip()
        # Build ILIKE filters for original + equivalent spellings
        equivalent_norms = get_equivalent_norms(trimmed.lower())
        filters = [StockItem.product_name.ilike(f"%{trimmed}%")]
        for norm in equivalent_norms:
            if norm != trimmed.lower():
                filters.append(StockItem.product_name.ilike(f"%{norm}%"))

        q = session.query(StockItem).filter(or_(*filters))
        q = _apply_warehouse_filter(q, warehouse)
        results = q.all()

        # Fallback: if no results with full phrase, try individual words.
        # Handles cases like "Starling Pearl" → stock has "Starling".
        if not results and " " in trimmed:
            words = trimmed.split()
            for word in words:
                word = word.strip()
                if len(word) < 3:
                    continue
                word_norms = get_equivalent_norms(word.lower())
                word_filters = [StockItem.product_name.ilike(f"%{word}%")]
                for wn in word_norms:
                    if wn != word.lower():
                        word_filters.append(StockItem.product_name.ilike(f"%{wn}%"))
                wq = session.query(StockItem).filter(or_(*word_filters))
                wq = _apply_warehouse_filter(wq, warehouse)
                results = wq.all()
                if results:
                    break

        return [item.to_dict() for item in results]
    finally:
        session.close()


def search_stock_by_ids(
    product_ids: list[int],
    warehouse: str | None = None,
) -> list[dict]:
    """Get stock items by product catalog IDs (exact match)."""
    if not product_ids:
        return []
    session = get_session()
    try:
        q = session.query(StockItem).filter(
            StockItem.product_id.in_(product_ids),
        )
        q = _apply_warehouse_filter(q, warehouse)
        return [item.to_dict() for item in q.all()]
    finally:
        session.close()


def get_available_by_category(category: str, warehouse: str | None = None) -> list[dict]:
    """Get all items with available stock (quantity > 0) in a category."""
    session = get_session()
    try:
        q = session.query(StockItem).filter(
            StockItem.category == category,
            StockItem.quantity > 0,
        )
        q = _apply_warehouse_filter(q, warehouse)
        return [item.to_dict() for item in q.order_by(StockItem.product_name).all()]
    finally:
        session.close()


def get_stock_summary(
    warehouse: str | None = None,
    bypass_active_filter: bool = False,
) -> dict:
    """Get stock statistics: total items, available, fallback count, last sync time.

    Args:
        warehouse: Optional warehouse filter.
        bypass_active_filter: If True and warehouse is set, skip active-warehouse
            check. Used by stock_sync validation to read any warehouse.
    """
    session = get_session()
    try:
        q = session.query(StockItem)
        if bypass_active_filter and warehouse:
            q = q.filter_by(warehouse=warehouse)
        else:
            q = _apply_warehouse_filter(q, warehouse)
        items = q.all()

        if not items:
            return {"total": 0, "available": 0, "fallback": 0, "synced_at": None}

        return {
            "total": len(items),
            "available": sum(1 for i in items if i.quantity > 0),
            "fallback": sum(1 for i in items if i.is_fallback),
            "synced_at": max(i.synced_at for i in items if i.synced_at),
        }
    finally:
        session.close()


def check_stock_for_order(
    order_items: list[dict],
    warehouse: str | None = None,
) -> dict:
    """Check stock availability for ordered items.

    Args:
        order_items: List of dicts with keys: base_flavor, quantity, product_name.
        warehouse: Optional warehouse filter.

    Returns:
        {
            "all_in_stock": bool,
            "items": [{product_name, base_flavor, ordered_qty, stock_entries, total_available, is_sufficient}],
            "insufficient_items": [same structure, only insufficient],
        }
    """
    session = get_session()
    try:
        results = []
        all_ok = True

        for item in order_items:
            flavor = item["base_flavor"].strip()
            ordered_qty = item.get("quantity", 1)

            # Type-filter: only search in allowed categories (sticks or devices)
            product_type = get_product_type(flavor)
            allowed_cats = _get_allowed_categories(product_type)

            # Phase 8: product_id path only — no ILIKE fallback
            product_ids = item.get("product_ids")
            if product_ids:
                stock_entries = (
                    session.query(StockItem)
                    .filter(
                        StockItem.product_id.in_(product_ids),
                        StockItem.category.in_(allowed_cats),
                    )
                )
            else:
                # No product_ids → unresolved, return empty (no text matching)
                logger.warning(
                    "check_stock_for_order: no product_ids for '%s', "
                    "treating as unresolved (total_available=0)",
                    flavor,
                )
                stock_entries = session.query(StockItem).filter(False)  # empty
            stock_entries = _apply_warehouse_filter(stock_entries, warehouse)
            stock_entries = stock_entries.all()

            total_available = sum(
                max(s.quantity, 0)
                for s in stock_entries
            )
            is_sufficient = total_available >= ordered_qty

            entry = {
                "product_name": item.get("product_name", flavor),
                "base_flavor": flavor,
                "ordered_qty": ordered_qty,
                "stock_entries": [s.to_dict() for s in stock_entries],
                "total_available": total_available,
                "is_sufficient": is_sufficient,
            }
            # Preserve display_name for OOS template
            if item.get("display_name"):
                entry["display_name"] = item["display_name"]
            # Preserve original_product_name for region-aware alternatives
            if item.get("original_product_name"):
                entry["original_product_name"] = item["original_product_name"]
            # Preserve optional flag for conditional item handling (Phase C)
            if item.get("optional"):
                entry["optional"] = True
            results.append(entry)

            if not is_sufficient:
                all_ok = False

        return {
            "all_in_stock": all_ok,
            "items": results,
            "insufficient_items": [r for r in results if not r["is_sufficient"]],
        }
    finally:
        session.close()
