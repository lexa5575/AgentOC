"""Stock lookup tool for LLM agents."""

import logging

from db.stock import CATEGORY_PRICES, search_stock

logger = logging.getLogger(__name__)


def search_stock_tool(flavor: str) -> str:
    """Check current stock availability for a product flavor or name.

    Use this whenever a customer asks if a specific product is available,
    in stock, how much it costs, or whether you carry it.

    Args:
        flavor: Product name or flavor to search for.
                Examples: "Turquoise", "Green", "T Mint", "Tropical", "Silver"

    Returns:
        Stock availability info with quantities and prices.
    """
    try:
        items = search_stock(flavor)
        if not items:
            return f"No products found matching '{flavor}'."

        available = [it for it in items if it["quantity"] > 0]

        if available:
            lines = [f"{flavor} — IN STOCK:"]
            for item in available:
                price = CATEGORY_PRICES.get(item["category"])
                price_str = f" (${price}/box)" if price else ""
                lines.append(
                    f"  • {item['product_name']} [{item['category']}]"
                    f" — qty: {item['quantity']}{price_str}"
                )
        else:
            lines = [f"{flavor} — OUT OF STOCK (qty: 0)"]

        return "\n".join(lines)

    except Exception as e:
        logger.warning("search_stock_tool failed for '%s': %s", flavor, e)
        return f"Could not check stock for '{flavor}' at this time."
