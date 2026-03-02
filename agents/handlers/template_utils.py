"""Template helpers shared by template-based handlers."""

import logging

from agents.reply_templates import REPLY_TEMPLATES
from db.memory import decrement_discount

logger = logging.getLogger(__name__)


def fill_template_reply(
    classification,
    result: dict,
    situation: str,
) -> tuple[dict, bool]:
    """Fill a reply template for a given situation/payment type.

    Returns:
        (updated_result, template_found)
    """
    if not result["client_found"]:
        result["draft_reply"] = "(Клиент не в базе — авто-ответ не генерируется)"
        result["template_used"] = False
        result["needs_routing"] = False
        return result, False

    client = result["client_data"]
    payment_type = client.get("payment_type", "unknown")
    template = REPLY_TEMPLATES.get((situation, payment_type))
    if not template:
        return result, False

    price = classification.price or ""
    discount = client.get("discount_percent", 0)
    discount_left = client.get("discount_orders_left", 0)
    zelle_address = client.get("zelle_address", "")

    price_clean = price.replace("$", "").replace(",", "")
    try:
        price_num = float(price_clean)
    except (ValueError, TypeError):
        price_num = 0.0

    apply_discount = (
        situation == "new_order"
        and discount > 0
        and discount_left > 0
        and price_num > 0
    )
    if apply_discount:
        final_price = f"${price_num * (1 - discount / 100):.2f}"
        discount_str = str(discount)
    else:
        final_price = price
        discount_str = "0"

    reply = template
    reply = reply.replace("{PRICE}", price)
    reply = reply.replace("{DISCOUNT}", discount_str)
    reply = reply.replace("{FINAL_PRICE}", final_price)
    reply = reply.replace("{ZELLE_ADDRESS}", zelle_address)
    reply = reply.replace("{CUSTOMER_NAME}", classification.client_name or client["name"])
    reply = reply.replace("{CUSTOMER_STREET}", classification.customer_street or "")
    reply = reply.replace("{CUSTOMER_CITY_STATE_ZIP}", classification.customer_city_state_zip or "")
    reply = reply.replace("{TRACKING_URL}", "[tracking URL pending]")

    if not apply_discount and price:
        reply = reply.replace(f"{price} - 0% = {price}", price)

    if apply_discount:
        decrement_discount(classification.client_email)
        logger.info(
            "Discount applied for %s: %s%% (%d -> %d orders left)",
            classification.client_email,
            discount,
            discount_left,
            discount_left - 1,
        )

    result["template_used"] = True
    result["draft_reply"] = reply
    result["needs_routing"] = False
    return result, True
