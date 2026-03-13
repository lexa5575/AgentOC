"""
Price Constants and Order Price Calculation
--------------------------------------------

Unit prices by stock category and order total calculation.
"""

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Product type constants (sticks vs devices)
# ---------------------------------------------------------------------------

DEVICE_CATEGORIES = {"ONE", "STND", "PRIME"}
STICK_CATEGORIES = {"KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA", "УНИКАЛЬНАЯ_ТЕРЕА"}

# Unit prices by stock category (fixed catalog)
CATEGORY_PRICES: dict[str, int] = {
    "TEREA_EUROPE": 110,
    "KZ_TEREA": 110,
    "ARMENIA": 110,
    "TEREA_JAPAN": 115,
    "УНИКАЛЬНАЯ_ТЕРЕА": 115,
    "ONE": 99,
    "STND": 149,
    "PRIME": 245,
}


def calculate_order_price(stock_check_items: list[dict]) -> float | None:
    """Calculate total order price from stock check results.

    Strict mode: returns None if any item is unmatched (no stock_entries),
    has an unknown category, or falls into multiple price groups.
    No fallback prices — caller decides what to do with None.
    """
    if not stock_check_items:
        return None

    total = 0.0
    for item in stock_check_items:
        entries = item.get("stock_entries", [])
        if not entries:
            logger.warning(
                "Price calc: no stock entries for '%s'",
                item.get("base_flavor", "?"),
            )
            return None

        # All entries must resolve to the same unit price
        prices_seen = {CATEGORY_PRICES.get(e["category"]) for e in entries}
        prices_seen.discard(None)
        if len(prices_seen) != 1:
            logger.warning(
                "Price calc: ambiguous categories for '%s': %s",
                item.get("base_flavor", "?"),
                [e["category"] for e in entries],
            )
            return None

        unit_price = prices_seen.pop()
        total += item["ordered_qty"] * unit_price

    return total
