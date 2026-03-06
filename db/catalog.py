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
- get_catalog_products() → all catalog entries for resolver matching
- get_display_name(stock_name, category) → customer-friendly name with region
- get_base_display_name(stock_name) → customer-friendly name without region
"""

import logging
import re

from sqlalchemy.exc import IntegrityError

from db.models import ProductCatalog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spelling equivalents: variant spellings of the same flavor across regions.
# Maps non-canonical name_norm → canonical name_norm.
# ---------------------------------------------------------------------------
_SPELLING_EQUIVALENTS: dict[str, str] = {
    "siena": "sienna",  # Armenia "Siena" = EU "Sienna"
}


def get_equivalent_norms(name_norm: str) -> set[str]:
    """Return all equivalent name_norms (including the input itself).

    Example: get_equivalent_norms("sienna") → {"sienna", "siena"}
    """
    canonical = _SPELLING_EQUIVALENTS.get(name_norm, name_norm)
    equivalents = {name_norm, canonical}
    for k, v in _SPELLING_EQUIVALENTS.items():
        if v == canonical:
            equivalents.add(k)
    return equivalents


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


# ---------------------------------------------------------------------------
# Catalog queries (Phase 2)
# ---------------------------------------------------------------------------

def get_catalog_products() -> list[dict]:
    """Get all catalog entries for resolver matching.

    Returns:
        List of dicts: [{id, category, name_norm, stock_name}, ...]
        Deduplicated by definition (UNIQUE constraint on category + name_norm).
    """
    from db.models import get_session

    session = get_session()
    try:
        entries = session.query(ProductCatalog).all()
        return [
            {
                "id": e.id,
                "category": e.category,
                "name_norm": e.name_norm,
                "stock_name": e.stock_name,
            }
            for e in entries
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Display names (customer-facing)
# ---------------------------------------------------------------------------

_DEVICE_PREFIXES = ("ONE", "STND", "PRIME")

# Brand prefixes and region suffixes to strip before building display names.
# Ensures idempotency: calling display functions on already-decorated names
# (e.g. "Tera Green EU") won't produce "Terea Tera Green EU EU".
_BRAND_STRIP = ("Terea ", "Tera ", "Heets ")
_REGION_STRIP_SUFFIXES = (
    " made in middle east",
    " made in armenia",
    " made in europe",
    " made in japan",
    " eu",
    " me",
    " kz",
    " japan",
)


def _strip_decorations(name: str) -> str:
    """Strip brand prefixes, T-prefix, and region suffixes from a product name.

    Returns the bare product core: "Tera Green EU" → "Green", "T Purple" → "Purple".
    """
    name = name.strip()
    for prefix in _BRAND_STRIP:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name.startswith("T ") and len(name) > 2:
        name = name[2:]
    lower = name.lower()
    for suffix in _REGION_STRIP_SUFFIXES:
        if lower.endswith(suffix):
            name = name[: len(name) - len(suffix)]
            break
    return name.strip()


def get_display_name(stock_name: str, category: str) -> str:
    """Convert DB product name + category to customer-friendly display name.

    Includes region info to distinguish products from different origins.
    Idempotent: strips existing decorations before applying new ones.

    Examples:
        ("T Purple", "TEREA_JAPAN") → "Terea Purple made in Japan"
        ("Purple", "TEREA_EUROPE") → "Terea Purple EU"
        ("Purple", "ARMENIA") → "Terea Purple ME"
        ("Purple", "KZ_TEREA") → "Terea Purple ME"
        ("Fusion Menthol", "УНИКАЛЬНАЯ_ТЕРЕА") → "Terea Fusion Menthol made in Japan"
        ("ONE Green", "ONE") → "ONE Green"
        ("Tera Green EU", "TEREA_EUROPE") → "Terea Green EU"  (no double-decoration)
    """
    upper = stock_name.upper().strip()
    for prefix in _DEVICE_PREFIXES:
        if upper == prefix or upper.startswith(prefix + " "):
            return stock_name

    core = _strip_decorations(stock_name)

    if category in ("TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"):
        return f"Terea {core} made in Japan"
    if category == "TEREA_EUROPE":
        return f"Terea {core} EU"
    if category in ("ARMENIA", "KZ_TEREA"):
        return f"Terea {core} ME"
    return stock_name


def get_base_display_name(stock_name: str) -> str:
    """Convert DB product name to generic customer-friendly name (no region).

    Used in OOS problem descriptions where the specific region doesn't matter.
    Idempotent: strips existing decorations before applying new ones.

    Examples:
        "T Purple" → "Terea Purple"
        "Purple" → "Terea Purple"
        "Silver" → "Terea Silver"
        "Fusion Menthol" → "Terea Fusion Menthol"
        "ONE Green" → "ONE Green"
        "Tera Turquoise EU" → "Terea Turquoise"  (no double-decoration)
    """
    upper = stock_name.upper().strip()
    for prefix in _DEVICE_PREFIXES:
        if upper == prefix or upper.startswith(prefix + " "):
            return stock_name

    core = _strip_decorations(stock_name)
    return f"Terea {core}"
