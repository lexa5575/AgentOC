"""Stock lookup tool for LLM agents."""

import logging

from db.stock import CATEGORY_PRICES, search_stock

logger = logging.getLogger(__name__)


def search_stock_tool(flavor: str) -> str:
    """Check current stock availability for a product flavor or name.

    Use this whenever a customer mentions a product, asks about availability,
    or you need to verify a product name exists in our catalog.

    Args:
        flavor: Flavor or color name to search for. Use JUST the flavor,
                not the full name. Examples: "Turquoise", "Green", "Mint",
                "Silver", "Purple". NOT "Terea Green" or "Green Middle East".

    Returns:
        Stock availability info with quantities and prices.
    """
    try:
        items = search_stock(flavor)
        if not items:
            return f"No products found matching '{flavor}'."

        available = [
            it for it in items
            if (it["quantity"] - it.get("maks_sales", 0)) > 0
        ]

        if available:
            lines = [f"{flavor} — IN STOCK:"]
            for item in available:
                avail_qty = item["quantity"] - item.get("maks_sales", 0)
                price = CATEGORY_PRICES.get(item["category"])
                price_str = f" (${price}/box)" if price else ""
                lines.append(
                    f"  • {item['product_name']} [{item['category']}]"
                    f" — available: {avail_qty}{price_str}"
                )
        else:
            lines = [f"{flavor} — OUT OF STOCK (available: 0)"]

        return "\n".join(lines)

    except Exception as e:
        logger.warning("search_stock_tool failed for '%s': %s", flavor, e)
        return f"Could not check stock for '{flavor}' at this time."
