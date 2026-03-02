"""
Migration: Add profile fields to clients table
------------------------------------------------

Adds notes, llm_summary, summary_updated_at to existing clients table.

Usage:
    docker exec agentos-api python scripts/migrate_client_profile.py
"""

import logging
import sys

from sqlalchemy import text

from db.models import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_COLUMNS = [
    ("notes", "TEXT DEFAULT ''"),
    ("llm_summary", "TEXT DEFAULT ''"),
    ("summary_updated_at", "TIMESTAMP"),
]


def migrate():
    """Add profile columns to clients table."""
    with engine.connect() as conn:
        for col_name, col_type in _COLUMNS:
            result = conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'clients'
                AND column_name = :col
            """), {"col": col_name})

            if result.fetchone():
                logger.info("Column %s already exists, skipping", col_name)
                continue

            logger.info("Adding column %s to clients...", col_name)
            conn.execute(text(
                f"ALTER TABLE clients ADD COLUMN {col_name} {col_type}"
            ))

        conn.commit()
        logger.info("Migration complete!")


if __name__ == "__main__":
    try:
        migrate()
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        sys.exit(1)
