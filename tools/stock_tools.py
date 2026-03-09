"""Stock lookup tool for LLM agents."""

import logging

from db.region_family import CATEGORY_REGION_SUFFIX
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

        # Group by (product_name, region) — combine ARMENIA + KZ_TEREA into "ME"
        in_stock: dict[tuple[str, str], dict] = {}   # (name, region) → {qty, price}
        oos: dict[tuple[str, str], dict] = {}

        for it in items:
            avail_qty = it["quantity"]
            region = CATEGORY_REGION_SUFFIX.get(it["category"], "")
            price = CATEGORY_PRICES.get(it["category"])
            key = (it["product_name"], region)

            if avail_qty > 0:
                if key in in_stock:
                    in_stock[key]["qty"] += avail_qty
                else:
                    in_stock[key] = {"qty": avail_qty, "price": price}
            else:
                if key not in in_stock and key not in oos:
                    oos[key] = {"price": price}

        lines = []

        if in_stock:
            lines.append(f"{flavor} — IN STOCK:")
            for (name, region), info in sorted(in_stock.items()):
                price_str = f" (${info['price']}/box)" if info["price"] else ""
                region_str = f" {region}" if region else ""
                lines.append(
                    f"  • {name}{region_str}"
                    f" — available: {info['qty']}{price_str}"
                )

        if oos:
            lines.append(f"{flavor} — OUT OF STOCK:")
            for (name, region), info in sorted(oos.items()):
                price_str = f" (${info['price']}/box)" if info["price"] else ""
                region_str = f" {region}" if region else ""
                lines.append(
                    f"  • {name}{region_str}"
                    f" — OUT OF STOCK{price_str}"
                )

        if not lines:
            lines = [f"{flavor} — OUT OF STOCK (available: 0)"]

        return "\n".join(lines)

    except Exception as e:
        logger.warning("search_stock_tool failed for '%s': %s", flavor, e)
        return f"Could not check stock for '{flavor}' at this time."
