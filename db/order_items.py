"""
Order Item History
------------------

Save, replace, and query client order items for preference tracking
and personalized OOS alternative selection.
"""

import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.models import ClientOrderItem, get_session
from db.stock import get_product_type

logger = logging.getLogger(__name__)


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
