"""
Gmail Poller
------------

Orchestrator: polls Gmail for new messages, processes each through
the email agent, and sends results to Telegram (human-in-the-loop).

Does NOT send replies to customers — only to the operator via Telegram.

Usage:
    from tools.gmail_poller import poll_gmail
    count = poll_gmail()  # returns number of processed messages
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from os import getenv

from agents.email_agent import classify_and_process
from db.memory import (
    email_already_processed,
    email_is_deferred,
    get_gmail_state,
    set_gmail_state,
)
from tools.gmail import GmailClient
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

_gmail_clients: dict[str, GmailClient] = {}
_poll_lock = threading.Lock()
_RECENT_UNREAD_WINDOW_DAYS = 14
_MAX_MERGE_GAP_HOURS = 8  # Merge same-thread messages within this gap (covers overnight)


def _get_client(account: str = "default") -> GmailClient:
    """Lazy singleton for GmailClient per account."""
    if account not in _gmail_clients:
        _gmail_clients[account] = GmailClient(account=account)
    return _gmail_clients[account]


def _format_email_text(msg: dict) -> str:
    """Format a Gmail message dict into the text format expected by classify_and_process."""
    parts = []

    from_addr = msg.get("from", "")
    from_raw = msg.get("from_raw", from_addr)
    parts.append(f"From: {from_raw}")

    if msg.get("reply_to"):
        parts.append(f"Reply-To: {msg['reply_to']}")

    parts.append(f"Subject: {msg.get('subject', '')}")

    attachments = msg.get("attachments") or []
    if attachments:
        att_desc = ", ".join(
            f"{a.get('filename', '?')} ({a.get('mime_type', '?')})" for a in attachments
        )
        parts.append(f"Attachments: {att_desc}")

    parts.append(f"Body: {msg.get('body', '')}")

    return "\n".join(parts)


from agents.formatters import format_combined_email_text as _format_combined_email_text


def _send_telegram_result(msg: dict, result: str) -> None:
    """Send formatted processing result to Telegram."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Truncate result for Telegram (max 4096 chars)
    if len(result) > 3500:
        result = result[:3500] + "\n... (truncated)"

    # Escape HTML special chars in dynamic content
    subject = (msg.get("subject", "") or "").replace("<", "&lt;").replace(">", "&gt;")
    from_addr = (msg.get("from", "") or "").replace("<", "&lt;").replace(">", "&gt;")
    result_escaped = result.replace("<", "&lt;").replace(">", "&gt;")

    # Hold-aware header: detect hold results by prefix
    is_hold = result.startswith("\u270b HOLD:")
    header = "\u270b <b>Письмо отложено</b>" if is_hold else "\U0001f4e8 <b>Новое письмо обработано!</b>"
    label = "Причина:" if is_hold else "Результат обработки:"

    text = (
        f"{header}\n\n"
        f"<b>От:</b> {from_addr}\n"
        f"<b>Тема:</b> {subject}\n\n"
        f"<b>{label}</b>\n"
        f"<pre>{result_escaped}</pre>\n\n"
        f"\u23f0 {now}"
    )

    send_telegram(text)


