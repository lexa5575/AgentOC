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


def _use_family_fulfillment() -> bool:
    """Feature flag for region-family expansion in fulfillment queries.

    Default=true. Set USE_FAMILY_FULFILLMENT=false/0/no/off to disable.
    """
    return getenv("USE_FAMILY_FULFILLMENT", "true").lower() not in ("false", "0", "no", "off")


# ── Fulfillment statuses ─────────────────────────────────────────────

STATUS_UPDATED = "updated"
STATUS_SKIPPED_SPLIT = "skipped_split"
STATUS_SKIPPED_UNRESOLVED = "skipped_unresolved_order"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
STATUS_BLOCKED_AMBIGUOUS = "blocked_ambiguous_variant"
STATUS_ERROR = "error"
STATUS_PROCESSING = "processing"

# Statuses that block retry (success + in-progress + duplicate-protection)
_BLOCKING_STATUSES = frozenset({STATUS_UPDATED, STATUS_PROCESSING, STATUS_SKIPPED_DUPLICATE})

# Statuses that allow retry (retriable business failures)
_RETRIABLE_STATUSES = frozenset({
    STATUS_SKIPPED_SPLIT,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_BLOCKED_AMBIGUOUS,
    STATUS_ERROR,
})


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
            max(e.quantity - (e.maks_sales or 0), 0)
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
            total = sum(max(e.quantity - (e.maks_sales or 0), 0) for e in entries) if entries else 0
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
) -> tuple[list[dict], list[dict]]:
    """Get order items from ClientOrderItem table for payment_received flow.

    Phase 4 variant_id-first read path:
    - If row has variant_id → use directly (product_ids=[variant_id]), no re-resolve.
    - If variant_id is NULL and REQUIRE_VARIANT_ID=false → legacy re-resolve.
    - If variant_id is NULL and REQUIRE_VARIANT_ID=true → add to skipped list.

    Blocking rules (Phase 4.1+):
    - Strict mode (REQUIRE_VARIANT_ID=true): ANY skipped → ([], skipped).
      Skipped items have no product_ids_count field.
    - Non-strict mode: legacy re-resolve with len(product_ids)!=1 → skipped.
      Skipped items carry product_ids_count for diagnostics.
    - In BOTH modes, any skipped items block the whole order.

    Args:
        client_email: Client email.
        order_id: Optional order ID. If None, uses the most recent order.

    Returns:
        Tuple of (ready_items, skipped_items).
        ready_items: dicts with base_flavor, product_name, quantity, product_ids.
        skipped_items: dicts for items that could not be resolved.
          Strict-mode skipped items: no product_ids_count field.
          Legacy-ambiguous skipped items: product_ids_count field present.
        Both empty if no matching order items found.
    """
    strict = getenv("REQUIRE_VARIANT_ID", "").lower() in ("true", "1", "yes")

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
                return [], []
            items = q.filter_by(order_id=latest.order_id).all()

        if not items:
            return [], []

        ready = []
        skipped = []
        catalog_entries = None  # lazy-loaded for legacy re-resolve

        for item in items:
            if item.variant_id is not None:
                # Phase 4: variant_id exists → direct lookup, no re-resolve
                ready.append({
                    "base_flavor": item.base_flavor,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "product_ids": [item.variant_id],
                })
            elif strict:
                # Strict mode: NULL variant_id → skip (do not re-resolve)
                skipped.append({
                    "base_flavor": item.base_flavor,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "product_ids": [],
                })
                logger.warning(
                    "Fulfillment read-path: variant_id NULL for %s/%s/%s "
                    "(strict mode, skipped)",
                    email, order_id, item.base_flavor,
                )
            else:
                # Legacy path: re-resolve from text (temporary, REQUIRE_VARIANT_ID=false)
                # Try product_name first (contains region), fallback to base_flavor
                from db.product_resolver import resolve_product_to_catalog

                pn = (item.product_name or "").strip()
                bf = (item.base_flavor or "").strip()
                resolve_name = pn or bf

                resolved = resolve_product_to_catalog(resolve_name)
                if resolved.confidence not in ("exact", "high") and pn and pn != bf:
                    resolved = resolve_product_to_catalog(bf)

                product_ids = (
                    resolved.product_ids
                    if resolved.confidence in ("exact", "high")
                    else []
                )

                if len(product_ids) == 1:
                    ready.append({
                        "base_flavor": item.base_flavor,
                        "product_name": item.product_name,
                        "quantity": item.quantity,
                        "product_ids": product_ids,
                    })
                elif len(product_ids) > 1:
                    # Same-family multi-match → pick preferred, cross-family → skip
                    from db.region_family import get_preferred_product_id, is_same_family

                    if catalog_entries is None:
                        from db.catalog import get_catalog_products
                        catalog_entries = get_catalog_products()
                    id_to_cat = {
                        e["id"]: e["category"]
                        for e in catalog_entries
                        if e["id"] in set(product_ids)
                    }
                    # Fail-closed: not all pids found → skip
                    if len(id_to_cat) != len(product_ids):
                        skipped.append({
                            "base_flavor": item.base_flavor,
                            "product_name": item.product_name,
                            "quantity": item.quantity,
                            "product_ids": product_ids,
                            "product_ids_count": len(product_ids),
                        })
                    elif is_same_family(set(id_to_cat.values())):
                        preferred = get_preferred_product_id(product_ids, catalog_entries)
                        ready.append({
                            "base_flavor": item.base_flavor,
                            "product_name": item.product_name,
                            "quantity": item.quantity,
                            "product_ids": [preferred] if preferred else product_ids,
                        })
                    else:
                        skipped.append({
                            "base_flavor": item.base_flavor,
                            "product_name": item.product_name,
                            "quantity": item.quantity,
                            "product_ids": product_ids,
                            "product_ids_count": len(product_ids),
                        })
                        logger.warning(
                            "Fulfillment read-path: legacy re-resolve for %s/%s/%s "
                            "cross-family %d product_ids (skipped)",
                            email, order_id, item.base_flavor, len(product_ids),
                        )
                else:
                    skipped.append({
                        "base_flavor": item.base_flavor,
                        "product_name": item.product_name,
                        "quantity": item.quantity,
                        "product_ids": product_ids,
                        "product_ids_count": len(product_ids),
                    })
                    logger.warning(
                        "Fulfillment read-path: legacy re-resolve for %s/%s/%s "
                        "returned %d product_ids (skipped)",
                        email, order_id, item.base_flavor, len(product_ids),
                    )

        # Hard block: if strict mode and any skipped → block whole order (rule §4.4)
        if strict and skipped:
            logger.warning(
                "Fulfillment blocked: %d/%d items missing variant_id for %s "
                "(strict mode, whole order blocked)",
                len(skipped), len(ready) + len(skipped), email,
            )
            return [], skipped

        # Phase 4.1: even in non-strict mode, ambiguous items block whole order (rule §4.3)
        if skipped:
            logger.warning(
                "Fulfillment blocked: %d/%d items ambiguous after legacy re-resolve "
                "for %s (whole order blocked)",
                len(skipped), len(ready) + len(skipped), email,
            )
            return [], skipped

        return ready, skipped
    finally:
        session.close()


