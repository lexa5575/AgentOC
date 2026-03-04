"""
Migration: Create product_catalog table + add product_id to stock_items
------------------------------------------------------------------------

Steps:
1. Creates product_catalog table (via create_all)
2. Adds product_id column to stock_items (ALTER TABLE)
3. Creates index on stock_items.product_id
4. Backfills product_id for all existing stock_items

Usage:
    docker exec agentos-api python scripts/migrate_product_catalog.py
"""

import logging
import sys

from sqlalchemy import text

from db.catalog import ensure_catalog_entry, normalize_product_name
from db.models import Base, StockItem, engine, get_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Run the full migration."""
    # Step 1: Create product_catalog table
    Base.metadata.create_all(engine)
    logger.info("Step 1: product_catalog table created (or already exists)")

    # Step 2: Add product_id column to stock_items
    with engine.connect() as conn:
        # Check if column already exists
        result = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'stock_items' AND column_name = 'product_id'
        """))

        if result.fetchone():
            logger.info("Step 2: product_id column already exists, skipping")
        else:
            conn.execute(text("""
                ALTER TABLE stock_items
                ADD COLUMN product_id INTEGER REFERENCES product_catalog(id)
            """))
            conn.commit()
            logger.info("Step 2: product_id column added to stock_items")

        # Step 3: Create index
        result = conn.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'stock_items' AND indexname = 'ix_stock_items_product_id'
        """))

        if result.fetchone():
            logger.info("Step 3: index ix_stock_items_product_id already exists, skipping")
        else:
            conn.execute(text("""
                CREATE INDEX ix_stock_items_product_id ON stock_items(product_id)
            """))
            conn.commit()
            logger.info("Step 3: index ix_stock_items_product_id created")

    # Step 4: Backfill product_id for existing stock_items
    session = get_session()
    try:
        items = session.query(StockItem).all()
        total = len(items)
        filled = 0
        already_set = 0

        for item in items:
            if item.product_id is not None:
                already_set += 1
                continue

            catalog_id = ensure_catalog_entry(session, item.category, item.product_name)
            item.product_id = catalog_id
            filled += 1

        session.commit()
        logger.info(
            "Step 4: Backfill complete — %d total, %d filled, %d already set",
            total, filled, already_set,
        )
    except Exception as e:
        session.rollback()
        logger.error("Backfill failed: %s", e, exc_info=True)
        raise
    finally:
        session.close()

    # Step 5: Verification
    session = get_session()
    try:
        total_stock = session.query(StockItem).count()
        null_count = session.query(StockItem).filter(StockItem.product_id.is_(None)).count()
        from db.models import ProductCatalog
        catalog_count = session.query(ProductCatalog).count()

        logger.info("--- Verification ---")
        logger.info("stock_items total:       %d", total_stock)
        logger.info("product_catalog entries:  %d", catalog_count)
        logger.info("stock_items with NULL product_id: %d", null_count)

        if null_count > 0:
            logger.warning("WARNING: %d stock_items still have NULL product_id!", null_count)
        else:
            logger.info("OK: all stock_items have product_id assigned")
    finally:
        session.close()


if __name__ == "__main__":
    try:
        migrate()
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        sys.exit(1)
