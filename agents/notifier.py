"""
Email Notifier
--------------

Telegram notification functions for the email pipeline.
All side effects (send_telegram calls) are isolated here.
"""

import logging

from agents.checker import format_check_result_for_telegram
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)


def build_oos_message(classification, result: dict) -> str:
    """Build Telegram notification text for out-of-stock situations.

    Caller must ensure result["stock_issue"] is set before calling.
    Returns formatted HTML string for Telegram.
    """
    insufficient = result["stock_issue"]["stock_check"]["insufficient_items"]
    best_alts = result["stock_issue"].get("best_alternatives", {})

    oos_lines = []
    for item in insufficient:
        partial = f" (частично: {item['total_available']} шт)" if item["total_available"] > 0 else ""
        oos_lines.append(
            f"{item['base_flavor']} (заказано {item['ordered_qty']}, на складе {item['total_available']}){partial}"
        )

    alt_lines = []
    for flavor, decision in best_alts.items():
        options = decision.get("alternatives", [])
        if not options:
            alt_lines.append(f"{flavor}: не найдена")
            continue
        rendered = []
        for opt in options[:3]:
            alt = opt["alternative"]
            reason = opt.get("reason", "fallback")
            reason_ru = {
                "same_flavor": "тот же вкус",
                "history": f"из истории ({opt.get('order_count', '?')} заказов)",
                "profile": "из профиля",
                "llm": "ИИ рекомендация",
                "fallback": "из наличия",
            }.get(reason, reason)
            rendered.append(f"{alt['category']} / {alt['product_name']} [{reason_ru}]")
        alt_lines.append(f"{flavor}: " + "; ".join(rendered))

    return (
        f"\u26a0\ufe0f <b>Нет на складе!</b>\n\n"
        f"<b>Клиент:</b> {classification.client_email}\n"
        f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
        f"<b>Нет в наличии:</b>\n" + "\n".join(oos_lines) + "\n\n"
        f"<b>Альтернатива:</b>\n" + "\n".join(alt_lines) + "\n\n"
        f"Ответ заполнен по шаблону."
    )


def notify_new_client(classification, result: dict) -> None:
    """Send Telegram alert when an unknown client sends an email."""
    if not result["client_found"] and result["needs_reply"]:
        logger.warning("New client not in database: %s", classification.client_email)

        details = []
        if classification.order_id:
            details.append(f"<b>Заказ:</b> #{classification.order_id}")
        if classification.price:
            details.append(f"<b>Сумма:</b> {classification.price}")
        if classification.items:
            details.append(f"<b>Товар:</b> {classification.items}")
        details_text = "\n".join(details)

        send_telegram(
            f"\u26a0\ufe0f <b>Новый клиент написал письмо!</b>\n\n"
            f"<b>От:</b> {classification.client_email}\n"
            f"<b>Имя:</b> {classification.client_name or 'не указано'}\n"
            f"<b>Ситуация:</b> {classification.situation}\n"
            + (f"\n{details_text}\n" if details_text else "")
            + f"\nДобавь клиента в базу через Admin Agent."
        )


def notify_price_alerts(
    classification,
    result: dict,
    gmail_thread_id: str | None,
) -> None:
    """Send Telegram alert for price mismatch or unmatched items."""
    price_alert = result.get("price_alert")
    if not price_alert:
        return

    alert_type = price_alert["type"]
    if alert_type == "unmatched":
        send_telegram(
            f"\u26a0\ufe0f <b>Цена не рассчитана!</b>\n\n"
            f"<b>Клиент:</b> {classification.client_email}\n"
            f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
            f"<b>Товары:</b> {', '.join(price_alert['items'])}\n"
            f"<b>Thread:</b> {gmail_thread_id or 'N/A'}\n\n"
            f"Товары не сопоставились с каталогом. Шаблон не отправлен, используется LLM."
        )
    elif alert_type == "mismatch":
        send_telegram(
            f"\u26a0\ufe0f <b>Расхождение цен!</b>\n\n"
            f"<b>Клиент:</b> {classification.client_email}\n"
            f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
            f"<b>Цена сайта:</b> {price_alert['site_price']}\n"
            f"<b>Цена каталога:</b> {price_alert['calculated_price']}\n"
            f"<b>Thread:</b> {gmail_thread_id or 'N/A'}\n\n"
            f"Используется цена сайта."
        )


def notify_oos_with_draft(
    tg_msg: str | None,
    result: dict,
    checker_obj,
) -> bool:
    """Send OOS Telegram with draft preview and optional checker warnings.

    Returns True if a message was sent, False otherwise.
    """
    if tg_msg and result.get("draft_reply"):
        draft_preview = result["draft_reply"][:500]
        checker_msg = ""
        if checker_obj and not checker_obj.is_ok:
            checker_msg = "\n\n" + format_check_result_for_telegram(checker_obj)
        send_telegram(tg_msg + f"\n--- DRAFT ---\n<pre>{draft_preview}</pre>" + checker_msg)
        return True
    return False


def notify_checker_issues(classification, result: dict, checker_obj) -> bool:
    """Send Telegram alert for non-OOS checker issues.

    Returns True if a message was sent, False otherwise.
    """
    if checker_obj and not checker_obj.is_ok:
        draft_preview = (result.get("draft_reply") or "")[:500]
        send_telegram(
            f"\u26a0\ufe0f <b>Checker: проблемы в ответе</b>\n\n"
            f"<b>Клиент:</b> {classification.client_email}\n"
            f"<b>Ситуация:</b> {classification.situation}\n\n"
            + format_check_result_for_telegram(checker_obj)
            + f"\n\n--- DRAFT ---\n<pre>{draft_preview}</pre>"
        )
        return True
    return False


def notify_reply_ready(classification, result: dict) -> None:
    """Send Telegram notification when a reply is ready (template or AI)."""
    if result.get("needs_reply") and result.get("draft_reply"):
        draft_preview = result["draft_reply"][:400]
        template_str = "шаблон" if result.get("template_used") else "ИИ"
        details = ""
        if classification.order_id:
            details += f"<b>Заказ:</b> #{classification.order_id}\n"
        if classification.price:
            details += f"<b>Сумма:</b> {classification.price}\n"
        if classification.items:
            details += f"<b>Товар:</b> {classification.items}\n"
        send_telegram(
            f"\u2705 <b>Ответ готов</b> [{template_str}]\n\n"
            f"<b>Клиент:</b> {classification.client_name or ''} ({classification.client_email})\n"
            f"<b>Ситуация:</b> {classification.situation}\n"
            + details
            + f"\n--- DRAFT ---\n<pre>{draft_preview}</pre>"
        )
