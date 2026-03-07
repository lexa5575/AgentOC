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

from sqlalchemy.exc import IntegrityError

from db.models import ClientOrderItem, FulfillmentEvent, StockItem, get_session
from db.warehouse_geo import resolve_warehouse_from_address

logger = logging.getLogger(__name__)

# ── Fulfillment statuses ─────────────────────────────────────────────

STATUS_UPDATED = "updated"
STATUS_SKIPPED_SPLIT = "skipped_split"
STATUS_SKIPPED_UNRESOLVED = "skipped_unresolved_order"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
STATUS_ERROR = "error"


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

    Shared by _try_warehouse and _collect_split_breakdown.
    """
    if product_ids:
        return (
            session.query(StockItem)
            .filter(
                StockItem.product_id.in_(product_ids),
                StockItem.warehouse == warehouse,
            )
            .all()
        )
    return (
        session.query(StockItem)
        .filter(
            StockItem.product_name.ilike(base_flavor),
            StockItem.warehouse == warehouse,
        )
        .all()
    )


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

        total_available = sum(e.quantity for e in entries if e.quantity > 0)
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
            total = sum(e.quantity for e in entries if e.quantity > 0) if entries else 0
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
    from tools.google_sheets import SheetsClient

    result = {"updated": 0, "skipped": 0, "errors": [], "details": []}

    config = load_sheet_config(warehouse)
    if not config:
        result["errors"].append(f"No sheet config for warehouse {warehouse}")
        return result

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

def get_order_items_for_fulfillment(
    client_email: str,
    order_id: str | None = None,
) -> list[dict]:
    """Get order items from ClientOrderItem table for payment_received flow.

    Re-resolves product_ids via catalog resolver for accurate stock matching.
    Does NOT depend on conversation_state.

    Args:
        client_email: Client email.
        order_id: Optional order ID. If None, uses the most recent order.

    Returns:
        List of dicts with base_flavor, product_name, quantity, product_ids.
        Empty list if no matching order items found.
    """
    session = get_session()
    try:
        email = client_email.lower().strip()
        q = session.query(ClientOrderItem).filter_by(client_email=email)

        if order_id:
            items = q.filter_by(order_id=order_id).all()
        else:
            # Find the most recent order
            latest = q.order_by(ClientOrderItem.created_at.desc()).first()
            if not latest or not latest.order_id:
                return []
            items = q.filter_by(order_id=latest.order_id).all()

        if not items:
            return []

        from db.product_resolver import resolve_product_to_catalog

        result = []
        for item in items:
            resolved = resolve_product_to_catalog(item.base_flavor)
            product_ids = (
                resolved.product_ids
                if resolved.confidence in ("exact", "high")
                else []
            )
            result.append({
                "base_flavor": item.base_flavor,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "product_ids": product_ids,
            })

        return result
    finally:
        session.close()


# ── Idempotency ──────────────────────────────────────────────────────

def is_duplicate_fulfillment(
    client_email: str,
    order_id: str | None,
    trigger_type: str,
    gmail_message_id: str | None = None,
) -> bool:
    """Check if a fulfillment event was already recorded.

    Checks by:
    1. gmail_message_id + trigger_type (if gmail_message_id provided)
    2. client_email + order_id + trigger_type (fallback)
    """
    session = get_session()
    try:
        if gmail_message_id:
            existing = (
                session.query(FulfillmentEvent)
                .filter_by(
                    gmail_message_id=gmail_message_id,
                    trigger_type=trigger_type,
                )
                .first()
            )
            if existing:
                return True

        if order_id:
            existing = (
                session.query(FulfillmentEvent)
                .filter_by(
                    client_email=client_email.lower().strip(),
                    order_id=order_id,
                    trigger_type=trigger_type,
                )
                .first()
            )
            if existing:
                return True

        return False
    finally:
        session.close()


STATUS_PROCESSING = "processing"


def claim_fulfillment_event(
    client_email: str,
    order_id: str | None,
    trigger_type: str,
    status: str,
    warehouse: str | None = None,
    gmail_message_id: str | None = None,
    details: dict | None = None,
) -> dict:
    """Atomically claim a fulfillment slot via INSERT.

    The INSERT is the source of truth for idempotency. DB unique constraints
    (gmail_message_id+trigger_type, client_email+order_id+trigger_type)
    prevent duplicate increments even under concurrent execution.

    For the "updated" path, callers should claim with status="processing",
    perform the Sheets write, then call finalize_fulfillment_event() to set
    the final status ("updated" or "error").

    Returns:
        {"created": bool, "duplicate": bool, "error": str|None, "event_id": int|None}
    """
    session = get_session()
    try:
        event = FulfillmentEvent(
            client_email=client_email.lower().strip(),
            order_id=order_id,
            gmail_message_id=gmail_message_id,
            trigger_type=trigger_type,
            status=status,
            warehouse=warehouse,
            details_json=json.dumps(details) if details else None,
        )
        session.add(event)
        session.commit()
        event_id = event.id
        logger.info(
            "Fulfillment event claimed: %s/%s/%s -> %s (id=%s)",
            client_email, order_id, trigger_type, status, event_id,
        )
        return {"created": True, "duplicate": False, "error": None, "event_id": event_id}
    except IntegrityError:
        session.rollback()
        logger.info(
            "Fulfillment duplicate blocked by DB: %s/%s/%s",
            client_email, order_id, trigger_type,
        )
        return {"created": False, "duplicate": True, "error": None, "event_id": None}
    except Exception as e:
        session.rollback()
        logger.error("Failed to claim fulfillment event: %s", e)
        return {"created": False, "duplicate": False, "error": str(e), "event_id": None}
    finally:
        session.close()


def finalize_fulfillment_event(
    event_id: int,
    status: str,
    details: dict | None = None,
) -> bool:
    """Update a claimed fulfillment event to its final status.

    Called after increment_maks_sales to set "updated" or "error".
    Returns True on success.
    """
    session = get_session()
    try:
        event = session.query(FulfillmentEvent).filter_by(id=event_id).first()
        if not event:
            logger.error("finalize_fulfillment_event: event_id=%s not found", event_id)
            return False
        event.status = status
        if details is not None:
            event.details_json = json.dumps(details)
        session.commit()
        logger.info(
            "Fulfillment event finalized: id=%s -> %s", event_id, status,
        )
        return True
    except Exception as e:
        session.rollback()
        logger.error("Failed to finalize fulfillment event %s: %s", event_id, e)
        return False
    finally:
        session.close()
