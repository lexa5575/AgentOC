"""
Fulfillment Engine
------------------

Deterministic warehouse selection and maks_sales increment.

Architecture constraints:
- No dependency on LLM conversation_state for fulfillment data
- No private imports from tools/stock_sync.py internals
- Uses public APIs: db.sheet_config, tools.google_sheets, db.warehouse_geo
"""

import json
import logging
from os import getenv

from db.models import ClientOrderItem, StockItem, get_session
from db.warehouse_geo import resolve_warehouse_from_address

logger = logging.getLogger(__name__)


def _use_family_fulfillment() -> bool:
    """Feature flag for region-family expansion in fulfillment queries.

    Default=true. Set USE_FAMILY_FULFILLMENT=false/0/no/off to disable.
    """
    return getenv("USE_FAMILY_FULFILLMENT", "true").lower() not in ("false", "0", "no", "off")


# ── Fulfillment statuses + event lifecycle (re-exported from fulfillment_events) ──

from db.fulfillment_events import (
    STATUS_UPDATED,
    STATUS_SKIPPED_SPLIT,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_BLOCKED_AMBIGUOUS,
    STATUS_ERROR,
    STATUS_PROCESSING,
    _BLOCKING_STATUSES,
    _RETRIABLE_STATUSES,
    is_duplicate_fulfillment,
    claim_fulfillment_event,
    finalize_fulfillment_event,
    parse_details_json,
)


# ── Warehouse config accessor (reads env var, no private imports) ────

def get_warehouse_spreadsheet_id(warehouse: str) -> str | None:
    """Get spreadsheet_id for a warehouse from STOCK_WAREHOUSES env var."""
    raw = getenv("STOCK_WAREHOUSES", "").strip()
    if raw:
        try:
            configs = json.loads(raw)
            for cfg in configs:
                if cfg["name"] == warehouse:
                    return cfg["spreadsheet_id"]
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    # Legacy single-warehouse fallback
    if warehouse == getenv("STOCK_WAREHOUSE_NAME", "LA_MAKS"):
        return getenv("STOCK_SPREADSHEET_ID", "") or None
    return None


# ── Warehouse selection ──────────────────────────────────────────────

def select_fulfillment_warehouse(
    order_items: list[dict],
    city_state_zip: str,
) -> dict:
    """Select a single warehouse that can fulfill the entire order.

    Tries warehouses in geographic proximity order. Success only if ONE
    warehouse covers ALL items with sufficient quantities.

    Args:
        order_items: List of dicts with keys: base_flavor, quantity,
            product_ids (optional).
        city_state_zip: Client address for proximity ordering.

    Returns:
        {
            "status": str,
            "warehouse": str | None,
            "matched_items": list | None,
            "tried_warehouses": list[str],
        }
    """
    if not order_items:
        return {
            "status": STATUS_SKIPPED_UNRESOLVED,
            "warehouse": None,
            "matched_items": None,
            "tried_warehouses": [],
        }

    priority = resolve_warehouse_from_address(city_state_zip)
    tried = []

    session = get_session()
    try:
        for wh in priority:
            tried.append(wh)
            matched = _try_warehouse(session, wh, order_items)
            if matched is not None:
                return {
                    "status": STATUS_UPDATED,
                    "warehouse": wh,
                    "matched_items": matched,
                    "tried_warehouses": tried,
                }

        breakdown = _collect_split_breakdown(session, priority, order_items)
        return {
            "status": STATUS_SKIPPED_SPLIT,
            "warehouse": None,
            "matched_items": None,
            "tried_warehouses": tried,
            "split_breakdown": breakdown,
        }
    finally:
        session.close()


