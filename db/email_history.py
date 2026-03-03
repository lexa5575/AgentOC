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
) -> None:
    """Save an email (inbound or outbound) to the history table."""
    session = get_session()
    try:
        record = EmailHistory(
            client_email=client_email.lower().strip(),
            direction=direction,
            subject=subject,
            body=body,
            situation=situation,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
        )
        session.add(record)
        session.commit()
        logger.info("Saved %s email for %s (situation=%s, thread=%s)", direction, client_email, situation, gmail_thread_id)
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

def get_gmail_state() -> str | None:
    """Get last processed Gmail history_id."""
    session = get_session()
    try:
        state = session.query(GmailState).first()
        return state.last_history_id if state else None
    finally:
        session.close()


def set_gmail_state(history_id: str) -> None:
    """Update last processed Gmail history_id."""
    session = get_session()
    try:
        state = session.query(GmailState).first()
        if state:
            state.last_history_id = history_id
        else:
            session.add(GmailState(id=1, last_history_id=history_id))
        session.commit()
        logger.info("Gmail state updated: history_id=%s", history_id)
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


# ---------------------------------------------------------------------------
# Gmail thread history + merged full history
# ---------------------------------------------------------------------------

def get_full_email_history(client_email: str, max_results: int = 10) -> list[dict]:
    """Get conversation history: local DB + Gmail, merged and deduplicated.

    Always supplements from Gmail when local DB has fewer than max_results.
    This ensures the profiler and handlers see the full conversation history
    even for clients with 500+ messages in Gmail but few in local DB.
    """
    history = get_email_history(client_email, max_total=max_results)

    if len(history) < max_results:
        gmail_history = get_gmail_thread_history(client_email, max_results=max_results)
        if gmail_history:
            local_subjects = {(h["subject"], h["direction"]) for h in history}
            for gh in gmail_history:
                if (gh["subject"], gh["direction"]) not in local_subjects:
                    history.append(gh)

            def _sort_key(h):
                dt = h["created_at"]
                if dt.tzinfo is not None:
                    return dt.timestamp()
                return dt.replace(tzinfo=timezone.utc).timestamp()

            history.sort(key=_sort_key)
            history = history[-max_results:]

    return history


def get_full_thread_history(gmail_thread_id: str, max_results: int = 20) -> list[dict]:
    """Get thread history: local DB first, supplement from Gmail if sparse.

    Mirrors get_full_email_history() pattern but for a specific Gmail thread.
    """
    history = get_thread_history(gmail_thread_id, limit=max_results)

    if len(history) < 2:
        gmail_history = _fetch_gmail_thread_by_id(gmail_thread_id)
        if gmail_history:
            local_keys = {(h["subject"], h["direction"]) for h in history}
            for gh in gmail_history:
                if (gh["subject"], gh["direction"]) not in local_keys:
                    history.append(gh)

            def _sort_key(h):
                dt = h["created_at"]
                if dt.tzinfo is not None:
                    return dt.timestamp()
                return dt.replace(tzinfo=timezone.utc).timestamp()

            history.sort(key=_sort_key)
            history = history[-max_results:]

    return history


def _fetch_gmail_thread_by_id(gmail_thread_id: str) -> list[dict]:
    """Fetch thread history from Gmail API by thread ID."""
    from tools.gmail import GmailClient

    try:
        gmail = GmailClient()
        history = gmail.fetch_thread(gmail_thread_id)
        logger.info(
            "Gmail thread fetch for %s: %d messages",
            gmail_thread_id, len(history),
        )
        return history
    except Exception as e:
        logger.error("Failed to fetch Gmail thread %s: %s", gmail_thread_id, e)
        return []


def get_gmail_thread_history(client_email: str, max_results: int = 10) -> list[dict]:
    """Fetch conversation history from Gmail API for a client.

    Used when local DB has little or no history (e.g., new automation
    but client has years of prior emails in Gmail).

    Returns list in same format as get_email_history().
    """
    from tools.gmail import GmailClient

    try:
        gmail = GmailClient()
        history = gmail.search_thread_history(client_email, max_results=max_results)
        logger.info(
            "Gmail thread history for %s: %d messages found",
            client_email, len(history),
        )
        return history
    except Exception as e:
        logger.error("Failed to fetch Gmail thread history for %s: %s", client_email, e)
        return []
