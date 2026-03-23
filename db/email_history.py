"""
Email History Operations
------------------------

Save/read email history, Gmail state management, Gmail thread search.
"""

import logging
from datetime import timezone

from db.models import EmailHistory, GmailState, get_session

logger = logging.getLogger(__name__)


# Priority scoring: meaningful situations > noise
_PRIORITY_SCORES = {
    "new_order": 3,
    "discount_request": 3,
    "payment_question": 2,
    "shipping_timeline": 2,
    "other": 1,
    "tracking": 0,
    "payment_received": 0,
}


def save_email(
    client_email: str,
    direction: str,
    subject: str,
    body: str,
    situation: str,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    deferred: bool = False,
) -> None:
    """Save an email (inbound or outbound) to the history table.

    UPSERT: if gmail_message_id already exists, updates only processing
    metadata (situation, deferred). Preserves body, subject, created_at
    for audit trail integrity.
    """
    session = get_session()
    try:
        # UPSERT: if gmail_message_id exists, update only processing metadata
        if gmail_message_id:
            existing = (
                session.query(EmailHistory)
                .filter_by(gmail_message_id=gmail_message_id)
                .first()
            )
            if existing:
                existing.situation = situation
                existing.deferred = deferred
                # body, subject, created_at, direction — preserved
                session.commit()
                logger.info(
                    "Updated email %s: situation=%s, deferred=%s",
                    gmail_message_id[:12], situation, deferred,
                )
                return

        # Normal INSERT
        record = EmailHistory(
            client_email=client_email.lower().strip(),
            direction=direction,
            subject=subject,
            body=body,
            situation=situation,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
            deferred=deferred,
        )
        session.add(record)
        session.commit()
        logger.info("Saved %s email for %s (situation=%s, thread=%s, deferred=%s)", direction, client_email, situation, gmail_thread_id, deferred)
    except Exception as e:
        logger.error("Failed to save email history: %s", e)
        session.rollback()
    finally:
        session.close()


def get_thread_history(gmail_thread_id: str, limit: int = 20) -> list[dict]:
    """Fetch most recent emails in a Gmail thread, sorted chronologically.

    Args:
        gmail_thread_id: The Gmail thread ID to fetch.
        limit: Maximum number of messages to return.

    Returns:
        List of email dicts sorted oldest-first.
    """
    session = get_session()
    try:
        # Query newest first for a correct LIMIT window, then re-sort to oldest-first.
        rows = (
            session.query(EmailHistory)
            .filter_by(gmail_thread_id=gmail_thread_id)
            .order_by(EmailHistory.created_at.desc())
            .limit(limit)
            .all()
        )
        rows.reverse()
        return [r.to_dict() for r in rows]
    finally:
        session.close()