def process_client_email(client_email: str, account: str = "default") -> str:
    """Find the next relevant unread email from client_email and process it.

    Args:
        client_email: The client's email address.
        account: Gmail account to use ("default" or "tilda").

    Returns the formatted result string (same as poll_gmail sends to Telegram).
    """
    from tools.gmail import GMAIL_ACCOUNTS

    suffix = GMAIL_ACCOUNTS.get(account, "")
    if not getenv(f"GMAIL_REFRESH_TOKEN{suffix}", ""):
        return f"Gmail аккаунт '{account}' не настроен (нет GMAIL_REFRESH_TOKEN{suffix})."

    client = _get_client(account=account)

    # Search for unread messages from this sender + website order notifications
    unread = client.search_unread_from(client_email, max_results=5)

    # Also check website order notifications (from order@shipmecarton.com)
    # Merge both sources — client may have direct emails AND site orders
    order_notifs = client.search_unread_order_notifications(client_email)
    seen_ids = {m["msg_id"] for m in unread}
    for notif in order_notifs:
        if notif["msg_id"] not in seen_ids:
            unread.append(notif)

    if not unread:
        return f"Нет непрочитанных писем от {client_email}."

    # Build message candidates with timestamps + deferred detection.
    candidates = []
    for msg_info in unread:
        msg_id = msg_info["msg_id"]
        try:
            msg = client.get_message(msg_id)
            _is_processed = email_already_processed(msg_id)
            _is_deferred = email_is_deferred(msg_id) if _is_processed else False
            candidates.append({
                "msg_id": msg_id,
                "msg": msg,
                "created_at": msg.get("created_at"),
                "processed": _is_processed and not _is_deferred,
                "deferred": _is_deferred,
            })
        except Exception as e:
            logger.error("Failed to load unread message %s: %s", msg_id, e, exc_info=True)

    if not candidates:
        return f"Не удалось прочитать непрочитанные письма от {client_email}."

    # Only process messages from a recent window anchored at the newest unread.
    timestamps = [c["created_at"] for c in candidates if c["created_at"] is not None]
    if timestamps:
        newest_ts = max(timestamps)
        cutoff = newest_ts - timedelta(days=_RECENT_UNREAD_WINDOW_DAYS)
        recent = [c for c in candidates if c["created_at"] and c["created_at"] >= cutoff]
    else:
        # No timestamps available — treat all candidates as recent.
        recent = list(candidates)

    # Separate unprocessed from already-processed.
    unprocessed = [c for c in recent if not c["processed"]]
    if not unprocessed:
        return f"Все непрочитанные письма от {client_email} уже обработаны."

    _dt_key = lambda c: c["created_at"] or datetime.min.replace(tzinfo=timezone.utc)

    # Pre-reconcile: finalize deferred candidates where operator already replied.
    # Must run BEFORE primary selection — otherwise finalized deferred
    # can become primary or leak into same_thread/email_text.
    _deferred_in_batch = [c for c in unprocessed if c.get("deferred")]
    _finalized_ids = set()

    if _deferred_in_batch:
        from db.email_history import finalize_deferred

        # One fetch_thread() per thread (cached), then per-message evaluation
        _thread_snapshots = {}
        for dc in _deferred_in_batch:
            dc_thread = dc["msg"].get("gmail_thread_id")
            if not dc_thread or not dc["created_at"]:
                continue

            # Fetch thread snapshot once per thread_id
            if dc_thread not in _thread_snapshots:
                _thread_snapshots[dc_thread] = client.fetch_thread(dc_thread)

            # Evaluate THIS specific deferred message against the snapshot
            snapshot = _thread_snapshots[dc_thread]
            has_newer_outbound = any(
                m["direction"] == "outbound"
                and m.get("created_at")
                and m["created_at"] > dc["created_at"]
                for m in snapshot
            )

            if has_newer_outbound:
                finalize_deferred(dc["msg_id"])
                _finalized_ids.add(dc["msg_id"])
                logger.info(
                    "Pre-reconcile: finalized deferred %s "
                    "(manual reply after %s in thread %s)",
                    dc["msg_id"], dc["created_at"], dc_thread,
                )

    # Remove finalized from unprocessed before primary selection
    if _finalized_ids:
        unprocessed = [c for c in unprocessed if c["msg_id"] not in _finalized_ids]

    if not unprocessed:
        if _finalized_ids:
            return (
                f"Все deferred письма от {client_email} закрыты "
                f"(оператор уже ответил вручную)."
            )
        return f"Все непрочитанные письма от {client_email} уже обработаны."

    # Pick the newest unprocessed message as the primary.
    primary = max(unprocessed, key=_dt_key)
    primary_thread = primary["msg"].get("gmail_thread_id")

    # Collect all unprocessed messages in the same thread, but only merge
    # messages that are close in time (within _MAX_MERGE_GAP_HOURS).
    # Messages far apart (e.g. payment confirmation 12 days ago + reorder today)
    # are different intents and must NOT be merged.
    if primary_thread:
        thread_msgs = sorted(
            [c for c in unprocessed if c["msg"].get("gmail_thread_id") == primary_thread],
            key=_dt_key,
        )
        primary_ts = _dt_key(primary)
        merge_cutoff = primary_ts - timedelta(hours=_MAX_MERGE_GAP_HOURS)
        same_thread = [c for c in thread_msgs if _dt_key(c) >= merge_cutoff]
        # Stale messages (outside merge window) — mark as processed so they
        # don't get picked up on the next trigger.
        stale_thread = [c for c in thread_msgs if _dt_key(c) < merge_cutoff]
        if stale_thread:
            from db.email_history import save_email
            for c in stale_thread:
                save_email(
                    client_email=client_email,
                    direction="inbound",
                    subject=c["msg"].get("subject", ""),
                    body="(stale unread — skipped, outside merge window)",
                    situation="skipped_stale",
                    gmail_message_id=c["msg_id"],
                    gmail_thread_id=primary_thread,
                )
            logger.info(
                "Skipped %d stale same-thread messages (older than %dh from newest)",
                len(stale_thread), _MAX_MERGE_GAP_HOURS,
            )
    else:
        same_thread = [primary]

    # Build email text: combine same-thread messages with dates.
    if len(same_thread) > 1:
        email_text = _format_combined_email_text(same_thread)
    else:
        email_text = _format_email_text(primary["msg"])

    try:
        result = classify_and_process(
            email_text,
            gmail_message_id=primary["msg_id"],
            gmail_thread_id=primary_thread,
            gmail_account=account,
        )
        _send_telegram_result(primary["msg"], result)

        # Mark extra same-thread messages as processed so they aren't
        # picked up again on the next trigger.
        if len(same_thread) > 1:
            from db.email_history import save_email
            for c in same_thread:
                if c["msg_id"] != primary["msg_id"]:
                    save_email(
                        client_email=client_email,
                        direction="inbound",
                        subject=c["msg"].get("subject", ""),
                        body="(merged into combined processing)",
                        situation="merged",
                        gmail_message_id=c["msg_id"],
                        gmail_thread_id=primary_thread,
                    )

        return result
    except Exception as e:
        logger.error("Failed to process message %s: %s", primary["msg_id"], e, exc_info=True)
        return f"Ошибка обработки письма {primary['msg_id']}: {e}"

    stale_count = sum(1 for c in candidates if c not in recent and not c["processed"])
    if stale_count > 0:
        return (
            f"Свежих непрочитанных писем от {client_email} нет. "
            f"Остались только старые unread ({stale_count}) за пределами "
            f"{_RECENT_UNREAD_WINDOW_DAYS} дней."
        )
    return f"Все непрочитанные письма от {client_email} уже обработаны."


