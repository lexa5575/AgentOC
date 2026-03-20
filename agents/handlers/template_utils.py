"""Template helpers shared by template-based handlers."""

import html as html_mod
import logging

from agents.reply_templates import REPLY_TEMPLATES
from db.memory import decrement_discount

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_clean_body(email_text: str) -> str:
    """Extract and clean body for keyword matching.

    Strips quoted text, signatures, normalizes Unicode quotes/apostrophes.
    """
    marker = "Body:"
    idx = email_text.find(marker)
    body = email_text[idx + len(marker):].strip() if idx >= 0 else email_text.strip()
    try:
        from tools.email_parser import _strip_quoted_text
        body = _strip_quoted_text(body)
    except (ImportError, AttributeError):
        pass
    # Normalize curly quotes/apostrophes to ASCII
    body = body.replace("\u2018", "'").replace("\u2019", "'")
    body = body.replace("\u201c", '"').replace("\u201d", '"')
    return body.lower()


def _calc_recheck_date(
    gmail_thread_id: str | None,
    facts: dict | None = None,
    payment_type: str | None = None,
) -> str | None:
    """5 business days from ship date. Returns None if no reliable ship date found."""
    from datetime import date, timedelta

    base_date = None

    # Priority 1: shipped_at from state facts
    if facts and facts.get("shipped_at"):
        try:
            from datetime import datetime
            shipped = datetime.fromisoformat(facts["shipped_at"])
            base_date = shipped.date()
        except (ValueError, TypeError):
            pass

    # Priority 2: first relevant outbound in thread
    if base_date is None and gmail_thread_id:
        try:
            from db.email_history import get_full_thread_history
            history = get_full_thread_history(gmail_thread_id, max_results=15)
            if payment_type == "prepay":
                target_situations = {"payment_received"}
            else:
                target_situations = {"new_order", "payment_received"}
            for email in history:
                if (email.get("direction") == "outbound"
                        and email.get("situation") in target_situations):
                    sent_at = email.get("created_at")
                    if sent_at and hasattr(sent_at, "date"):
                        base_date = sent_at.date()
                    break
        except Exception:
            pass

    if base_date is None:
        return None

    # Add 5 business days (skip Sat/Sun)
    days_added = 0
    current = base_date
    while days_added < 5:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days_added += 1

    return current.strftime("%A, %B %d")


# ---------------------------------------------------------------------------
# HTML converter
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main template filler
# ---------------------------------------------------------------------------

def fill_template_reply(
    classification,
    result: dict,
    situation: str,
    override_payment_type: str | None = None,
) -> tuple[dict, bool]:
    """Fill a reply template for a given situation/payment type.

    Args:
        classification: EmailClassification object.
        result: Pipeline result dict.
        situation: Template situation key (e.g. "new_order", "tracking").
        override_payment_type: Override payment_type for template lookup
            (e.g. "has_discount" for discount handler).

    Returns:
        (updated_result, template_found)
    """
    if not result["client_found"]:
        result["draft_reply"] = "(Клиент не в базе — авто-ответ не генерируется)"
        result["template_used"] = False
        result["needs_routing"] = False
        return result, False

    client = result["client_data"]
    payment_type = override_payment_type or client.get("payment_type", "unknown")
    template = REPLY_TEMPLATES.get((situation, payment_type))
    if not template:
        # Fallback: try "any" key (e.g. ("tracking", "any"))
        template = REPLY_TEMPLATES.get((situation, "any"))
    if not template:
        return result, False

    if getattr(classification, "parser_used", False):
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

    if "{FINAL_PRICE}" in template and not price:
        logger.warning(
            "Template requires {FINAL_PRICE} but no price for %s — skipping",
            classification.client_email,
        )
        return result, False

    discount = client.get("discount_percent", 0)
    discount_left = client.get("discount_orders_left", 0)
    zelle_address = client.get("zelle_address", "")

    # Guard: template requires Zelle address but empty → skip
    if "{ZELLE_ADDRESS}" in template and not zelle_address:
        logger.warning(
            "Template requires {ZELLE_ADDRESS} but empty for %s — skipping",
            classification.client_email,
        )
        return result, False

    # Guard: template requires RECHECK_DATE but no reliable ship date → skip
    if "{RECHECK_DATE}" in template:
        state = result.get("conversation_state") or {}
        facts = state.get("facts") or {}
        recheck = _calc_recheck_date(
            result.get("gmail_thread_id"),
            facts=facts,
            payment_type=client.get("payment_type"),
        )
        if not recheck:
            logger.warning(
                "Template requires {RECHECK_DATE} but no ship date for %s — skipping",
                classification.client_email,
            )
            return result, False
    else:
        recheck = None

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
        discount_str = str(discount) if discount > 0 else "0"

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
    reply = reply.replace("{DISCOUNT_ORDERS_LEFT}", str(discount_left))
    if recheck:
        reply = reply.replace("{RECHECK_DATE}", recheck)

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
    result["template_situation"] = situation
    result["draft_reply"] = reply
    result["draft_reply_html"] = to_gmail_html(
        reply, result.get("order_summary", "")
    )
    result["needs_routing"] = False
    return result, True
