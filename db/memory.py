"""
Memory Layer
------------

Unified data access layer for all business data (clients, email history).
All database operations go through this module.
"""

import logging

from db.models import Client, EmailHistory, GmailState, StockBackup, StockItem, get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client operations
# ---------------------------------------------------------------------------

def get_client(email: str) -> dict | None:
    """Look up a client by email.

    Returns dict with client data or None if not found.
    """
    session = get_session()
    try:
        client = session.query(Client).filter_by(
            email=email.lower().strip()
        ).first()
        if client:
            logger.info("Client found: %s (%s)", email, client.payment_type)
            return client.to_dict()
        logger.warning("Client not found: %s", email)
        return None
    finally:
        session.close()


def list_clients() -> list[dict]:
    """List all clients ordered by name."""
    session = get_session()
    try:
        clients = session.query(Client).order_by(Client.name).all()
        return [c.to_dict() for c in clients]
    finally:
        session.close()


def add_client(
    email: str,
    name: str,
    payment_type: str,
    zelle_address: str = "",
    discount_percent: int = 0,
    discount_orders_left: int = 0,
) -> dict:
    """Add a new client. Returns the created client dict.

    Raises ValueError if client already exists or payment_type is invalid.
    """
    if payment_type not in ("prepay", "postpay"):
        raise ValueError(f"payment_type must be 'prepay' or 'postpay', got '{payment_type}'")
    if not 0 <= discount_percent <= 100:
        raise ValueError(f"discount_percent must be 0-100, got {discount_percent}")

    email = email.lower().strip()
    session = get_session()
    try:
        existing = session.query(Client).filter_by(email=email).first()
        if existing:
            raise ValueError(f"Client {email} already exists")

        client = Client(
            email=email,
            name=name,
            payment_type=payment_type,
            zelle_address=zelle_address,
            discount_percent=discount_percent,
            discount_orders_left=discount_orders_left,
        )
        session.add(client)
        session.commit()
        logger.info("Added client: %s (%s, %s)", email, name, payment_type)
        return client.to_dict()
    finally:
        session.close()


def update_client(email: str, **fields) -> dict | None:
    """Update client fields. Returns updated client dict or None if not found.

    Supported fields: name, payment_type, zelle_address, discount_percent, discount_orders_left.
    """
    email = email.lower().strip()
    allowed = {"name", "payment_type", "zelle_address", "discount_percent", "discount_orders_left"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}

    if "payment_type" in fields and fields["payment_type"] not in ("prepay", "postpay"):
        raise ValueError(f"payment_type must be 'prepay' or 'postpay'")

    session = get_session()
    try:
        client = session.query(Client).filter_by(email=email).first()
        if not client:
            return None

        for key, value in fields.items():
            setattr(client, key, value)
        session.commit()
        logger.info("Updated client %s: %s", email, fields)
        return client.to_dict()
    finally:
        session.close()


def delete_client(email: str) -> bool:
    """Delete a client. Returns True if deleted, False if not found."""
    email = email.lower().strip()
    session = get_session()
    try:
        client = session.query(Client).filter_by(email=email).first()
        if not client:
            return False
        session.delete(client)
        session.commit()
        logger.info("Deleted client: %s", email)
        return True
    finally:
        session.close()


def decrement_discount(email: str) -> None:
    """Decrement discount_orders_left by 1. Resets discount_percent when 0."""
    session = get_session()
    try:
        client = session.query(Client).filter_by(
            email=email.lower().strip()
        ).first()
        if client and client.discount_orders_left and client.discount_orders_left > 0:
            client.discount_orders_left -= 1
            if client.discount_orders_left == 0:
                client.discount_percent = 0
            session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Email history operations
# ---------------------------------------------------------------------------

# Priority scoring: meaningful situations > noise
_PRIORITY_SCORES = {
    "new_order": 3,
    "discount_request": 3,
    "payment_question": 2,
    "shipping_timeline": 2,
    "other": 1,
    "tracking": 0,
    "payment_received": 0,
}


def save_email(
    client_email: str,
    direction: str,
    subject: str,
    body: str,
    situation: str,
    gmail_message_id: str | None = None,
) -> None:
    """Save an email (inbound or outbound) to the history table."""
    session = get_session()
    try:
        record = EmailHistory(
            client_email=client_email.lower().strip(),
            direction=direction,
            subject=subject,
            body=body,
            situation=situation,
            gmail_message_id=gmail_message_id,
        )
        session.add(record)
        session.commit()
        logger.info("Saved %s email for %s (situation=%s)", direction, client_email, situation)
    except Exception as e:
        logger.error("Failed to save email history: %s", e)
        session.rollback()
    finally:
        session.close()