def _query_stock_entries(
    session, warehouse: str, base_flavor: str, product_ids: list,
) -> list:
    """Query StockItem entries for an item in a warehouse.

    When USE_FAMILY_FULFILLMENT=true, expands product_ids to include
    same-family siblings (e.g. variant_id=ARMENIA Silver → also finds
    KZ_TEREA Silver). Expansion is strictly by name_norm + family.

    Shared by _try_warehouse and _collect_split_breakdown.
    """
    if product_ids:
        expanded = list(product_ids)
        if _use_family_fulfillment():
            from db.catalog import get_catalog_products
            from db.region_family import expand_to_family_ids
            expanded = expand_to_family_ids(product_ids, get_catalog_products())
        return (
            session.query(StockItem)
            .filter(
                StockItem.product_id.in_(expanded),
                StockItem.warehouse == warehouse,
            )
            .all()
        )
    # Phase 8: no ILIKE fallback — empty product_ids → no entries
    logger.warning(
        "_query_stock_entries: no product_ids for '%s' in warehouse '%s', "
        "returning empty (no text matching)",
        base_flavor, warehouse,
    )
    return []


def _try_warehouse(
    session, warehouse: str, order_items: list[dict],
) -> list[dict] | None:
    """Check if one warehouse can fulfill ALL items.

    Sums quantities across matching stock entries (not first()).

    Returns:
        List of matched items with source info for increment, or None.
    """
    matched = []

    for item in order_items:
        base_flavor = item["base_flavor"].strip()
        ordered_qty = item.get("quantity", 1)
        product_ids = item.get("product_ids", [])

        entries = _query_stock_entries(session, warehouse, base_flavor, product_ids)

        if not entries:
            return None

        total_available = sum(
            max(e.quantity, 0)
            for e in entries
        )
        if total_available < ordered_qty:
            return None

        # Pick primary entry (highest quantity) for maks_sales increment
        primary = max(entries, key=lambda e: e.quantity)

        matched.append({
            "base_flavor": base_flavor,
            "product_name": primary.product_name,
            "ordered_qty": ordered_qty,
            "category": primary.category,
            "source_row": primary.source_row,
            "maks_sales": primary.maks_sales,
            "stock_item_id": primary.id,
            "total_available": total_available,
        })

    return matched


def _collect_split_breakdown(
    session, warehouses: list[str], order_items: list[dict],
) -> list[dict]:
    """Collect per-item availability across all warehouses.

    Called only on the skipped_split path.
    Warehouse order in availability matches the warehouses param.
    """
    breakdown = []
    for item in order_items:
        base_flavor = item["base_flavor"].strip()
        ordered_qty = item.get("quantity", 1)
        product_ids = item.get("product_ids", [])

        availability = {}
        for wh in warehouses:
            entries = _query_stock_entries(session, wh, base_flavor, product_ids)
            total = sum(max(e.quantity, 0) for e in entries) if entries else 0
            availability[wh] = total

        breakdown.append({
            "base_flavor": base_flavor,
            "ordered_qty": ordered_qty,
            "availability": availability,
        })
    return breakdown


# ── maks_sales increment ─────────────────────────────────────────────

