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

        available = []
        out_of_stock = []
        for it in items:
            avail_qty = it["quantity"] - it.get("maks_sales", 0)
            if avail_qty > 0:
                available.append((it, avail_qty))
            else:
                out_of_stock.append(it)

        lines = []

        # Show in-stock items
        if available:
            lines.append(f"{flavor} — IN STOCK:")
            for item, avail_qty in available:
                price = CATEGORY_PRICES.get(item["category"])
                price_str = f" (${price}/box)" if price else ""
                lines.append(
                    f"  • {item['product_name']} [{item['category']}]"
                    f" — available: {avail_qty}{price_str}"
                )

        # Show OOS items so LLM knows they exist but are unavailable
        if out_of_stock:
            lines.append(f"{flavor} — OUT OF STOCK:")
            for item in out_of_stock:
                price = CATEGORY_PRICES.get(item["category"])
                price_str = f" (${price}/box)" if price else ""
                lines.append(
                    f"  • {item['product_name']} [{item['category']}]"
                    f" — OUT OF STOCK{price_str}"
                )

        if not lines:
            lines = [f"{flavor} — OUT OF STOCK (available: 0)"]

        return "\n".join(lines)

    except Exception as e:
        logger.warning("search_stock_tool failed for '%s': %s", flavor, e)
        return f"Could not check stock for '{flavor}' at this time."