def get_email_history(client_email: str, max_total: int = 10) -> list[dict]:
    """Fetch conversation history for a client, with priority selection.

    Always includes the last 3 messages. Fills remaining slots with
    high-priority earlier messages (orders, prices, stock discussions).
    """
    session = get_session()
    try:
        rows = (
            session.query(EmailHistory)
            .filter_by(client_email=client_email.lower().strip())
            .order_by(EmailHistory.created_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            return []

        # Always include the 3 most recent messages
        recent = rows[:3]
        earlier = rows[3:]

        # Score earlier messages by priority
        scored = []
        for row in earlier:
            score = _PRIORITY_SCORES.get(row.situation, 1)
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Fill remaining slots with highest-priority earlier messages
        remaining_slots = max_total - len(recent)
        selected_earlier = [row for _, row in scored[:remaining_slots]]

        # Combine and sort chronologically (oldest first)
        combined = list(recent) + selected_earlier
        combined.sort(key=lambda r: r.created_at)

        return [r.to_dict() for r in combined]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gmail state operations
# ---------------------------------------------------------------------------

_ACCOUNT_STATE_IDS = {"default": 1, "tilda": 2}


def get_gmail_state(account: str = "default") -> str | None:
    """Get last processed Gmail history_id for an account."""
    state_id = _ACCOUNT_STATE_IDS.get(account, 1)
    session = get_session()
    try:
        state = session.query(GmailState).filter_by(id=state_id).first()
        return state.last_history_id if state else None
    finally:
        session.close()


def set_gmail_state(history_id: str, account: str = "default") -> None:
    """Update last processed Gmail history_id for an account."""
    state_id = _ACCOUNT_STATE_IDS.get(account, 1)
    session = get_session()
    try:
        state = session.query(GmailState).filter_by(id=state_id).first()
        if state:
            state.last_history_id = history_id
        else:
            session.add(GmailState(id=state_id, last_history_id=history_id))
        session.commit()
        logger.info("Gmail state updated: account=%s, history_id=%s", account, history_id)
    except Exception as e:
        logger.error("Failed to update Gmail state: %s", e)
        session.rollback()
    finally:
        session.close()


def email_already_processed(gmail_message_id: str) -> bool:
    """Check if an email was already processed (deduplication)."""
    session = get_session()
    try:
        exists = (
            session.query(EmailHistory)
            .filter_by(gmail_message_id=gmail_message_id)
            .first()
        )
        return exists is not None
    finally:
        session.close()


def email_is_deferred(gmail_message_id: str) -> bool:
    """Check if an email is saved as deferred (needs manual processing)."""
    session = get_session()
    try:
        record = (
            session.query(EmailHistory)
            .filter_by(gmail_message_id=gmail_message_id, deferred=True)
            .first()
        )
        return record is not None
    finally:
        session.close()


def finalize_deferred(gmail_message_id: str) -> None:
    """Mark deferred email as finalized — only flips deferred=False.

    Preserves original body, subject, situation, created_at for audit trail.
    """
    session = get_session()
    try:
        updated = (
            session.query(EmailHistory)
            .filter_by(gmail_message_id=gmail_message_id, deferred=True)
            .update({"deferred": False})
        )
        session.commit()
        if updated:
            logger.info("Finalized deferred email: %s", gmail_message_id[:12])
    except Exception as e:
        logger.error("Failed to finalize deferred email: %s", e)
        session.rollback()
    finally:
        session.close()


def get_deferred_client_emails() -> list[str]:
    """Get distinct client emails that have deferred inbound messages.

    Used by the poller to auto-reprocess deferred emails once
    the operator adds the client to the database.
    """
    session = get_session()
    try:
        rows = (
            session.query(EmailHistory.client_email)
            .filter_by(deferred=True, direction="inbound")
            .distinct()
            .all()
        )
        return [r[0] for r in rows]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Gmail thread history + merged full history
# ---------------------------------------------------------------------------

def get_full_email_history(
    client_email: str, max_results: int = 10, gmail_account: str = "default",
) -> list[dict]:
    """Get conversation history: local DB + Gmail, merged and deduplicated.

    Always supplements from Gmail when local DB has fewer than max_results.
    This ensures the profiler and handlers see the full conversation history
    even for clients with 500+ messages in Gmail but few in local DB.
    """
    history = get_email_history(client_email, max_total=max_results)

    if len(history) < max_results:
        gmail_history = get_gmail_thread_history(
            client_email, max_results=max_results, gmail_account=gmail_account,
        )
        if gmail_history:
            # Deduplicate by gmail_message_id (unique per message).
            # Old approach used (subject, direction) which collapsed all messages
            # in a thread to at most one inbound + one outbound.
            local_msg_ids = {
                h.get("gmail_message_id")
                for h in history
                if h.get("gmail_message_id")
            }
            for gh in gmail_history:
                gh_mid = gh.get("gmail_message_id")
                if gh_mid and gh_mid in local_msg_ids:
                    continue  # already in local DB
                history.append(gh)
                if gh_mid:
                    local_msg_ids.add(gh_mid)

            def _sort_key(h):
                dt = h["created_at"]
                if dt.tzinfo is not None:
                    return dt.timestamp()
                return dt.replace(tzinfo=timezone.utc).timestamp()

            history.sort(key=_sort_key)
            history = history[-max_results:]

    return history


def get_full_thread_history(
    gmail_thread_id: str, max_results: int = 20, gmail_account: str = "default",
) -> list[dict]:
    """Get thread history: local DB first, supplement from Gmail if sparse.

    Mirrors get_full_email_history() pattern but for a specific Gmail thread.
    """
    history = get_thread_history(gmail_thread_id, limit=max_results)

    # Keep behavior aligned with get_full_email_history():
    # when local DB has fewer than requested messages, supplement from Gmail API.
    if len(history) < max_results:
        # Keep Gmail fetch bounded to avoid huge thread payloads on long-lived clients.
        fetch_limit = min(max_results * 3, 120)
        gmail_history = _fetch_gmail_thread_by_id(
            gmail_thread_id,
            gmail_account=gmail_account,
            max_results=fetch_limit,
        )
        if gmail_history:
            # Deduplicate by gmail_message_id (unique per message).
            local_msg_ids = {
                h.get("gmail_message_id")
                for h in history
                if h.get("gmail_message_id")
            }
            for gh in gmail_history:
                gh_mid = gh.get("gmail_message_id")
                if gh_mid and gh_mid in local_msg_ids:
                    continue
                history.append(gh)
                if gh_mid:
                    local_msg_ids.add(gh_mid)

            def _sort_key(h):
                dt = h["created_at"]
                if dt.tzinfo is not None:
                    return dt.timestamp()
                return dt.replace(tzinfo=timezone.utc).timestamp()

            history.sort(key=_sort_key)
            history = history[-max_results:]

    return history


def _fetch_gmail_thread_by_id(
    gmail_thread_id: str,
    gmail_account: str = "default",
    max_results: int | None = None,
) -> list[dict]:
    """Fetch thread history from Gmail API by thread ID."""
    from tools.gmail import GmailClient

    try:
        gmail = GmailClient(account=gmail_account)
        history = gmail.fetch_thread(gmail_thread_id, max_messages=max_results)
        logger.info(
            "Gmail thread fetch for %s: %d messages",
            gmail_thread_id, len(history),
        )
        return history
    except Exception as e:
        logger.error("Failed to fetch Gmail thread %s: %s", gmail_thread_id, e)
        return []


def get_gmail_thread_history(
    client_email: str, max_results: int = 10, gmail_account: str = "default",
) -> list[dict]:
    """Fetch conversation history from Gmail API for a client.

    Used when local DB has little or no history (e.g., new automation
    but client has years of prior emails in Gmail).

    Returns list in same format as get_email_history().
    """
    from tools.gmail import GmailClient

    try:
        gmail = GmailClient(account=gmail_account)
        history = gmail.search_thread_history(client_email, max_results=max_results)
        logger.info(
            "Gmail thread history for %s: %d messages found",
            client_email, len(history),
        )
        return history
    except Exception as e:
        logger.error("Failed to fetch Gmail thread history for %s: %s", client_email, e)
        return []