def increment_maks_sales(warehouse: str, matched_items: list[dict]) -> dict:
    """Increment maks_sales in Google Sheets and local DB.

    For each matched item:
    1. Load sheet config -> find maks_col for the item's category
    2. Write new maks_sales value to Google Sheets
    3. Update local StockItem.maks_sales

    Args:
        warehouse: Target warehouse name.
        matched_items: Items from select_fulfillment_warehouse().

    Returns:
        {"updated": N, "skipped": N, "errors": [...], "details": [...]}
    """
    from db.sheet_config import load_sheet_config

    result = {"updated": 0, "skipped": 0, "errors": [], "details": []}

    config = load_sheet_config(warehouse)
    if not config:
        result["errors"].append(f"No sheet config for warehouse {warehouse}")
        return result

    from tools.google_sheets import SheetsClient

    # category -> maks_col mapping
    cat_to_maks_col: dict[str, int] = {}
    for section in config.sections:
        if section.maks_col is not None:
            cat_to_maks_col[section.name] = section.maks_col

    spreadsheet_id = get_warehouse_spreadsheet_id(warehouse)
    if not spreadsheet_id:
        result["errors"].append(f"No spreadsheet_id for warehouse {warehouse}")
        return result

    client = SheetsClient()
    sheet_pattern = warehouse.replace("_", " ")
    sheet_name = client.find_active_sheet(
        spreadsheet_id, warehouse_pattern=sheet_pattern,
    )

    session = get_session()
    try:
        for item in matched_items:
            category = item["category"]
            maks_col = cat_to_maks_col.get(category)

            if maks_col is None:
                result["skipped"] += 1
                result["details"].append({
                    "product_name": item["product_name"],
                    "reason": f"no maks_col for category {category}",
                })
                continue

            source_row = item["source_row"]
            if source_row is None:
                result["skipped"] += 1
                result["details"].append({
                    "product_name": item["product_name"],
                    "reason": "no source_row",
                })
                continue

            # Read current value from Sheets (source of truth) to avoid
            # overwriting manual edits or values from previous fulfillments
            # that weren't synced back to local DB.
            try:
                sheet_value = client.get_cell_value(
                    spreadsheet_id, sheet_name, source_row, maks_col,
                )
                old_maks = int(sheet_value) if sheet_value else 0
            except Exception:
                # Fallback to local DB value if Sheets read fails
                old_maks = item["maks_sales"]
            new_maks = old_maks + item["ordered_qty"]

            try:
                client.update_cell(
                    spreadsheet_id, sheet_name, source_row, maks_col, new_maks,
                )
                stock_item = (
                    session.query(StockItem)
                    .filter_by(id=item["stock_item_id"])
                    .first()
                )
                if stock_item:
                    stock_item.maks_sales = new_maks
                    session.flush()

                result["updated"] += 1
                result["details"].append({
                    "product_name": item["product_name"],
                    "old_maks": old_maks,
                    "new_maks": new_maks,
                    "source_row": source_row,
                    "maks_col": maks_col,
                })
                logger.info(
                    "maks_sales updated: %s [%s] %d -> %d (row=%d, col=%d)",
                    item["product_name"], warehouse,
                    old_maks, new_maks, source_row, maks_col,
                )
            except Exception as e:
                result["errors"].append(f"{item['product_name']}: {e}")
                logger.error(
                    "Failed to update maks_sales for %s: %s",
                    item["product_name"], e, exc_info=True,
                )

        session.commit()
    except Exception as e:
        session.rollback()
        result["errors"].append(f"DB commit failed: {e}")
    finally:
        session.close()

    return result


# ── Deterministic order-item source for payment_received ─────────────

class _ItemList(list):
    """List subclass that carries resolved_order_id attribute.

    Non-breaking: existing callers unpack 2-tuple normally.
    Shipping hook reads resolved_order_id via getattr().
    """
    resolved_order_id: str | None = None


def _resolve_item_product_ids(item, catalog_entries):
    """Re-resolve product_ids for a ClientOrderItem with variant_id=NULL.

    Single source of truth for variant_id=NULL resolution:
    - resolve_product_to_catalog(product_name or base_flavor)
    - same-family multi-match → get_preferred_product_id() → [preferred]
    - cross-family → return product_ids as-is (for thread hint)
    - no match → []
    """
    from db.product_resolver import resolve_product_to_catalog
    from db.region_family import is_same_family, get_preferred_product_id

    pn = (item.product_name or "").strip()
    bf = (item.base_flavor or "").strip()
    resolved = resolve_product_to_catalog(pn or bf)
    if resolved.confidence not in ("exact", "high") and pn and pn != bf:
        resolved = resolve_product_to_catalog(bf)
    if resolved.confidence not in ("exact", "high"):
        return []
    product_ids = resolved.product_ids

    if len(product_ids) <= 1:
        return product_ids

    # Same-family → preferred
    id_to_cat = {e["id"]: e["category"] for e in catalog_entries if e["id"] in set(product_ids)}
    if len(id_to_cat) == len(product_ids) and is_same_family(set(id_to_cat.values())):
        preferred = get_preferred_product_id(product_ids, catalog_entries)
        return [preferred] if preferred else product_ids

    # Cross-family → return as-is for thread hint
    return product_ids


