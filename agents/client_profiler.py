"""
Client Profiler Agent
---------------------

LLM agent that generates a text summary of a client based on their
email history. Used to populate the llm_summary field on the Client model.

Can be triggered:
- On demand via Admin Agent (refresh_client_summary tool)
- Periodically via scheduled task (future)
"""

import logging
from datetime import datetime, timedelta, timezone

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from db.clients import get_client_profile, update_client_summary
from db.memory import get_full_email_history
from agents.formatters import format_email_history

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order item backfill from Gmail history
# ---------------------------------------------------------------------------

def _backfill_order_items(client_email: str, gmail_account: str = "default") -> int:
    """Parse Gmail order notifications and populate ClientOrderItem.

    Searches Gmail for order notifications from shipmecarton.com senders
    that mention this client (email appears in body or Reply-To).
    Uses try_parse_order for reliable field extraction.

    Guard: skips if ClientOrderItem records already exist for this client
    (means pipeline is actively tracking orders — no need to scan Gmail).

    Safe to call repeatedly — save_order_items skips duplicates via
    UNIQUE constraint (client_email, order_id, base_flavor).

    Returns number of new order items saved.
    """
    from tools.gmail import GmailClient
    from tools.email_parser import try_parse_order
    from db.stock import get_client_flavor_history, save_order_items

    # Guard: if order history already exists, backfill is not needed.
    if get_client_flavor_history(client_email):
        logger.debug("Backfill skipped for %s: order history already populated", client_email)
        return 0

    try:
        gmail = GmailClient(account=gmail_account)
        notifications = gmail.search_order_notifications(client_email, max_results=30)
    except Exception as e:
        logger.warning("Gmail order search failed for %s: %s", client_email, e)
        return 0

    total_saved = 0
    for msg in notifications:
        # Reconstruct email_text format expected by try_parse_order.
        # Reply-To header ensures client email is found even if body
        # doesn't have the "Email:" field.
        fake_email = (
            f"From: {msg.get('from', 'noreply@shipmecarton.com')}\n"
            f"Reply-To: {client_email}\n"
            f"Subject: {msg.get('subject', '')}\n"
            f"Body:\n{msg.get('body', '')}"
        )
        parsed = try_parse_order(fake_email)
        if parsed and parsed.order_items:
            # Safety check: ensure parsed client email matches expected.
            # Should always pass (Reply-To is set above), but guards
            # against edge cases where body contains a different email.
            if parsed.client_email and parsed.client_email.lower() != client_email.lower():
                logger.warning(
                    "Backfill email mismatch: expected %s, got %s — skipping message",
                    client_email, parsed.client_email,
                )
                continue
            saved = save_order_items(
                client_email=client_email,
                order_id=parsed.order_id,
                order_items=[
                    {
                        "product_name": oi.product_name,
                        "base_flavor": oi.base_flavor,
                        "quantity": oi.quantity,
                    }
                    for oi in parsed.order_items
                ],
            )
            total_saved += saved

    if total_saved:
        logger.info(
            "Backfilled %d order items for %s from Gmail",
            total_saved, client_email,
        )
    return total_saved


# ---------------------------------------------------------------------------
# Profiler Instructions
# ---------------------------------------------------------------------------
profiler_instructions = """\
You are a client profiler for shipmecarton.com.

Your job is to read a client's email history and produce a SHORT text summary
(2-4 sentences max) describing:
- What kind of customer they are (new, returning, frequent)
- What they typically order (flavors, quantities)
- How they pay and communicate (prompt, friendly, asks for discounts, etc.)
- Any notable patterns or preferences

Return ONLY the summary text. No headers, no bullet points, no JSON.
Keep it under 100 words. Write in English.

Example:
"Returning customer, orders every 2-3 weeks. Prefers Green and Turquoise flavors,
usually 2-3 cartons per order. Postpay, pays promptly via Zelle after delivery.
Friendly communication style, occasionally asks about new flavors."
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
profiler_agent = Agent(
    id="client-profiler",
    name="Client Profiler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=profiler_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Main Function
# ---------------------------------------------------------------------------
def generate_client_summary(client_email: str, gmail_account: str = "default") -> str | None:
    """Generate/update LLM summary for a client.

    Reads email history, runs profiler agent, saves summary to DB.

    Args:
        client_email: Client email address.

    Returns:
        Generated summary text, or None on failure.
    """
    # Get email history
    history = get_full_email_history(client_email, max_results=20, gmail_account=gmail_account)

    if not history:
        logger.info("No email history for %s, skipping summary", client_email)
        return None

    history_text = format_email_history(history)

    prompt = (
        f"Client email: {client_email}\n"
        f"Total messages: {len(history)}\n\n"
        f"{history_text}\n\n"
        f"Write a brief summary of this client:"
    )

    try:
        response = profiler_agent.run(prompt)
        summary = response.content.strip()

        if not summary:
            logger.warning("Profiler returned empty summary for %s", client_email)
            return None

        # Save to DB
        saved = update_client_summary(client_email, summary)
        if not saved:
            logger.error("Failed to save summary for %s: client not found", client_email)
            return None

        logger.info("Generated summary for %s: %s", client_email, summary[:80])

        # Backfill ClientOrderItem from Gmail order notifications
        # (covers clients who existed before the pipeline automation)
        _backfill_order_items(client_email, gmail_account=gmail_account)

        return summary

    except Exception as e:
        logger.error("Failed to generate summary for %s: %s", client_email, e)
        return None


# ---------------------------------------------------------------------------
# Auto-refresh guard
# ---------------------------------------------------------------------------
_REFRESH_INTERVAL = timedelta(hours=24)


def maybe_refresh_summary(client_email: str, gmail_account: str = "default") -> str | None:
    """Refresh client summary if stale (>24h) or never generated.

    Cost when skipping: 1 SQL query, 0 LLM tokens.

    Returns:
        New summary text if refreshed, None if skipped or failed.
    """
    profile = get_client_profile(client_email)
    if not profile:
        logger.debug("maybe_refresh_summary: client %s not found", client_email)
        return None

    updated_at = profile.get("summary_updated_at")
    if updated_at is not None:
        # Normalize to UTC-aware for comparison
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated_at
        if age < _REFRESH_INTERVAL:
            logger.debug(
                "Skipping summary refresh for %s (age: %s)",
                client_email, age,
            )
            return None

    logger.info("Auto-refreshing summary for %s", client_email)
    return generate_client_summary(client_email, gmail_account=gmail_account)
