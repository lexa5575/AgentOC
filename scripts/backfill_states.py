"""
Backfill Conversation States
-----------------------------

Backfill ConversationState for existing threads from EmailHistory.

Usage:
    docker exec agentos-api python scripts/backfill_states.py
"""

import logging
import sys
import time

from db.conversation_state import get_state, save_state
from db.email_history import get_thread_history
from db.models import Base, EmailHistory, engine, get_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limiting
DELAY_BETWEEN_THREADS = 0.5  # seconds


def get_all_thread_ids() -> list[tuple[str, str]]:
    """Get all unique thread IDs from email history."""
    session = get_session()
    try:
        rows = (
            session.query(EmailHistory.gmail_thread_id, EmailHistory.client_email)
            .filter(EmailHistory.gmail_thread_id.isnot(None))
            .distinct()
            .all()
        )
        return [(r.gmail_thread_id, r.client_email) for r in rows]
    finally:
        session.close()


def build_state_from_history(thread_history: list[dict]) -> dict:
    """Build initial state from thread history without LLM.
    
    This creates a minimal state structure. The full state
    will be built by the State Updater LLM on the next email.
    """
    if not thread_history:
        return {
            "status": "new",
            "topic": "general",
            "facts": {},
            "promises": [],
            "last_exchange": {"we_said": None, "they_said": None},
            "open_questions": [],
            "summary": "",
        }
    
    # Extract basic info from history
    last_inbound = None
    last_outbound = None
    situations = []
    
    for msg in thread_history:
        if msg["direction"] == "inbound":
            last_inbound = msg
        else:
            last_outbound = msg
        if msg.get("situation"):
            situations.append(msg["situation"])
    
    # Determine topic from situations
    topic = "general"
    if situations:
        # Use most common situation
        from collections import Counter
        topic = Counter(situations).most_common(1)[0][0]
    
    # Build last_exchange
    last_exchange = {"we_said": None, "they_said": None}
    if last_inbound:
        body = last_inbound.get("body", "")
        last_exchange["they_said"] = body[:200] if body else None
    if last_outbound:
        body = last_outbound.get("body", "")
        last_exchange["we_said"] = body[:200] if body else None
    
    # Check the LAST message direction to determine status
    last_msg = thread_history[-1]
    return {
        "status": "pending_response" if last_msg["direction"] == "inbound" else "resolved",
        "topic": topic,
        "facts": {},
        "promises": [],
        "last_exchange": last_exchange,
        "open_questions": [],
        "summary": f"Backfilled from {len(thread_history)} messages",
    }


def backfill():
    """Backfill conversation states for all threads."""
    # Ensure conversation_states table exists
    Base.metadata.create_all(engine)

    threads = get_all_thread_ids()
    logger.info("Found %d threads with thread_id", len(threads))
    
    processed = 0
    skipped = 0
    errors = 0
    
    for thread_id, client_email in threads:
        try:
            # Check if state already exists
            existing = get_state(thread_id)
            if existing:
                logger.debug("State already exists for %s, skipping", thread_id)
                skipped += 1
                continue
            
            # Get thread history
            history = get_thread_history(thread_id)
            if not history:
                logger.debug("No history for %s, skipping", thread_id)
                skipped += 1
                continue
            
            # Build state from history
            state = build_state_from_history(history)
            
            # Determine situation from last message
            situation = history[-1].get("situation", "other") if history else "other"
            
            # Save state
            save_state(
                gmail_thread_id=thread_id,
                client_email=client_email,
                state_json=state,
                situation=situation,
            )
            
            processed += 1
            logger.info(
                "Backfilled state for thread=%s, client=%s (%d/%d)",
                thread_id[:20], client_email, processed, len(threads),
            )
            
            # Rate limiting
            time.sleep(DELAY_BETWEEN_THREADS)
            
        except Exception as e:
            logger.error("Failed to backfill %s: %s", thread_id, e, exc_info=True)
            errors += 1
    
    logger.info(
        "Backfill complete: processed=%d, skipped=%d, errors=%d",
        processed, skipped, errors,
    )


if __name__ == "__main__":
    try:
        backfill()
    except Exception as e:
        logger.error("Backfill failed: %s", e, exc_info=True)
        sys.exit(1)