def get_order_items_for_fulfillment(
    client_email: str,
    order_id: str | None = None,
    gmail_thread_id: str | None = None,
    gmail_account: str = "default",
) -> tuple[list[dict], list[dict]]:
    """Get order items from ClientOrderItem table for payment_received flow.

    Unified variant_id resolution path:
    - If row has variant_id → use directly (product_ids=[variant_id]).
    - If variant_id is NULL → re-resolve via _resolve_item_product_ids():
      - Single match → ready.
      - Same-family multi-match → preferred id → ready.
      - Cross-family + thread hint available → apply_thread_hint → ready/skip.
      - Otherwise → skip.
    - Any skipped items block the whole order.

    Args:
        client_email: Client email.
        order_id: Optional order ID. If None, uses the most recent order.
        gmail_thread_id: Optional thread ID for thread-backed disambiguation.
        gmail_account: Gmail account label for thread history.

    Returns:
        Tuple of (ready_items, skipped_items).
        ready_items: dicts with base_flavor, product_name, quantity, product_ids.
        skipped_items: dicts with reason and product_ids_count for diagnostics.
        Both empty if no matching order items found.
    """

    session = get_session()
    try:
        email = client_email.lower().strip()
        q = session.query(ClientOrderItem).filter_by(client_email=email)

        actual_order_id = order_id  # tracks resolved order_id for shipping
        if order_id:
            items = q.filter_by(order_id=order_id).all()
        else:
            # Find the most recent order
            latest = q.order_by(ClientOrderItem.created_at.desc()).first()
            if not latest or not latest.order_id:
                return [], []
            actual_order_id = latest.order_id
            items = q.filter_by(order_id=latest.order_id).all()

        if not items:
            return [], []

        ready = _ItemList()
        skipped = []
        catalog_entries = None  # lazy
        thread_messages = None  # lazy — loaded once on first cross-family

        for item in items:
            if item.variant_id is not None:
                ready.append({
                    "base_flavor": item.base_flavor,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "product_ids": [item.variant_id],
                })
                continue

            # --- variant_id=NULL: unified resolution path ---
            if catalog_entries is None:
                from db.catalog import get_catalog_products
                catalog_entries = get_catalog_products()

            product_ids = _resolve_item_product_ids(item, catalog_entries)

            if len(product_ids) == 1:
                ready.append({
                    "base_flavor": item.base_flavor,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "product_ids": product_ids,
                })
                continue

            if len(product_ids) > 1 and gmail_thread_id:
                # Cross-family: apply thread hint
                if thread_messages is None:
                    from db.email_history import get_full_thread_history
                    thread_messages = get_full_thread_history(
                        gmail_thread_id, gmail_account=gmail_account,
                    )
                from db.region_preference import apply_thread_hint
                from db.region_family import get_preferred_product_id
                temp = {
                    "product_ids": list(product_ids),
                    "base_flavor": (item.base_flavor or "").strip(),
                }
                apply_thread_hint([temp], thread_messages, catalog_entries)
                narrowed = temp.get("product_ids", [])

                if len(narrowed) < len(product_ids) and narrowed:
                    preferred = get_preferred_product_id(narrowed, catalog_entries)
                    vid = preferred or narrowed[0]
                    ready.append({
                        "base_flavor": item.base_flavor,
                        "product_name": item.product_name,
                        "quantity": item.quantity,
                        "product_ids": [vid],
                    })
                    logger.info(
                        "Fulfillment: resolved from thread for %s/%s -> pid=%d",
                        email, item.base_flavor, vid,
                    )
                    continue

            # Not resolved → skip with structured reason
            reason = "ambiguous_variant" if len(product_ids) > 1 else "no_match"
            skipped.append({
                "base_flavor": item.base_flavor,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "product_ids": product_ids,
                "product_ids_count": len(product_ids),
                "reason": reason,
            })
            logger.warning(
                "Fulfillment read-path: variant_id NULL for %s/%s/%s "
                "— %s (product_ids_count=%d, skipped)",
                email, order_id, item.base_flavor, reason, len(product_ids),
            )

        # Any skipped items block the whole order
        if skipped:
            logger.warning(
                "Fulfillment blocked: %d/%d items unresolved for %s "
                "(whole order blocked)",
                len(skipped), len(ready) + len(skipped), email,
            )
            return [], skipped

        ready.resolved_order_id = actual_order_id
        return ready, skipped
    finally:
        session.close()


