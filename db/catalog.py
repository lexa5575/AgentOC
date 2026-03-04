"""
Product Catalog
---------------

Canonical product identity management.
Each unique product (category + normalized name) gets one catalog entry.
Multiple warehouses share the same product_id.

Functions:
- normalize_product_name(name) → lowered, trimmed, collapsed spaces
- ensure_catalog_entry(session, category, product_name) → catalog id
- ensure_catalog_entries(session, items) → count of new entries created
"""

import logging
import re

from sqlalchemy.exc import IntegrityError

from db.models import ProductCatalog

logger = logging.getLogger(__name__)


def normalize_product_name(name: str) -> str:
    """Normalize product name for deduplication: lower, trim, collapse spaces."""
    return re.sub(r"\s+", " ", name.strip()).lower()


def ensure_catalog_entry(session, category: str, product_name: str) -> int:
    """Find or create a catalog entry. Returns the catalog id.

    Lookup key: (category, name_norm).
    If entry exists, returns existing id without updating stock_name.
    If new, creates entry with stock_name = product_name as-is.

    Args:
        session: Active SQLAlchemy session (caller manages transaction).
        category: Product category (e.g. "TEREA_JAPAN").
        product_name: Raw product name from stock sheet (e.g. "T Purple").

    Returns:
        ProductCatalog.id for the matched/created entry.
    """
    name_norm = normalize_product_name(product_name)

    existing = (
        session.query(ProductCatalog)
        .filter_by(category=category, name_norm=name_norm)
        .first()
    )
    if existing:
        return existing.id

    entry = ProductCatalog(
        category=category,
        name_norm=name_norm,
        stock_name=product_name.strip(),
    )
    # Use savepoint so a race-condition IntegrityError doesn't roll back
    # the entire transaction (which may contain StockItem upserts).
    nested = session.begin_nested()
    try:
        session.add(entry)
        nested.commit()
    except IntegrityError:
        nested.rollback()
        existing = (
            session.query(ProductCatalog)
            .filter_by(category=category, name_norm=name_norm)
            .first()
        )
        if existing:
            return existing.id
        raise  # should not happen — re-raise if still missing

    session.flush()  # ensure id is populated
    logger.info("New catalog entry: %s | %s (id=%d)", category, product_name, entry.id)
    return entry.id


def ensure_catalog_entries(session, items: list[dict]) -> int:
    """Batch find-or-create catalog entries for a list of stock items.

    Each item dict must have 'category' and 'product_name' keys.
    Uses the provided session (no commit — caller manages transaction).

    Returns:
        Number of NEW entries created.
    """
    created = 0
    seen: dict[tuple[str, str], int] = {}  # (category, name_norm) -> id

    for item in items:
        category = item["category"]
        product_name = item["product_name"]
        name_norm = normalize_product_name(product_name)
        key = (category, name_norm)

        if key in seen:
            continue

        existing = (
            session.query(ProductCatalog)
            .filter_by(category=category, name_norm=name_norm)
            .first()
        )
        if existing:
            seen[key] = existing.id
            continue

        entry = ProductCatalog(
            category=category,
            name_norm=name_norm,
            stock_name=product_name.strip(),
        )
        nested = session.begin_nested()
        try:
            session.add(entry)
            nested.commit()
            session.flush()
            seen[key] = entry.id
            created += 1
        except IntegrityError:
            nested.rollback()
            existing = (
                session.query(ProductCatalog)
                .filter_by(category=category, name_norm=name_norm)
                .first()
            )
            if existing:
                seen[key] = existing.id

    if created:
        logger.info("Created %d new catalog entries", created)
    return created
