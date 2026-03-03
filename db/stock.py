"""
Stock Operations
----------------

Stock sync, search, availability checks, order item tracking,
and out-of-stock alternative selection.
"""

import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.models import ClientOrderItem, StockBackup, StockItem, get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base flavor extraction (mirrors email_parser._extract_base_flavor)
# Duplicated here to avoid circular import: db → tools → agents
# ---------------------------------------------------------------------------

_BRAND_PREFIXES = ("Tera ", "Terea ", "Heets ")
_REGION_SUFFIXES = (
    " made in Middle East",
    " made in Armenia",
    " EU",
    " Japan",
    " KZ",
)


def _base_flavor_from_name(product_name: str) -> str:
    """Strip brand prefix and region suffix to get base flavor.

    Examples:
        "Tera Green made in Middle East" → "Green"
        "Tera Amber made in Armenia"     → "Amber"
        "ONE Green"                      → "ONE Green"  (device, not stripped)
    """
    name = product_name.strip()
    for pfx in _BRAND_PREFIXES:
        if name.startswith(pfx):
            name = name[len(pfx):]
            break
    for sfx in _REGION_SUFFIXES:
        if name.lower().endswith(sfx.lower()):
            name = name[: -len(sfx)]
            break
    return name.strip()


# ---------------------------------------------------------------------------
# Product type constants (sticks vs devices)
# ---------------------------------------------------------------------------

DEVICE_CATEGORIES = {"ONE", "STND", "PRIME"}
STICK_CATEGORIES = {"KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA", "УНИКАЛЬНАЯ_ТЕРЕА"}

# Unit prices by stock category (fixed catalog)
CATEGORY_PRICES: dict[str, int] = {
    "TEREA_EUROPE": 110,
    "KZ_TEREA": 110,
    "ARMENIA": 110,
    "TEREA_JAPAN": 115,
    "УНИКАЛЬНАЯ_ТЕРЕА": 115,
    "ONE": 99,
    "STND": 149,
    "PRIME": 245,
}


def get_product_type(base_flavor: str) -> str:
    """Determine product type from base_flavor.

    Devices have brand prefix: 'ONE Green', 'STND Red', 'PRIME Black'.
    Also matches bare model names: 'ONE', 'STND', 'PRIME'.
    Sticks are just flavor: 'Green', 'Silver', 'Turquoise'.
    """
    upper = base_flavor.upper().strip()
    for prefix in ("ONE", "STND", "PRIME"):
        if upper == prefix or upper.startswith(prefix + " "):
            return "device"
    return "stick"


def _get_allowed_categories(product_type: str) -> set[str]:
    """Return allowed stock categories for a product type."""
    return DEVICE_CATEGORIES if product_type == "device" else STICK_CATEGORIES


# ---------------------------------------------------------------------------
# Stock sync & queries
# ---------------------------------------------------------------------------

def sync_stock(warehouse: str, items: list[dict]) -> int:
    """Sync stock data: backup current → upsert new items.

    Each item dict: {category, product_name, quantity, is_fallback, source_row, source_col}.
    Returns number of upserted records.
    """
    from datetime import datetime

    session = get_session()
    try:
        # Backup current data before overwrite
        existing = session.query(StockItem).filter_by(warehouse=warehouse).all()
        if existing:
            session.query(StockBackup).filter_by(warehouse=warehouse).delete()
            for item in existing:
                session.add(StockBackup(
                    warehouse=item.warehouse,
                    category=item.category,
                    product_name=item.product_name,
                    quantity=item.quantity,
                    is_fallback=item.is_fallback,
                    source_row=item.source_row,
                    source_col=item.source_col,
                    synced_at=item.synced_at,
                ))

        # Upsert items
        now = datetime.utcnow()
        new_keys = set()
        count = 0
        for item in items:
            key = (item["category"], item["product_name"])
            new_keys.add(key)

            record = (
                session.query(StockItem)
                .filter_by(
                    warehouse=warehouse,
                    category=item["category"],
                    product_name=item["product_name"],
                )
                .first()
            )
            if record:
                record.quantity = item["quantity"]
                record.is_fallback = item.get("is_fallback", False)
                record.source_row = item.get("source_row")
                record.source_col = item.get("source_col")
                record.synced_at = now
            else:
                session.add(StockItem(
                    warehouse=warehouse,
                    category=item["category"],
                    product_name=item["product_name"],
                    quantity=item["quantity"],
                    is_fallback=item.get("is_fallback", False),
                    source_row=item.get("source_row"),
                    source_col=item.get("source_col"),
                    synced_at=now,
                ))
            count += 1

        # Delete stale items no longer in the spreadsheet
        stale = (
            session.query(StockItem)
            .filter_by(warehouse=warehouse)
            .all()
        )
        deleted = 0
        for item in stale:
            if (item.category, item.product_name) not in new_keys:
                session.delete(item)
                deleted += 1

        session.commit()
        if deleted:
            logger.info("Stock sync for %s: %d stale items removed", warehouse, deleted)
        logger.info("Stock sync for %s: %d items upserted", warehouse, count)
        return count
    except Exception as e:
        logger.error("Stock sync failed for %s: %s", warehouse, e)
        session.rollback()
        raise
    finally:
        session.close()