# ── Idempotency ──────────────────────────────────────────────────────

def is_duplicate_fulfillment(
    client_email: str,
    order_id: str | None,
    trigger_type: str,
    gmail_message_id: str | None = None,
) -> bool:
    """Check if a fulfillment event with blocking status already exists.

    Blocks if status in {updated, processing} — prevents double-increment and race.
    Allows retry if status in {skipped_*, error} — retriable business failures.
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
            if existing and existing.status in _BLOCKING_STATUSES:
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
            if existing and existing.status in _BLOCKING_STATUSES:
                return True

        return False
    finally:
        session.close()


# ── details_json v2 helpers ──────────────────────────────────────────

def _ensure_v2(details: dict | None) -> dict | None:
    """Stamp details payload with v=2 if not already versioned.

    - None → None (no details to stamp)
    - dict without "v" → adds "v": 2
    - dict with "v" → left unchanged
    """
    if details is None:
        return None
    if "v" not in details:
        details["v"] = 2
    return details


def parse_details_json(raw: str | None) -> dict:
    """Parse details_json with backward-compatible version detection.

    - None / empty → {"version": 1}
    - JSON without "v" → parsed dict + version=1
    - JSON with "v" → parsed dict + version=<v>

    Returns:
        Parsed dict with "version" key always set.
    """
    if not raw:
        return {"version": 1}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"version": 1, "_raw": raw}
    if not isinstance(data, dict):
        return {"version": 1, "_raw": raw}
    data["version"] = data.pop("v", 1)
    return data


def _find_existing_event_id(session, client_email, order_id, trigger_type, gmail_message_id):
    """Find existing fulfillment event id by unique key. Returns id or None."""
    if gmail_message_id:
        ev = (
            session.query(FulfillmentEvent.id)
            .filter_by(gmail_message_id=gmail_message_id, trigger_type=trigger_type)
            .first()
        )
        if ev:
            return ev[0]
    if order_id:
        ev = (
            session.query(FulfillmentEvent.id)
            .filter_by(
                client_email=client_email.lower().strip(),
                order_id=order_id,
                trigger_type=trigger_type,
            )
            .first()
        )
        if ev:
            return ev[0]
    return None


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
        stamped = _ensure_v2(details)
        event = FulfillmentEvent(
            client_email=client_email.lower().strip(),
            order_id=order_id,
            gmail_message_id=gmail_message_id,
            trigger_type=trigger_type,
            status=status,
            warehouse=warehouse,
            details_json=json.dumps(stamped) if stamped else None,
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
        # Atomic retry: UPDATE existing event if its status is retriable
        session2 = get_session()
        try:
            event_id = _find_existing_event_id(
                session2, client_email, order_id, trigger_type, gmail_message_id,
            )
            if event_id is not None:
                rows = (
                    session2.query(FulfillmentEvent)
                    .filter(
                        FulfillmentEvent.id == event_id,
                        FulfillmentEvent.status.in_(_RETRIABLE_STATUSES),
                    )
                    .update(
                        {
                            FulfillmentEvent.status: status,
                            FulfillmentEvent.warehouse: warehouse,
                            FulfillmentEvent.details_json: (
                                json.dumps(stamped) if stamped else None
                            ),
                        },
                        synchronize_session=False,
                    )
                )
                session2.commit()
                if rows == 1:
                    logger.info(
                        "Fulfillment event RETRIED: id=%s → %s", event_id, status,
                    )
                    return {
                        "created": False, "duplicate": False, "retried": True,
                        "error": None, "event_id": event_id,
                    }
                logger.info(
                    "Fulfillment event NOT retriable: id=%s (rows=%d)",
                    event_id, rows,
                )
            else:
                logger.info(
                    "Fulfillment duplicate blocked by DB: %s/%s/%s",
                    client_email, order_id, trigger_type,
                )
        except Exception as retry_err:
            session2.rollback()
            logger.warning("Fulfillment retry failed: %s", retry_err)
        finally:
            session2.close()
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
            event.details_json = json.dumps(_ensure_v2(details))
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