def get_email_history(client_email: str, max_total: int = 10) -> list[dict]:
    """Fetch conversation history for a client, with priority selection.

    Always includes the last 3 messages. Fills remaining slots with
    high-priority earlier messages (orders, prices, stock discussions).
    """
    session = get_session()
    try:
        rows = (
            session.query(EmailHistory)
            .filter_by(client_email=client_email.lower().strip())
            .order_by(EmailHistory.created_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            return []

        # Always include the 3 most recent messages
        recent = rows[:3]
        earlier = rows[3:]

        # Score earlier messages by priority
        scored = []
        for row in earlier:
            score = _PRIORITY_SCORES.get(row.situation, 1)
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Fill remaining slots with highest-priority earlier messages
        remaining_slots = max_total - len(recent)
        selected_earlier = [row for _, row in scored[:remaining_slots]]

        # Combine and sort chronologically (oldest first)
        combined = list(recent) + selected_earlier
        combined.sort(key=lambda r: r.created_at)

        return [r.to_dict() for r in combined]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gmail state operations
# ---------------------------------------------------------------------------

def get_gmail_state() -> str | None:
    """Get last processed Gmail history_id."""
    session = get_session()
    try:
        state = session.query(GmailState).first()
        return state.last_history_id if state else None
    finally:
        session.close()


def set_gmail_state(history_id: str) -> None:
    """Update last processed Gmail history_id."""
    session = get_session()
    try:
        state = session.query(GmailState).first()
        if state:
            state.last_history_id = history_id
        else:
            session.add(GmailState(id=1, last_history_id=history_id))
        session.commit()
        logger.info("Gmail state updated: history_id=%s", history_id)
    except Exception as e:
        logger.error("Failed to update Gmail state: %s", e)
        session.rollback()
    finally:
        session.close()


def email_already_processed(gmail_message_id: str) -> bool:
    """Check if an email was already processed (deduplication)."""
    session = get_session()
    try:
        exists = (
            session.query(EmailHistory)
            .filter_by(gmail_message_id=gmail_message_id)
            .first()
        )
        return exists is not None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gmail thread history (search Gmail for prior conversation)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stock operations
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


def get_stock(product_name: str, warehouse: str | None = None) -> list[dict]:
    """Find stock by exact product name (case-insensitive)."""
    session = get_session()
    try:
        query = session.query(StockItem).filter(
            StockItem.product_name.ilike(product_name.strip())
        )
        if warehouse:
            query = query.filter_by(warehouse=warehouse)
        return [item.to_dict() for item in query.all()]
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

            stock_entries = (
                session.query(StockItem)
                .filter(StockItem.product_name.ilike(f"%{flavor}%"))
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


def get_alternatives_for_flavor(
    base_flavor: str,
    warehouse: str | None = None,
) -> list[dict]:
    """Get available stock items from categories where the given flavor exists.

    Returns only items with qty > 0 for the LLM to use as suggestions.
    """
    session = get_session()
    try:
        # Find categories containing this flavor
        matching = (
            session.query(StockItem.category)
            .filter(StockItem.product_name.ilike(f"%{base_flavor.strip()}%"))
            .distinct()
            .all()
        )
        categories = [row[0] for row in matching]

        if not categories:
            return []

        # Get all available items from those categories
        query = session.query(StockItem).filter(
            StockItem.category.in_(categories),
            StockItem.quantity > 0,
        )
        if warehouse:
            query = query.filter_by(warehouse=warehouse)

        return [
            item.to_dict()
            for item in query.order_by(StockItem.category, StockItem.product_name).all()
        ]
    finally:
        session.close()


def get_gmail_thread_history(client_email: str, max_results: int = 10) -> list[dict]:
    """Fetch conversation history from Gmail API for a client.

    Used when local DB has little or no history (e.g., new automation
    but client has years of prior emails in Gmail).

    Returns list in same format as get_email_history().
    """
    from tools.gmail import GmailClient

    try:
        gmail = GmailClient()
        history = gmail.search_thread_history(client_email, max_results=max_results)
        logger.info(
            "Gmail thread history for %s: %d messages found",
            client_email, len(history),
        )
        return history
    except Exception as e:
        logger.error("Failed to fetch Gmail thread history for %s: %s", client_email, e)
        return []