def search_stock(query: str, warehouse: str | None = None) -> list[dict]:
    """Search stock by substring match (ILIKE %query%)."""
    session = get_session()
    try:
        q = session.query(StockItem).filter(
            StockItem.product_name.ilike(f"%{query.strip()}%")
        )
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
        return [item.to_dict() for item in q.all()]
    finally:
        session.close()


def get_available_by_category(category: str, warehouse: str | None = None) -> list[dict]:
    """Get all items with quantity > 0 in a category."""
    session = get_session()
    try:
        q = session.query(StockItem).filter(
            StockItem.category == category,
            StockItem.quantity > 0,
        )
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
        return [item.to_dict() for item in q.order_by(StockItem.product_name).all()]
    finally:
        session.close()


def get_stock_summary(warehouse: str | None = None) -> dict:
    """Get stock statistics: total items, available, fallback count, last sync time."""
    session = get_session()
    try:
        q = session.query(StockItem)
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
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

            stock_entries = (
                session.query(StockItem)
                .filter(
                    StockItem.product_name.ilike(f"%{flavor}%"),
                    StockItem.category.in_(allowed_cats),
                )
            )
            if warehouse:
                stock_entries = stock_entries.filter_by(warehouse=warehouse)
            stock_entries = stock_entries.all()

            total_available = sum(s.quantity for s in stock_entries if s.quantity > 0)
            is_sufficient = total_available >= ordered_qty

            entry = {
                "product_name": item.get("product_name", flavor),
                "base_flavor": flavor,
                "ordered_qty": ordered_qty,
                "stock_entries": [s.to_dict() for s in stock_entries],
                "total_available": total_available,
                "is_sufficient": is_sufficient,
            }
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


# ---------------------------------------------------------------------------
# Order item history (for personalized OOS alternatives)
# ---------------------------------------------------------------------------

def save_order_items(
    client_email: str,
    order_id: str | None,
    order_items: list[dict],
) -> int:
    """Save structured order items for preference tracking.

    Each item dict: {product_name, base_flavor, quantity}.
    product_type is auto-detected from base_flavor.
    Skips duplicates via UNIQUE constraint.
    Returns number of saved items.
    """
    client_email = client_email.lower().strip()
    session = get_session()
    saved = 0
    try:
        for item in order_items:
            base_flavor = item["base_flavor"].strip()
            record = ClientOrderItem(
                client_email=client_email,
                order_id=order_id,
                product_name=item["product_name"],
                base_flavor=base_flavor,
                product_type=get_product_type(base_flavor),
                quantity=item.get("quantity", 1),
            )
            try:
                session.add(record)
                session.flush()
                saved += 1
            except IntegrityError:
                session.rollback()
                logger.debug("Order item already exists: %s / %s / %s", client_email, order_id, base_flavor)
        session.commit()
        if saved:
            logger.info("Saved %d order items for %s (order %s)", saved, client_email, order_id)
        return saved
    except Exception as e:
        logger.error("Failed to save order items: %s", e)
        session.rollback()
        return 0
    finally:
        session.close()


