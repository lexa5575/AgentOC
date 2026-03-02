"""
Migration: Create conversation_states table
---------------------------------------------

Run this script to create the conversation_states table.

Usage:
    docker exec agentos-api python scripts/migrate_conversation_states.py
"""

import logging
import sys

from sqlalchemy import text

from db.models import Base, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Create conversation_states table if it doesn't exist."""
    with engine.connect() as conn:
        # Check if table already exists
        result = conn.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'conversation_states'
        """))

        if result.fetchone():
            logger.info("Table conversation_states already exists, skipping")
            return

    # Create all missing tables (safe — won't touch existing ones)
    Base.metadata.create_all(engine)
    logger.info("Migration complete — conversation_states table created!")


if __name__ == "__main__":
    try:
        migrate()
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        sys.exit(1)
