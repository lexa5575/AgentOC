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

from db.fulfillment import (
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_SKIPPED_DUPLICATE,
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
    - payment_received_prepay: ClientOrderItem table via classification.order_id,
      with fallback to latest order if order_id lookup returns empty
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
        else:
            return  # Not a fulfillment trigger

        # Normalize IDs: empty string -> None
        order_id = getattr(classification, "order_id", None) or None
        msg_id = gmail_message_id or None
        client_email = classification.client_email

        # 1. Get order items (deterministic — no conversation_state)
        if trigger_type == "new_order_postpay":
            stock_items = result.get("_stock_check_items")
        else:
            # payment_received: deterministic from ClientOrderItem table
            # primary: classification.order_id
            stock_items = get_order_items_for_fulfillment(
                client_email, order_id,
            )
            # fallback: if order_id didn't match, try latest order
            if not stock_items and order_id is not None:
                stock_items = get_order_items_for_fulfillment(
                    client_email, None,
                )

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
                else None
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

            # Clear event_id — finalized successfully, no cleanup needed
            event_id = None

        elif status == STATUS_SKIPPED_SPLIT:
            # Skip paths claim with final status directly, no finalize needed
            event_id = None
            logger.warning(
                "Split warehouse for %s — maks_sales NOT updated",
                client_email,
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
