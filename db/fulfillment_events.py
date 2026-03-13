"""Fulfillment Event Lifecycle
------------------------------

FulfillmentEvent table management: status constants, idempotency checks,
claiming, retrying, and finalizing fulfillment events.
"""

import json
import logging

from sqlalchemy.exc import IntegrityError

from db.models import FulfillmentEvent, get_session

logger = logging.getLogger(__name__)


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
