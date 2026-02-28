"""
Database Initialization
-----------------------

Creates tables. Client data is managed via Admin Agent, not hardcoded.

Run:
    python -m db.init_data
"""

import logging

from db.models import Base, Client, engine, get_session

logger = logging.getLogger(__name__)


def init_default_data():
    """Create all tables (clients, email history, etc.)."""
    Base.metadata.create_all(engine)
    logger.info("Tables created (or already exist).")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    init_default_data()

    # Verify
    session = get_session()
    try:
        clients = session.query(Client).all()
        logger.info("Clients in database: %d", len(clients))
        for c in clients:
            logger.info("  %s | %s | %s | discount: %d%%", c.email, c.name, c.payment_type, c.discount_percent)
    finally:
        session.close()
