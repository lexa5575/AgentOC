"""
Shipping Job Management
-----------------------

PirateShip auto-fill job queue: address snapshot CRUD, city/state/zip parsing,
package selection, and atomic claim/complete/fail operations.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.models import OrderShippingAddress, ShippingJob, get_session
from db.warehouse_geo import STATE_NAME_TO_CODE, STATE_TO_WAREHOUSE

logger = logging.getLogger(__name__)


# ── Warehouse state mapping (for same-state package logic) ────────────

_WAREHOUSE_STATE: dict[str, str] = {
    "LA_MAKS": "CA",
    "CHICAGO_MAX": "IL",
    "MIAMI_MAKS": "FL",
}


# ── City/State/ZIP parsing ───────────────────────────────────────────

def parse_city_state_zip(text_val: str) -> tuple[str, str, str] | None:
    """Parse address into (city, state_code, zipcode).

    Patterns (tried in order):
    1. "City, ST ZIP" — comma + 2-letter code + ZIP
    2. "City, StateName ZIP" — comma + full state name + ZIP
    3. "City ST ZIP" — no comma, 2-letter code before ZIP

    Returns None if unparseable.
    """
    if not text_val or not text_val.strip():
        return None

    t = text_val.strip()

    # Pattern 1: "City, ST 12345" or "City, ST 12345-6789"
    m = re.match(r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", t)
    if m:
        code = m.group(2).upper()
        if code in STATE_TO_WAREHOUSE:
            return m.group(1).strip(), code, m.group(3)

    # Pattern 2: "City, StateName 12345"
    m = re.match(r"^(.+?),\s*([A-Za-z][A-Za-z ]+?)\s+(\d{5}(?:-\d{4})?)\s*$", t)
    if m:
        name = m.group(2).strip().lower()
        code = STATE_NAME_TO_CODE.get(name)
        if code:
            return m.group(1).strip(), code, m.group(3)

    # Pattern 3: "City ST 12345" — 2-letter code only, no comma
    m = re.match(r"^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", t)
    if m:
        code = m.group(2).upper()
        if code in STATE_TO_WAREHOUSE:
            return m.group(1).strip(), code, m.group(3)

    return None


# ── Package selection ─────────────────────────────────────────────────

def select_package(
    items: list[dict],
    warehouse: str,
    client_state: str,
) -> str | None:
    """Select package type based on items, warehouse, and client state.

    Rules:
    - 1 device only (0 sticks) → "Z device"
    - Then total = devices + sticks:
      - 1 → "FIRST CLASS"
      - Same state + 2-4 → "SHIPPING BAG"
      - 2-3 → "ENVELOPE"
      - 4 → "SHIPPING BAG"
      - 5-6 → "G Box 4-6"
      - 7-12 → "MEDIUM BOX"
      - >12 → None (too large)
    """
    devices = sum(i["quantity"] for i in items if i.get("product_type") == "device")
    sticks = sum(i["quantity"] for i in items if i.get("product_type") != "device")

    # 1 device only → Z device
    if devices == 1 and sticks == 0:
        return "Z device"

    total = devices + sticks
    if total < 1:
        return None

    wh_state = _WAREHOUSE_STATE.get(warehouse)
    same_state = wh_state is not None and client_state == wh_state

    if total == 1:
        return "FIRST CLASS"
    if same_state and 2 <= total <= 4:
        return "SHIPPING BAG"
    if 2 <= total <= 3:
        return "ENVELOPE"
    if total == 4:
        return "SHIPPING BAG"
    if 5 <= total <= 6:
        return "G Box 4-6"
    if 7 <= total <= 12:
        return "MEDIUM BOX"

    return None  # >12


# ── Address snapshot CRUD ─────────────────────────────────────────────

def save_order_shipping_address(
    email: str,
    order_id: str,
    name: str,
    street: str,
    csz: str,
) -> None:
    """Atomic UPSERT shipping address snapshot for an order.

    Uses INSERT ... ON CONFLICT DO UPDATE for race-safety.
    """
    email = email.lower().strip()
    session = get_session()
    try:
        session.execute(text("""
            INSERT INTO order_shipping_addresses
                (client_email, order_id, client_name, street, city_state_zip,
                 created_at, updated_at)
            VALUES (:email, :order_id, :name, :street, :csz,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (client_email, order_id)
            DO UPDATE SET
                client_name = EXCLUDED.client_name,
                street = EXCLUDED.street,
                city_state_zip = EXCLUDED.city_state_zip,
                updated_at = CURRENT_TIMESTAMP
        """), {
            "email": email, "order_id": order_id,
            "name": name, "street": street, "csz": csz,
        })
        session.commit()
        logger.info("Saved shipping address for %s order=%s", email, order_id)
    except Exception as e:
        session.rollback()
        logger.error("Failed to save shipping address: %s", e)
    finally:
        session.close()


def get_order_shipping_address(email: str, order_id: str | None) -> dict | None:
    """Get shipping address snapshot for an order."""
    if not order_id:
        return None
    email = email.lower().strip()
    session = get_session()
    try:
        addr = (
            session.query(OrderShippingAddress)
            .filter_by(client_email=email, order_id=order_id)
            .first()
        )
        if addr:
            return {
                "client_name": addr.client_name,
                "street": addr.street,
                "city_state_zip": addr.city_state_zip,
            }
        return None
    finally:
        session.close()


# ── Shipping job creation ─────────────────────────────────────────────

def create_shipping_job(
    fulfillment_event_id: int,
    client_email: str,
    order_id: str | None,
    client_name: str,
    street: str,
    city_state_zip: str,
    address_source: str,
    warehouse: str,
    items: list[dict],
) -> int | None:
    """Create a pending shipping job.

    Parses city_state_zip, computes package_type, inserts job.
    Returns job ID or None on failure.
    """
    from utils.telegram import send_telegram

    parsed = parse_city_state_zip(city_state_zip)
    if not parsed:
        logger.error(
            "Cannot parse address for shipping job: %s csz='%s'",
            client_email, city_state_zip,
        )
        send_telegram(
            f"⚠️ <b>Shipping job failed</b>\n"
            f"Client: {client_email}\nOrder: {order_id}\n"
            f"Cannot parse address: {city_state_zip}",
        )
        return None

    city, state, zipcode = parsed

    package = select_package(items, warehouse, state)
    if package is None:
        logger.warning(
            "Cannot determine package for shipping job: %s items=%d",
            client_email, sum(i["quantity"] for i in items),
        )
        send_telegram(
            f"⚠️ <b>Shipping job failed</b>\n"
            f"Client: {client_email}\nOrder: {order_id}\n"
            f"Too many items ({sum(i['quantity'] for i in items)}) — fill manually.",
        )
        return None

    session = get_session()
    try:
        job = ShippingJob(
            fulfillment_event_id=fulfillment_event_id,
            client_email=client_email.lower().strip(),
            order_id=order_id,
            client_name=client_name,
            street=street,
            city=city,
            state=state,
            zipcode=zipcode,
            address_source=address_source,
            warehouse=warehouse,
            items_json=json.dumps(items),
            package_type=package,
        )
        session.add(job)
        session.commit()
        job_id = job.id
        logger.info(
            "Shipping job created: id=%d %s order=%s wh=%s pkg=%s src=%s",
            job_id, client_email, order_id, warehouse, package, address_source,
        )

        if address_source == "client_record":
            send_telegram(
                f"📦 <b>Shipping job created</b> (⚠️ address from client record — verify manually)\n"
                f"Client: {client_email}\nOrder: {order_id}\n"
                f"Warehouse: {warehouse} | Package: {package}",
            )

        return job_id
    except IntegrityError:
        session.rollback()
        logger.warning("Duplicate shipping job for fulfillment_event_id=%d", fulfillment_event_id)
        return None
    except Exception as e:
        session.rollback()
        logger.error("Failed to create shipping job: %s", e)
        return None
    finally:
        session.close()


# ── Job queue operations ──────────────────────────────────────────────

def claim_next_shipping_job(max_retries: int = 3) -> dict | None:
    """Atomically claim the next pending shipping job.

    Uses ORM queries for DB portability (works with both PostgreSQL and SQLite).
    Also cleans up exhausted jobs (retry_count >= max_retries).

    Returns job dict or None if no jobs available.
    """
    from sqlalchemy import or_, and_

    token = str(uuid.uuid4())
    now = datetime.utcnow()
    until = now + timedelta(minutes=15)

    session = get_session()
    try:
        # Cleanup exhausted jobs first
        exhausted = (
            session.query(ShippingJob)
            .filter(
                ShippingJob.retry_count >= max_retries,
                or_(
                    ShippingJob.status == "pending",
                    and_(
                        ShippingJob.status == "claimed",
                        ShippingJob.claimed_until < now,
                    ),
                ),
            )
            .all()
        )
        for j in exhausted:
            j.status = "failed"
            j.error_message = "max retries exceeded"

        # Find next claimable job
        job = (
            session.query(ShippingJob)
            .filter(
                or_(
                    and_(
                        ShippingJob.status == "pending",
                        ShippingJob.retry_count < max_retries,
                    ),
                    and_(
                        ShippingJob.status == "claimed",
                        ShippingJob.claimed_until < now,
                        ShippingJob.retry_count < max_retries,
                    ),
                ),
            )
            .order_by(ShippingJob.created_at)
            .first()
        )

        if not job:
            session.commit()
            return None

        # Claim it
        job.status = "claimed"
        job.claim_token = token
        job.claimed_at = now
        job.claimed_until = until
        job.retry_count += 1
        session.commit()

        return {
            "id": job.id,
            "fulfillment_event_id": job.fulfillment_event_id,
            "client_email": job.client_email,
            "order_id": job.order_id,
            "client_name": job.client_name,
            "street": job.street,
            "city": job.city,
            "state": job.state,
            "zipcode": job.zipcode,
            "address_source": job.address_source,
            "warehouse": job.warehouse,
            "items": json.loads(job.items_json),
            "package_type": job.package_type,
            "status": job.status,
            "claim_token": job.claim_token,
            "retry_count": job.retry_count,
        }
    except Exception as e:
        session.rollback()
        logger.error("Failed to claim shipping job: %s", e)
        return None
    finally:
        session.close()


def complete_shipping_job(job_id: int, claim_token: str) -> bool:
    """Mark a claimed job as filled."""
    session = get_session()
    try:
        job = (
            session.query(ShippingJob)
            .filter_by(id=job_id, claim_token=claim_token, status="claimed")
            .first()
        )
        if not job:
            return False
        job.status = "filled"
        job.filled_at = datetime.utcnow()
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        logger.error("Failed to complete shipping job %d: %s", job_id, e)
        return False
    finally:
        session.close()


def fail_shipping_job(
    job_id: int,
    claim_token: str,
    error: str,
    permanent: bool = False,
    reset_retry: bool = False,
) -> bool:
    """Mark a job as failed or requeue it for retry.

    permanent=True or exhausted → status='failed'
    transient → status='pending', clear claim fields
    reset_retry=True → decrement retry_count (auth failures don't burn retries)
    """
    session = get_session()
    try:
        job = (
            session.query(ShippingJob)
            .filter_by(id=job_id, claim_token=claim_token, status="claimed")
            .first()
        )
        if not job:
            return False

        if permanent:
            job.status = "failed"
            job.error_message = error
        else:
            job.status = "pending"
            job.claim_token = None
            job.claimed_at = None
            job.claimed_until = None
            job.error_message = error
            if reset_retry and job.retry_count > 0:
                job.retry_count -= 1

        session.commit()
        logger.info(
            "Shipping job %d %s: %s",
            job_id, "failed permanently" if permanent else "requeued", error,
        )
        return True
    except Exception as e:
        session.rollback()
        logger.error("Failed to fail shipping job %d: %s", job_id, e)
        return False
    finally:
        session.close()
