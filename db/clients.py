"""
Client Operations
-----------------

CRUD operations for client data in PostgreSQL.
"""

import logging

from db.models import Client, get_session

logger = logging.getLogger(__name__)


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