def get_client_flavor_history(
    client_email: str,
    product_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Get unique flavors ordered by this client, ranked by frequency.

    Returns: [{"base_flavor": "Green", "order_count": 5, "last_ordered": datetime}, ...]
    """
    client_email = client_email.lower().strip()
    session = get_session()
    try:
        query = (
            session.query(
                ClientOrderItem.base_flavor,
                func.count(ClientOrderItem.id).label("order_count"),
                func.max(ClientOrderItem.created_at).label("last_ordered"),
            )
            .filter(ClientOrderItem.client_email == client_email)
        )
        if product_type:
            query = query.filter(ClientOrderItem.product_type == product_type)

        rows = (
            query
            .group_by(ClientOrderItem.base_flavor)
            .order_by(func.count(ClientOrderItem.id).desc(), func.max(ClientOrderItem.created_at).desc())
            .limit(limit)
            .all()
        )
        return [
            {"base_flavor": row[0], "order_count": row[1], "last_ordered": row[2]}
            for row in rows
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# OOS alternative selection
# ---------------------------------------------------------------------------

def select_best_alternatives(
    client_email: str,
    base_flavor: str,
    warehouse: str | None = None,
    max_options: int = 3,
) -> dict:
    """Select up to N best alternatives for an out-of-stock flavor.

    Never suggests the same flavor that was ordered — those entries are
    already counted in check_stock_for_order's total_available.

    Priority:
    1. Flavors from customer's order history that are currently in stock
       (personalized — what they actually like)
    2. Other available items, excluding the OOS flavor (general fallback)

    Returns:
    {
      "alternatives": [
        {"alternative": {...}, "reason": "history|fallback", "order_count": int|None},
        ...
      ],
      "reason": str,
      "order_count": int|None,
    }
    """
    product_type = get_product_type(base_flavor)
    allowed_cats = _get_allowed_categories(product_type)
    flavor = base_flavor.strip()

    if max_options < 1:
        max_options = 1

    session = get_session()
    try:
        selected: list[dict] = []
        seen = set()

        def _push(item: StockItem, reason: str, order_count: int | None = None):
            key = (item.category, item.product_name)
            if key in seen:
                return
            seen.add(key)
            selected.append({
                "alternative": item.to_dict(),
                "reason": reason,
                "order_count": order_count,
            })

        # Priority 1: history-based alternatives — flavors the client ordered
        # before, excluding the OOS flavor itself, currently in stock.
        history = get_client_flavor_history(client_email, product_type=product_type)
        for h in history:
            hist_flavor = h["base_flavor"]
            if hist_flavor.lower() == flavor.lower():
                continue

            q_hist = session.query(StockItem).filter(
                StockItem.product_name.ilike(f"%{hist_flavor}%"),
                StockItem.category.in_(allowed_cats),
                StockItem.quantity > 0,
            )
            if warehouse:
                q_hist = q_hist.filter_by(warehouse=warehouse)
            for item in q_hist.order_by(StockItem.quantity.desc()).all():
                _push(item, reason="history", order_count=h["order_count"])
                if len(selected) >= max_options:
                    break
            if len(selected) >= max_options:
                break

        # Priority 1.5: profile-based — match in-stock items against llm_summary.
        # Fills the gap when ClientOrderItem is empty (new automation, old client).
        # Uses word boundaries to avoid false positives ("one" in "one day" etc.).
        if len(selected) < max_options:
            import re
            from db.models import Client
            client_row = session.query(Client).filter_by(email=client_email).first()
            summary = (client_row.llm_summary or "") if client_row else ""
            if summary:
                q_profile = session.query(StockItem).filter(
                    StockItem.category.in_(allowed_cats),
                    StockItem.quantity > 0,
                    ~StockItem.product_name.ilike(f"%{flavor}%"),
                )
                if warehouse:
                    q_profile = q_profile.filter_by(warehouse=warehouse)
                for item in q_profile.order_by(StockItem.quantity.desc()).all():
                    item_base = _base_flavor_from_name(item.product_name)
                    if (
                        item_base
                        and item_base.lower() != flavor.lower()
                        and re.search(
                            r"\b" + re.escape(item_base) + r"\b",
                            summary,
                            re.IGNORECASE,
                        )
                    ):
                        _push(item, reason="profile")
                        if len(selected) >= max_options:
                            break

        # Priority 2: fallback — any available item excluding the OOS flavor.
        # Never includes the same flavor (e.g. "Amber from Armenia" when
        # client ordered Amber and total_available already accounts for it).
        if len(selected) < max_options:
            q_any = session.query(StockItem).filter(
                StockItem.category.in_(allowed_cats),
                StockItem.quantity > 0,
                ~StockItem.product_name.ilike(f"%{flavor}%"),
            )
            if warehouse:
                q_any = q_any.filter_by(warehouse=warehouse)
            for item in q_any.order_by(StockItem.quantity.desc()).all():
                _push(item, reason="fallback")
                if len(selected) >= max_options:
                    break

        if not selected:
            return {
                "alternatives": [],
                "reason": "none_available",
                "order_count": None,
            }

        return {
            "alternatives": selected[:max_options],
            "reason": selected[0]["reason"],
            "order_count": selected[0].get("order_count"),
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Price calculation from stock check results
# ---------------------------------------------------------------------------

def calculate_order_price(stock_check_items: list[dict]) -> float | None:
    """Calculate total order price from stock check results.

    Strict mode: returns None if any item is unmatched (no stock_entries),
    has an unknown category, or falls into multiple price groups.
    No fallback prices — caller decides what to do with None.
    """
    if not stock_check_items:
        return None

    total = 0.0
    for item in stock_check_items:
        entries = item.get("stock_entries", [])
        if not entries:
            logger.warning(
                "Price calc: no stock entries for '%s'",
                item.get("base_flavor", "?"),
            )
            return None

        # All entries must resolve to the same unit price
        prices_seen = {CATEGORY_PRICES.get(e["category"]) for e in entries}
        prices_seen.discard(None)
        if len(prices_seen) != 1:
            logger.warning(
                "Price calc: ambiguous categories for '%s': %s",
                item.get("base_flavor", "?"),
                [e["category"] for e in entries],
            )
            return None

        unit_price = prices_seen.pop()
        total += item["ordered_qty"] * unit_price

    return total
