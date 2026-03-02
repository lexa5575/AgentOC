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

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from db.clients import update_client_summary
from db.memory import get_full_email_history
from agents.reply_templates import format_email_history

logger = logging.getLogger(__name__)

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
def generate_client_summary(client_email: str) -> str | None:
    """Generate/update LLM summary for a client.

    Reads email history, runs profiler agent, saves summary to DB.

    Args:
        client_email: Client email address.

    Returns:
        Generated summary text, or None on failure.
    """
    # Get email history
    history = get_full_email_history(client_email, max_results=20)

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
        return summary

    except Exception as e:
        logger.error("Failed to generate summary for %s: %s", client_email, e)
        return None
