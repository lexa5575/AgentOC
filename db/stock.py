"""
Stock Operations
----------------

Stock sync, search, availability checks, order item tracking,
and out-of-stock alternative selection.
"""

import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.catalog import ensure_catalog_entry
from db.models import ClientOrderItem, StockBackup, StockItem, get_session

logger = logging.getLogger(__name__)


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


def extract_variant_id(product_ids: list[int] | None) -> int | None:
    """Extract variant_id from resolved product_ids.

    Single-match → return the id.
    Multi-match or empty → None + warning.
    """
    if not product_ids:
        return None
    if len(product_ids) == 1:
        return product_ids[0]
    logger.warning(
        "variant_id ambiguous: %d product_ids %s — stored as NULL",
        len(product_ids), product_ids,
    )
    return None


# Backward compat alias
_extract_variant_id = extract_variant_id


def has_ambiguous_variants(items: list[dict]) -> list[str]:
    """Return base_flavors of items with multi-match product_ids (len > 1).

    Used as a runtime gate: any ambiguous item should block auto-fulfillment.
    """
    return [
        item.get("base_flavor", "?")
        for item in items
        if len(item.get("product_ids") or []) > 1
    ]


# Backward compat alias
_has_ambiguous_variants = has_ambiguous_variants


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
                    maks_sales=item.maks_sales,
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
            catalog_id = ensure_catalog_entry(session, item["category"], item["product_name"])

            if record:
                record.quantity = item["quantity"]
                record.maks_sales = item.get("maks_sales", 0)
                record.is_fallback = item.get("is_fallback", False)
                record.source_row = item.get("source_row")
                record.source_col = item.get("source_col")
                record.product_id = catalog_id
                record.synced_at = now
            else:
                session.add(StockItem(
                    warehouse=warehouse,
                    category=item["category"],
                    product_name=item["product_name"],
                    quantity=item["quantity"],
                    maks_sales=item.get("maks_sales", 0),
                    is_fallback=item.get("is_fallback", False),
                    source_row=item.get("source_row"),
                    source_col=item.get("source_col"),
                    product_id=catalog_id,
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


def resolve_warehouse(text: str) -> str | None:
    """Extract warehouse from free text by matching location keywords.

    Checks longer aliases first, uses word boundaries to prevent
    false positives (e.g. "la" inside "balanced").
    """
    import re

    text_lower = text.lower()
    # Check longest keys first to match "los angeles" before "la"
    for alias in sorted(WAREHOUSE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", text_lower):
            return WAREHOUSE_ALIASES[alias]
    return None


def search_stock(query: str, warehouse: str | None = None) -> list[dict]:
    """Search stock by substring match (ILIKE %query%).

    Also searches spelling equivalents (e.g. "Sienna" also finds "Siena").
    If the query is a region name (e.g. "Japan"), returns all products
    from that region's categories.
    Used by LLM agents via search_stock_tool — intentionally broad.
    """
    from sqlalchemy import or_

    from db.catalog import get_equivalent_norms

    session = get_session()
    try:
        trimmed = query.strip()

        # Check if query is a region name → search by category instead
        region_cats = _REGION_CATEGORY_MAP.get(trimmed.lower())
        if region_cats:
            q = session.query(StockItem).filter(
                StockItem.category.in_(region_cats),
            )
            if warehouse:
                q = q.filter_by(warehouse=warehouse)
            return [item.to_dict() for item in q.order_by(StockItem.product_name).all()]

        # Strip common prefixes that aren't in stock names
        # Stock items are "Green", "Turquoise" — not "Terea Green"
        import re
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
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
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
                if warehouse:
                    wq = wq.filter_by(warehouse=warehouse)
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
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
        return [item.to_dict() for item in q.all()]
    finally:
        session.close()


def get_available_by_category(category: str, warehouse: str | None = None) -> list[dict]:
    """Get all items with available stock (quantity - maks_sales > 0) in a category."""
    session = get_session()
    try:
        q = session.query(StockItem).filter(
            StockItem.category == category,
            (StockItem.quantity - func.coalesce(StockItem.maks_sales, 0)) > 0,
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
            "available": sum(1 for i in items if (i.quantity - (i.maks_sales or 0)) > 0),
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
            if warehouse:
                stock_entries = stock_entries.filter_by(warehouse=warehouse)
            stock_entries = stock_entries.all()

            total_available = sum(
                max(s.quantity - (s.maks_sales or 0), 0)
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

    Each item dict: {product_name, base_flavor, quantity,
        variant_id (optional), display_name_snapshot (optional)}.
    product_type is auto-detected from base_flavor.
    Skips duplicates via UNIQUE constraint (using SAVEPOINT so
    a duplicate doesn't roll back previously inserted rows).
    Returns number of saved items.

    Guards:
    - order_id must be non-empty (NULL order_id is unsafe for dedup).
    """
    if not order_id or not str(order_id).strip():
        logger.warning(
            "save_order_items: order_id is empty/None, skipping for %s",
            client_email,
        )
        return 0

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
                variant_id=item.get("variant_id"),
                display_name_snapshot=item.get("display_name_snapshot"),
            )
            try:
                with session.begin_nested():  # SAVEPOINT
                    session.add(record)
                    session.flush()
                saved += 1
            except IntegrityError:
                # Savepoint rolled back, outer transaction intact
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


def replace_order_items(
    client_email: str,
    order_id: str,
    order_items: list[dict],
) -> int:
    """Atomically replace all order items for (client_email, order_id).

    DELETE old rows + INSERT new canonical rows in a single transaction.
    Used by OOS agrees_to_alternative when the confirmed item set may
    differ from what was originally saved.

    Guards:
    - order_id must be a non-empty string (NULL is unsafe for DELETE scope).
    - order_items must be non-empty (empty list = no-op, no deletion).

    Returns number of inserted items, or 0 on error / guard failure.
    """
    client_email = client_email.lower().strip()

    if not order_id or not order_id.strip():
        logger.warning("replace_order_items: order_id is empty, skipping for %s", client_email)
        return 0

    if not order_items:
        logger.warning("replace_order_items: empty order_items, skipping for %s order %s", client_email, order_id)
        return 0

    order_id = order_id.strip()
    session = get_session()
    try:
        deleted = session.query(ClientOrderItem).filter(
            ClientOrderItem.client_email == client_email,
            ClientOrderItem.order_id == order_id,
        ).delete()

        for item in order_items:
            base_flavor = item["base_flavor"].strip()
            session.add(ClientOrderItem(
                client_email=client_email,
                order_id=order_id,
                product_name=item.get("product_name", base_flavor),
                base_flavor=base_flavor,
                product_type=get_product_type(base_flavor),
                quantity=item.get("quantity", 1),
                variant_id=item.get("variant_id"),
                display_name_snapshot=item.get("display_name_snapshot"),
            ))

        session.commit()
        logger.info(
            "Replaced order items for %s order %s: %d deleted, %d inserted",
            client_email, order_id, deleted, len(order_items),
        )
        return len(order_items)
    except Exception as e:
        session.rollback()
        logger.error("Failed to replace order items for %s order %s: %s", client_email, order_id, e)
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
        avail_expr = StockItem.quantity - func.coalesce(StockItem.maks_sales, 0)
        q = session.query(StockItem).filter(
            StockItem.category.in_(allowed_cats),
            avail_expr > 0,
        )
        if exclude_product_ids:
            q = q.filter(~StockItem.product_id.in_(exclude_product_ids))
        if warehouse:
            q = q.filter_by(warehouse=warehouse)
        return [item.to_dict() for item in q.order_by(avail_expr.desc()).all()]
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
        for item in available:
            if (
                _resolver_normalize(item["product_name"]).lower() in oos_equivalents
                and item["category"] not in oos_region_cats
            ):
                same_flavor_items.append(item)
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
    try:
        from agents.alternatives import get_llm_alternatives
        llm_items = get_llm_alternatives(
            oos_flavor=base_flavor,
            available_items=available,
            order_history=history,
            client_summary=client_summary,
            max_options=llm_slots,
            excluded_products=llm_excluded,
        )
    except Exception as exc:
        logger.warning("LLM alternatives unavailable for '%s': %s", base_flavor, exc)
        llm_items = []

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

    # 5. Fallback: same_flavor empty AND LLM returned nothing → top-N by quantity
    if not selected:
        for item in available:
            if item["product_name"] not in (_excluded | seen_names):
                selected.append({"alternative": item, "reason": "fallback", "order_count": None})
                seen_names.add(item["product_name"])
            if len(selected) >= max_options:
                break

    if not selected:
        return {"alternatives": [], "reason": "none_available", "order_count": None}

    return {
        "alternatives": selected[:max_options],
        "reason": selected[0]["reason"],
        "order_count": None,
    }


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