def poll_gmail() -> int:
    """Poll Gmail for new messages, process each, send to Telegram.

    Returns number of processed messages.
    Thread-safe: only one poll can run at a time.
    """
    # Check if Gmail is configured
    if not getenv("GMAIL_REFRESH_TOKEN", ""):
        logger.debug("Gmail not configured, skipping poll")
        return 0

    # Prevent concurrent runs (background loop + manual trigger)
    if not _poll_lock.acquire(blocking=False):
        logger.info("Gmail poll already running, skipping")
        return 0

    try:
        return _poll_gmail_locked()
    finally:
        _poll_lock.release()


def _poll_gmail_locked() -> int:
    """Internal poll logic (must be called under _poll_lock)."""
    client = _get_client()
    processed = 0

    try:
        # Get last known history_id
        history_id = get_gmail_state()

        if not history_id:
            # First run — save position FIRST, then process existing unreads
            current = client.get_current_history_id()
            set_gmail_state(current)  # Save immediately to prevent re-processing on restart

            unread = client.list_unread_inbox(max_results=5)
            logger.info("Gmail poller first run: %d unread primary messages", len(unread))

            if unread:
                send_telegram(
                    f"\u2705 <b>Gmail poller запущен!</b>\n\n"
                    f"Найдено {len(unread)} непрочитанных писем (Primary), обрабатываю..."
                )
                for msg_info in unread:
                    msg_id = msg_info["msg_id"]
                    if email_already_processed(msg_id):
                        continue
                    try:
                        msg = client.get_message(msg_id)
                        email_text = _format_email_text(msg)
                        result = classify_and_process(
                            email_text,
                            gmail_message_id=msg_id,
                            gmail_thread_id=msg.get("gmail_thread_id"),
                            auto_mode=True,
                        )
                        _send_telegram_result(msg, result)
                        processed += 1
                    except Exception as e:
                        logger.error("Failed to process unread message %s: %s", msg_id, e, exc_info=True)
            else:
                send_telegram(
                    "\u2705 <b>Gmail poller запущен!</b>\n\n"
                    "Непрочитанных писем нет. Отслеживаю новые."
                )

            return processed

        # Fetch new messages since last check
        new_messages = client.get_new_messages(after_history_id=history_id)

        if not new_messages:
            logger.debug("No new Gmail messages")
            return 0

        logger.info("Found %d new Gmail messages", len(new_messages))

        latest_history_id = history_id

        for msg_info in new_messages:
            msg_id = msg_info["msg_id"]

            # Deduplication check
            if email_already_processed(msg_id):
                logger.debug("Skipping already processed message: %s", msg_id)
                continue

            try:
                # Fetch full message
                msg = client.get_message(msg_id)
                logger.info(
                    "Processing Gmail message: from=%s, subject=%s",
                    msg["from"], msg["subject"],
                )

                # Format for classify_and_process
                email_text = _format_email_text(msg)

                # Process through email agent pipeline
                result = classify_and_process(
                    email_text,
                    gmail_message_id=msg_id,
                    gmail_thread_id=msg.get("gmail_thread_id"),
                    auto_mode=True,
                )

                # Send result to Telegram
                _send_telegram_result(msg, result)

                processed += 1

            except Exception as e:
                logger.error("Failed to process message %s: %s", msg_id, e, exc_info=True)
                send_telegram(
                    f"\U0001f6a8 <b>Ошибка обработки Gmail!</b>\n\n"
                    f"Message ID: {msg_id}\n"
                    f"Ошибка: {e}"
                )

            # Update history_id after each message
            if msg_info.get("history_id"):
                latest_history_id = msg_info["history_id"]

        # Save latest history_id
        if latest_history_id != history_id:
            set_gmail_state(latest_history_id)

        if processed:
            logger.info("Gmail poll complete: %d messages processed", processed)

    except Exception as e:
        logger.error("Gmail poll failed: %s", e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Gmail poller error!</b>\n\n"
            f"Ошибка: {e}\n\n"
            f"Проверь логи контейнера."
        )

    return processed
