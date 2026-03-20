"""Fulfillment trigger — best-effort maks_sales update after draft creation.

Called from pipeline.py ONLY after successful Gmail draft creation.
Never blocks the pipeline. Errors are logged but don't affect the reply.

Trigger conditions:
- ("new_order", "postpay")  -> ships immediately
- ("payment_received", "prepay") -> ships after payment confirmed

NOT triggered for ("new_order", "prepay") — just sends Zelle request.

Claim lifecycle:
- For "updated" path: claim with status="processing" -> increment -> finalize as "updated" or "error"
- For skip paths: claim with final status directly (skipped_split, skipped_unresolved)
- Duplicate attempts blocked by DB unique constraints at claim stage
- On any exception after claim: finalize as "error" (never left as "processing")
"""

import logging
from os import getenv

from db.fulfillment import (
    STATUS_BLOCKED_AMBIGUOUS,
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_SKIPPED_OUT_OF_STOCK,
    STATUS_SKIPPED_SPLIT,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_UPDATED,
    claim_fulfillment_event,
    finalize_fulfillment_event,
    get_order_items_for_fulfillment,
    increment_maks_sales,
    is_duplicate_fulfillment,
    select_fulfillment_warehouse,
)

logger = logging.getLogger(__name__)


def try_fulfillment(
    classification,
    result: dict,
    gmail_message_id: str | None = None,
) -> None:
    """Best-effort fulfillment + maks_sales update.

    Must be called ONLY after successful Gmail draft creation.
    Never raises — all errors are caught and logged.
    Attaches result["fulfillment"] for admin visibility.

    Order item sources (deterministic, no conversation_state):
    - new_order_postpay: result["_stock_check_items"] (from pipeline stock check)
    - payment_received_prepay — 3 branches:
      1. payment_items_unresolved=True → SKIPPED_UNRESOLVED, no DB fallback
      2. _stock_check_items present → use directly (dual-intent, PAY-* order_id)
      3. Normal → ClientOrderItem table via order_id; fallback to latest order
         only when has_explicit_order_id=False
    """
    trigger_type = None
    event_id = None  # Track claimed event for exception safety
    try:
        client = result.get("client_data") or {}
        payment_type = client.get("payment_type", "unknown")
        situation = result.get("situation", getattr(classification, "situation", ""))

        # Determine trigger type
        if situation == "new_order" and payment_type == "postpay":
            trigger_type = "new_order_postpay"
        elif situation == "payment_received" and payment_type == "prepay":
            trigger_type = "payment_received_prepay"
        elif (
            result.get("effective_situation") == "new_order"
            and payment_type == "postpay"
        ):
            # OOS-derived effective new_order — source gate (plan §7.4)
            _TRUSTED_FULFILLMENT_SOURCES = {"thread_extraction", "pending_oos"}
            source = result.get("confirmation_source")
            if source not in _TRUSTED_FULFILLMENT_SOURCES:
                logger.info(
                    "Fulfillment skipped for %s: effective_situation=new_order "
                    "but source=%s not trusted",
                    classification.client_email, source,
                )
                return
            trigger_type = "new_order_postpay"
        else:
            return  # Not a fulfillment trigger

        # Normalize IDs: empty/whitespace-only -> None
        raw_order_id = getattr(classification, "order_id", None) or ""
        order_id = raw_order_id.strip() or None
        _resolved_oid = order_id  # default; overwritten only in Branch 3
        msg_id = gmail_message_id or None
        client_email = classification.client_email

        # 0.5 Phase 4.1: missing order_id blocks new_order_postpay (rule §4.5)
        if trigger_type == "new_order_postpay" and order_id is None:
            blocked_details = {
                "v": 2,
                "reason": "missing_order_id_new_order_postpay",
            }
            result["fulfillment"] = {
                "status": STATUS_BLOCKED_AMBIGUOUS,
                "warehouse": None,
                "trigger_type": trigger_type,
                "reason": "missing_order_id_new_order_postpay",
            }
            claim_fulfillment_event(
                client_email=client_email,
                order_id=None,
                trigger_type=trigger_type,
                status=STATUS_BLOCKED_AMBIGUOUS,
                gmail_message_id=msg_id,
                details=blocked_details,
            )
            logger.warning(
                "Fulfillment blocked (missing order_id) for %s: "
                "new_order_postpay requires order_id",
                client_email,
            )
            return

        # 1. Get order items (deterministic — no conversation_state)
        skipped_items = []
        if trigger_type == "new_order_postpay":
            stock_items = result.get("_stock_check_items")
        else:
            # payment_received_prepay — 3 branches:

            # Branch 1: Dual-intent resolve FAILED → block, NO DB fallback
            if result.get("payment_items_unresolved"):
                result["fulfillment"] = {
                    "status": STATUS_SKIPPED_UNRESOLVED,
                    "warehouse": None,
                    "trigger_type": trigger_type,
                    "reason": "payment_items_unresolved",
                }
                claim_fulfillment_event(
                    client_email=client_email,
                    order_id=order_id,
                    trigger_type=trigger_type,
                    status=STATUS_SKIPPED_UNRESOLVED,
                    gmail_message_id=msg_id,
                    details={"reason": "payment_items_unresolved"},
                )
                logger.warning(
                    "Fulfillment skipped for %s: dual-intent items unresolved, "
                    "no DB fallback",
                    client_email,
                )
                return

            # Branch 2: Dual-intent resolved → use freshly resolved items
            if result.get("_stock_check_items"):
                stock_items = result["_stock_check_items"]

            # Branch 3: Normal payment_received → read from ClientOrderItem table
            else:
                gmail_thread_id = result.get("gmail_thread_id")
                gmail_account = result.get("gmail_account", "default")
                stock_items, skipped_items = get_order_items_for_fulfillment(
                    client_email, order_id,
                    gmail_thread_id=gmail_thread_id,
                    gmail_account=gmail_account,
                )
                _resolved_oid = getattr(stock_items, "resolved_order_id", None) or order_id
                # Fallback to latest order ONLY when order_id is NOT explicit.
                # Explicit order_id not found → don't touch wrong order.
                if not stock_items and not skipped_items and order_id is not None:
                    # Defensive: compute explicit flag if pipeline didn't set it
                    _AUTO_PREFIXES = ("PAY-", "AUTO-")
                    is_explicit = result.get(
                        "has_explicit_order_id",
                        not order_id.startswith(_AUTO_PREFIXES),
                    )
                    if is_explicit:
                        logger.warning(
                            "Fulfillment: explicit order_id=%s not found in DB "
                            "for %s — skipping latest-order fallback",
                            order_id, client_email,
                        )
                    else:
                        stock_items, skipped_items = get_order_items_for_fulfillment(
                            client_email, None,
                            gmail_thread_id=gmail_thread_id,
                            gmail_account=gmail_account,
                        )
                        _resolved_oid = getattr(stock_items, "resolved_order_id", None) or order_id

        # 1.1 Skipped items gate: if any items couldn't be resolved,
        # the whole order is blocked.
        if skipped_items:
            blocked_flavors = [s["base_flavor"] for s in skipped_items]
            # Read reason from skipped item (set by unified path).
            # Fallback: product_ids_count for backward compat.
            has_ambiguous = any(
                s.get("reason") == "ambiguous_variant"
                or (s.get("reason") is None and s.get("product_ids_count") is not None)
                for s in skipped_items
            )
            reason = "ambiguous_variant" if has_ambiguous else "unresolved_variant_strict"
            blocked_details = {
                "v": 2,
                "reason": reason,
                "skipped_items": [
                    {
                        "base_flavor": s["base_flavor"],
                        "product_ids_count": s.get("product_ids_count", 0),
                        "reason": s.get("reason", "unknown"),
                    }
                    for s in skipped_items
                ],
            }
            result["fulfillment"] = {
                "status": STATUS_BLOCKED_AMBIGUOUS,
                "warehouse": None,
                "trigger_type": trigger_type,
                "ambiguous_flavors": blocked_flavors,
                "reason": reason,
            }
            claim_fulfillment_event(
                client_email=client_email,
                order_id=order_id,
                trigger_type=trigger_type,
                status=STATUS_BLOCKED_AMBIGUOUS,
                gmail_message_id=msg_id,
                details=blocked_details,
            )
            logger.warning(
                "Fulfillment blocked (%s) for %s: %s",
                reason, client_email, blocked_flavors,
            )
            return

        if not stock_items:
            result["fulfillment"] = {
                "status": STATUS_SKIPPED_UNRESOLVED,
                "warehouse": None,
                "trigger_type": trigger_type,
            }
            claim_fulfillment_event(
                client_email=client_email,
                order_id=order_id,
                trigger_type=trigger_type,
                status=STATUS_SKIPPED_UNRESOLVED,
                gmail_message_id=msg_id,
            )
            return

        # 1.5 Phase 3 ambiguity gate: block fulfillment if pipeline
        # flagged ambiguous variants (plan §9.6, rule §4.3).
        if result.get("fulfillment_blocked"):
            ambiguous = result.get("ambiguous_flavors", [])
            blocked_details = {
                "v": 2,
                "reason": "ambiguous_variant",
                "skipped_items": [
                    {"base_flavor": bf, "product_ids_count": "multi"}
                    for bf in ambiguous
                ],
            }
            result["fulfillment"] = {
                "status": STATUS_BLOCKED_AMBIGUOUS,
                "warehouse": None,
                "trigger_type": trigger_type,
                "ambiguous_flavors": ambiguous,
                "reason": "ambiguous_variant",
            }
            claim_fulfillment_event(
                client_email=client_email,
                order_id=order_id,
                trigger_type=trigger_type,
                status=STATUS_BLOCKED_AMBIGUOUS,
                gmail_message_id=msg_id,
                details=blocked_details,
            )
            logger.warning(
                "Fulfillment blocked (ambiguous variants) for %s: %s",
                client_email, ambiguous,
            )
            return

        # 2. Fast-path idempotency pre-check
        if is_duplicate_fulfillment(client_email, order_id, trigger_type, msg_id):
            result["fulfillment"] = {
                "status": STATUS_SKIPPED_DUPLICATE,
                "warehouse": None,
                "trigger_type": trigger_type,
            }
            return

        # 3. Get client address
        city_state_zip = (
            getattr(classification, "customer_city_state_zip", "")
            or client.get("city_state_zip", "")
        )

        # 4. Select warehouse
        fulfillment = select_fulfillment_warehouse(stock_items, city_state_zip)
        status = fulfillment["status"]
        wh = fulfillment["warehouse"]

        # 4b. Handle infra error (no active warehouses)
        if status == STATUS_ERROR and fulfillment.get("reason") == "no_active_warehouses":
            claim = claim_fulfillment_event(
                client_email=client_email,
                order_id=order_id,
                trigger_type=trigger_type,
                status=STATUS_ERROR,
                warehouse=None,
                gmail_message_id=msg_id,
                details={"v": 2, "reason": "no_active_warehouses"},
            )
            result["fulfillment"] = {
                "status": STATUS_ERROR,
                "warehouse": None,
                "trigger_type": trigger_type,
                "error": "no active warehouses configured",
            }
            if claim.get("error"):
                result["fulfillment"]["error"] += f"; claim error: {claim['error']}"
            elif claim.get("created"):
                # First time seeing this — alert operator
                from utils.telegram import send_telegram
                send_telegram(
                    "\u26a0\ufe0f <b>Fulfillment blocked</b>\n\n"
                    f"Client: {client_email}\nOrder: {order_id}\n"
                    "Reason: no active warehouses configured.\n"
                    "Check STOCK_WAREHOUSES env var."
                )
            # duplicate or retried → no alert (already sent on first occurrence)
            return

        # 5. Atomic claim
        # For "updated" path: claim as "processing" first, finalize after Sheets write
        # For skip paths: claim with final status directly
        claim_status = STATUS_PROCESSING if status == STATUS_UPDATED else status
        claim = claim_fulfillment_event(
            client_email=client_email,
            order_id=order_id,
            trigger_type=trigger_type,
            status=claim_status,
            warehouse=wh,
            gmail_message_id=msg_id,
            details=(
                {"matched_count": len(fulfillment["matched_items"])}
                if fulfillment.get("matched_items")
                else {
                    "v": 2,
                    "split_breakdown": fulfillment.get("split_breakdown"),
                    "tried_warehouses": fulfillment.get("tried_warehouses", []),
                }
            ),
        )

        if claim["duplicate"]:
            result["fulfillment"] = {
                "status": STATUS_SKIPPED_DUPLICATE,
                "warehouse": None,
                "trigger_type": trigger_type,
            }
            return

        if claim["error"]:
            result["fulfillment"] = {
                "status": STATUS_ERROR,
                "warehouse": None,
                "trigger_type": trigger_type,
                "error": claim["error"],
            }
            return

        event_id = claim["event_id"]

        # 6. Build fulfillment result
        result["fulfillment"] = {
            "status": status,
            "warehouse": wh,
            "trigger_type": trigger_type,
            "tried_warehouses": fulfillment.get("tried_warehouses", []),
            "split_breakdown": fulfillment.get("split_breakdown"),
        }

        # 7. If single warehouse found -> increment maks_sales
        if status == STATUS_UPDATED and fulfillment["matched_items"]:
            update_result = increment_maks_sales(wh, fulfillment["matched_items"])
            result["fulfillment"]["update_result"] = update_result

            # Finalize: check if increment actually succeeded
            has_errors = bool(update_result.get("errors"))
            expected_updates = len(fulfillment["matched_items"])
            actual_updates = update_result.get("updated", 0)
            actual_skipped = update_result.get("skipped", 0)

            if has_errors or (actual_updates == 0 and actual_skipped < expected_updates):
                # Increment failed or produced no updates
                final_status = STATUS_ERROR
                result["fulfillment"]["status"] = STATUS_ERROR
                error_detail = "; ".join(update_result.get("errors", [])) or "no items updated"
                result["fulfillment"]["error"] = error_detail
                logger.error(
                    "Fulfillment increment failed: %s warehouse=%s errors=%s",
                    client_email, wh, update_result.get("errors"),
                )
            else:
                final_status = STATUS_UPDATED
                logger.info(
                    "Fulfillment OK: %s warehouse=%s updated=%d skipped=%d",
                    client_email, wh, actual_updates, actual_skipped,
                )

            # Finalize the DB event
            finalized = finalize_fulfillment_event(
                event_id,
                status=final_status,
                details=update_result,
            )
            if not finalized:
                logger.error(
                    "Failed to finalize fulfillment event %s to %s",
                    event_id, final_status,
                )
                result["fulfillment"]["status"] = STATUS_ERROR
                result["fulfillment"]["error"] = (
                    result["fulfillment"].get("error", "")
                    + "; finalize_fulfillment_event failed"
                ).lstrip("; ")

            # Shipping job hook: create PirateShip auto-fill job
            if finalized and final_status == STATUS_UPDATED and bool(getenv("SHIPPING_API_TOKEN", "")):
                _saved_event_id = event_id  # capture before zeroing
                try:
                    from db.clients import get_client as _get_client
                    from db.shipping import create_shipping_job, get_order_shipping_address
                    from db.stock import get_product_type

                    # Address source chain:
                    # 1. OrderShippingAddress by resolved_order_id (preferred)
                    # 2. Client record (both street + csz must be non-empty)
                    # 3. Skip + Telegram alert
                    _addr = get_order_shipping_address(client_email, _resolved_oid)
                    _addr_source = "order_snapshot"

                    if not _addr:
                        _fresh = _get_client(client_email) or {}
                        _s = _fresh.get("street", "")
                        _c = _fresh.get("city_state_zip", "")
                        _n = _fresh.get("name", "")
                        if _s and _c and _n:
                            _addr = {"client_name": _n, "street": _s, "city_state_zip": _c}
                            _addr_source = "client_record"

                    if _addr:
                        ps_items = [{
                            "base_flavor": m["base_flavor"],
                            "quantity": m["ordered_qty"],
                            "product_type": get_product_type(m["base_flavor"]),
                        } for m in fulfillment["matched_items"]]

                        create_shipping_job(
                            fulfillment_event_id=_saved_event_id,
                            client_email=client_email,
                            order_id=_resolved_oid,
                            client_name=_addr["client_name"],
                            street=_addr["street"],
                            city_state_zip=_addr["city_state_zip"],
                            address_source=_addr_source,
                            warehouse=wh,
                            items=ps_items,
                        )
                    else:
                        from utils.telegram import send_telegram
                        logger.warning("No address for shipping job: %s order=%s", client_email, _resolved_oid)
                        send_telegram(
                            f"⚠️ <b>Shipping job skipped</b>\n"
                            f"Client: {client_email}\nOrder: {_resolved_oid}\n"
                            f"No address found. Fill PirateShip manually.",
                        )
                except Exception:
                    logger.exception("Failed to create shipping job for %s", client_email)

            # Clear event_id — finalized successfully, no cleanup needed
            event_id = None

        elif status in (STATUS_SKIPPED_SPLIT, STATUS_SKIPPED_OUT_OF_STOCK):
            # Skip paths claim with final status directly, no finalize needed
            event_id = None
            logger.warning(
                "%s for %s — maks_sales NOT updated",
                status, client_email,
            )

    except Exception as e:
        logger.error("Fulfillment trigger failed: %s", e, exc_info=True)
        result["fulfillment"] = {
            "status": STATUS_ERROR,
            "warehouse": None,
            "trigger_type": trigger_type or "unknown",
            "error": str(e),
        }
        # Safety net: if we claimed an event but crashed before finalize,
        # don't leave it stuck as "processing"
        if event_id is not None:
            try:
                finalize_fulfillment_event(
                    event_id,
                    status=STATUS_ERROR,
                    details={"exception": str(e)},
                )
            except Exception:
                logger.error(
                    "Failed to finalize event %s in exception handler", event_id,
                )
