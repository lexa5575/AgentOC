"""
Conversation State Operations
-----------------------------

CRUD operations for ConversationState — compact JSON state per Gmail thread.
"""

import json
import logging
from datetime import datetime

from db.models import ConversationState, get_session

logger = logging.getLogger(__name__)


def get_state(gmail_thread_id: str) -> dict | None:
    """Get conversation state for a Gmail thread.

    Returns:
        State dict or None if not found.
    """
    session = get_session()
    try:
        state = (
            session.query(ConversationState)
            .filter_by(gmail_thread_id=gmail_thread_id)
            .first()
        )
        if state:
            result = state.to_dict()
            # Parse JSON
            try:
                result["state"] = json.loads(result["state_json"] or "{}")
            except json.JSONDecodeError:
                result["state"] = {}
            return result
        return None
    finally:
        session.close()


def save_state(
    gmail_thread_id: str,
    client_email: str,
    state_json: str | dict,
    situation: str = "other",
) -> None:
    """Save or update conversation state for a thread.

    Args:
        gmail_thread_id: Gmail thread ID.
        client_email: Client email address.
        state_json: State as JSON string or dict.
        situation: Current situation (for tracking).
    """
    session = get_session()
    try:
        # Serialize dict to JSON if needed
        if isinstance(state_json, dict):
            state_json = json.dumps(state_json, ensure_ascii=False)

        existing = (
            session.query(ConversationState)
            .filter_by(gmail_thread_id=gmail_thread_id)
            .first()
        )

        if existing:
            # Update existing
            existing.state_json = state_json
            existing.last_situation = situation
            existing.message_count = (existing.message_count or 0) + 1
            existing.updated_at = datetime.utcnow()
        else:
            # Create new
            state = ConversationState(
                gmail_thread_id=gmail_thread_id,
                client_email=client_email.lower().strip(),
                state_json=state_json,
                message_count=1,
                last_situation=situation,
            )
            session.add(state)

        session.commit()
        logger.info(
            "Saved conversation state for thread=%s, client=%s",
            gmail_thread_id, client_email,
        )
    except Exception as e:
        logger.error("Failed to save conversation state: %s", e)
        session.rollback()
    finally:
        session.close()


def get_client_states(client_email: str, limit: int = 5) -> list[dict]:
    """Get recent conversation states for a client.

    Args:
        client_email: Client email address.
        limit: Maximum number of states to return.

    Returns:
        List of state dicts, most recent first.
    """
    session = get_session()
    try:
        rows = (
            session.query(ConversationState)
            .filter_by(client_email=client_email.lower().strip())
            .order_by(ConversationState.updated_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for row in rows:
            state_dict = row.to_dict()
            try:
                state_dict["state"] = json.loads(state_dict["state_json"] or "{}")
            except json.JSONDecodeError:
                state_dict["state"] = {}
            result.append(state_dict)
        return result
    finally:
        session.close()


def delete_state(gmail_thread_id: str) -> bool:
    """Delete conversation state for a thread.

    Returns:
        True if deleted, False if not found.
    """
    session = get_session()
    try:
        state = (
            session.query(ConversationState)
            .filter_by(gmail_thread_id=gmail_thread_id)
            .first()
        )
        if not state:
            return False
        session.delete(state)
        session.commit()
        logger.info("Deleted conversation state: thread=%s", gmail_thread_id)
        return True
    finally:
        session.close()