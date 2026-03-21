"""
Stock Operations
----------------

Stock sync, search, availability checks, order item tracking,
and out-of-stock alternative selection.
"""

import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.catalog import ensure_catalog_entry
from db.models import ClientOrderItem, ProductCatalog, StockBackup, StockItem, get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Product type constants (sticks vs devices) — re-exported from db.prices
# ---------------------------------------------------------------------------

from db.prices import CATEGORY_PRICES, DEVICE_CATEGORIES, STICK_CATEGORIES, calculate_order_price  # noqa: E402


def extract_variant_id(
    product_ids: list[int] | None,
    catalog_entries: list[dict] | None = None,
    client_email: str | None = None,
) -> int | None:
    """Extract variant_id from resolved product_ids.

    Single-match → return the id.
    Same-family multi-match → return preferred id (ARMENIA for ME).
    Cross-family → check client order history for previous variant.
    Otherwise → None + warning.

    Args:
        product_ids: Resolved product catalog ids.
        catalog_entries: Optional pre-loaded catalog (avoids extra DB call).
        client_email: Optional client email for history-based disambiguation.
    """
    if not product_ids:
        return None
    if len(product_ids) == 1:
        return product_ids[0]

    from db.region_family import get_preferred_product_id

    if catalog_entries is None:
        from db.catalog import get_catalog_products
        catalog_entries = get_catalog_products()

    preferred = get_preferred_product_id(product_ids, catalog_entries)
    if preferred is not None:
        return preferred

    # Cross-family fallback: check client's order history
    if client_email:
        history_variant = _resolve_variant_from_history(client_email, product_ids)
        if history_variant is not None:
            logger.info(
                "variant_id resolved from client history: %s → %d (from %s)",
                product_ids, history_variant, client_email,
            )
            return history_variant

    logger.warning(
        "variant_id ambiguous: %d product_ids %s — stored as NULL",
        len(product_ids), product_ids,
    )
    return None


def _resolve_variant_from_history(
    client_email: str,
    product_ids: list[int],
) -> int | None:
    """Check client's previous orders for a matching variant_id.

    Returns the most recently used variant_id that matches one of the
    candidate product_ids, or None if no match found.
    """
    from db.models import get_session, ClientOrderItem

    session = get_session()
    try:
        # Find the most recent order item from this client
        # where variant_id is one of the candidates
        prev = (
            session.query(ClientOrderItem.variant_id)
            .filter(
                ClientOrderItem.client_email == client_email.lower().strip(),
                ClientOrderItem.variant_id.in_(product_ids),
            )
            .order_by(ClientOrderItem.created_at.desc())
            .first()
        )
        if prev:
            return prev[0]
        return None
    except Exception as e:
        logger.warning("_resolve_variant_from_history failed for %s: %s", client_email, e)
        return None
    finally:
        session.close()


# Backward compat alias
_extract_variant_id = extract_variant_id


def has_ambiguous_variants(
    items: list[dict],
    catalog_entries: list[dict] | None = None,
    client_email: str | None = None,
) -> list[str]:
    """Return base_flavors of items with ambiguous (cross-family) product_ids.

    Same-family multi-match (e.g. ARMENIA + KZ_TEREA) is NOT ambiguous.
    Cross-family with client history match → NOT ambiguous (resolved from history).
    Cross-family or unknown product_ids ARE ambiguous → block fulfillment.

    FAIL-CLOSED: unknown pid not in catalog → ambiguous.

    Args:
        items: Order items with 'product_ids' and 'base_flavor' keys.
        catalog_entries: Optional pre-loaded catalog (avoids extra DB call).
        client_email: Optional client email for history-based disambiguation.
    """
    from db.region_family import is_same_family

    ambiguous = []
    catalog: dict[int, dict] | None = None

    for item in items:
        pids = item.get("product_ids") or []
        if len(pids) <= 1:
            continue

        # Lazy-load catalog on first multi-match
        if catalog is None:
            if catalog_entries is None:
                from db.catalog import get_catalog_products
                catalog_entries = get_catalog_products()
            catalog = {e["id"]: e for e in catalog_entries}

        # Fail-closed: if any pid not found in catalog → ambiguous
        if any(pid not in catalog for pid in pids):
            ambiguous.append(item.get("base_flavor", "?"))
            continue

        categories = {catalog[pid]["category"] for pid in pids}
        if not is_same_family(categories):
            # Cross-family: check if client history can disambiguate
            if client_email and _resolve_variant_from_history(client_email, pids) is not None:
                continue  # resolved from history — not ambiguous
            ambiguous.append(item.get("base_flavor", "?"))

    return ambiguous


# Backward compat alias
_has_ambiguous_variants = has_ambiguous_variants


def get_product_type(base_flavor: str) -> str:
    """Determine product type from base_flavor.

    Devices have brand prefix: 'ONE Green', 'STND Red', 'PRIME Black'.
    Also matches bare model names: 'ONE', 'STND', 'PRIME'.
    Sticks are just flavor: 'Green', 'Silver', 'Turquoise'.
    """
    upper = base_flavor.upper().strip()
    for prefix in ("ONE", "STND", "PRIME"):
        if upper == prefix or upper.startswith(prefix + " "):
            return "device"
    return "stick"


def _get_allowed_categories(product_type: str) -> set[str]:
    """Return allowed stock categories for a product type."""
    return DEVICE_CATEGORIES if product_type == "device" else STICK_CATEGORIES


# ---------------------------------------------------------------------------
# Stock sync — re-exported from db.stock_sync
# ---------------------------------------------------------------------------

from db.stock_sync import sync_stock  # noqa: E402


# ---------------------------------------------------------------------------
# Stock search & availability — re-exported from db.stock_search
# ---------------------------------------------------------------------------

from db.stock_search import (  # noqa: E402
    _REGION_CATEGORY_MAP,
    WAREHOUSE_ALIASES,
    resolve_warehouse,
    search_stock,
    search_stock_by_ids,
    get_available_by_category,
    get_stock_summary,
    check_stock_for_order,
)


# ---------------------------------------------------------------------------
# Order item history — re-exported from db.order_items
# ---------------------------------------------------------------------------

from db.order_items import save_order_items, replace_order_items, get_client_flavor_history, get_last_order  # noqa: E402


# ---------------------------------------------------------------------------
# OOS alternative selection — re-exported from db.alternatives
# ---------------------------------------------------------------------------

from db.alternatives import _get_available_items, select_best_alternatives  # noqa: E402


# calculate_order_price re-exported from db.prices (see top of file)
