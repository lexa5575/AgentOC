"""Template helpers shared by template-based handlers."""

import html as html_mod
import logging

from agents.reply_templates import REPLY_TEMPLATES
from db.memory import decrement_discount

logger = logging.getLogger(__name__)


def to_gmail_html(plain_reply: str, order_summary: str = "") -> str:
    """Convert a filled template reply to styled HTML for Gmail draft.

    Applies formatting matching the business email style:
    - Total line → bold, with order summary appended
    - "In memo or comments" warning → red bold
    - "If paid today" line → bold
    - Empty lines → asterisk separators
    """
    lines = plain_reply.split("\n")
    html_lines = []

    for line in lines:
        line_lower = line.lower()

        if not line.strip():
            html_lines.append("*")
            continue

        escaped = html_mod.escape(line)

        if "total" in line_lower and ("shipping" in line_lower or "free" in line_lower):
            text = escaped
            if order_summary:
                text += f" ({html_mod.escape(order_summary)})"
            html_lines.append(f"<b>{text}</b>")
        elif "in memo or comments" in line_lower:
            warning = "( In memo or comments don't put anything please ! )"
            warning_esc = html_mod.escape(warning)
            styled = (
                f'<span style="color:red;font-weight:bold">'
                f"{warning_esc}</span>"
            )
            html_lines.append(escaped.replace(warning_esc, styled))
        elif "if paid today" in line_lower:
            html_lines.append(f"<b>{escaped}</b>")
        else:
            html_lines.append(escaped)

    body = "<br>\n".join(html_lines)
    return (
        '<div style="font-family:Arial,sans-serif;font-size:14px;'
        f'color:#000;">{body}</div>'
    )


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

    if classification.parser_used:
        price = classification.price or ""
    else:
        calc = result.get("calculated_price")
        price = f"${calc:.2f}" if calc is not None else ""

    # Guard: template requires price but none available → skip template
    if "{PRICE}" in template and not price:
        logger.warning(
            "Template requires {PRICE} but no price available for %s — skipping",
            classification.client_email,
        )
        return result, False

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
    street = classification.customer_street or client.get("street", "")
    city_zip = classification.customer_city_state_zip or client.get("city_state_zip", "")
    reply = reply.replace("{CUSTOMER_STREET}", street)
    reply = reply.replace("{CUSTOMER_CITY_STATE_ZIP}", city_zip)
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
    result["draft_reply_html"] = to_gmail_html(
        reply, result.get("order_summary", "")
    )
    result["needs_routing"] = False
    return result, True
