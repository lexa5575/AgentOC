"""
Migration: Add variant_id + display_name_snapshot to client_order_items
-----------------------------------------------------------------------

Phase 1 of variant_id-first migration (plan §10.1, §11 Phase 1).

Steps:
1. Adds variant_id column (nullable FK -> product_catalog.id)
2. Adds display_name_snapshot column (nullable)
3. Creates index ix_client_order_items_variant_id

No data backfill — columns start NULL.
No behavior change.

Usage:
    docker exec agentos-api python scripts/migrate_variant_id.py

Rollback:
    DROP INDEX IF EXISTS ix_client_order_items_variant_id;
    ALTER TABLE client_order_items DROP COLUMN display_name_snapshot;
    ALTER TABLE client_order_items DROP COLUMN variant_id;
"""

import logging
import sys

from sqlalchemy import text

from db.models import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Run Phase 1 schema migration."""
    with engine.connect() as conn:
        # Step 1: Add variant_id column
        result = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'client_order_items' AND column_name = 'variant_id'
        """))

        if result.fetchone():
            logger.info("Step 1: variant_id column already exists, skipping")
        else:
            conn.execute(text("""
                ALTER TABLE client_order_items
                ADD COLUMN variant_id INTEGER REFERENCES product_catalog(id)
            """))
            conn.commit()
            logger.info("Step 1: variant_id column added")

        # Step 2: Add display_name_snapshot column
        result = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'client_order_items' AND column_name = 'display_name_snapshot'
        """))

        if result.fetchone():
            logger.info("Step 2: display_name_snapshot column already exists, skipping")
        else:
            conn.execute(text("""
                ALTER TABLE client_order_items
                ADD COLUMN display_name_snapshot VARCHAR
            """))
            conn.commit()
            logger.info("Step 2: display_name_snapshot column added")

        # Step 3: Create index on variant_id
        result = conn.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'client_order_items'
              AND indexname = 'ix_client_order_items_variant_id'
        """))

        if result.fetchone():
            logger.info("Step 3: index ix_client_order_items_variant_id already exists, skipping")
        else:
            conn.execute(text("""
                CREATE INDEX ix_client_order_items_variant_id
                ON client_order_items (variant_id)
            """))
            conn.commit()
            logger.info("Step 3: index ix_client_order_items_variant_id created")

    # Step 4: Verification
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'client_order_items'
              AND column_name IN ('variant_id', 'display_name_snapshot')
            ORDER BY column_name
        """))
        cols = result.fetchall()
        logger.info("--- Verification ---")
        for col in cols:
            logger.info("  %s: type=%s nullable=%s", col[0], col[1], col[2])

        if len(cols) == 2:
            logger.info("OK: both columns exist")
        else:
            logger.warning("WARNING: expected 2 columns, found %d", len(cols))


if __name__ == "__main__":
    try:
        migrate()
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        sys.exit(1)
