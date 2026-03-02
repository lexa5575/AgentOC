"""
Tracking Handler
----------------

Handles tracking-related questions:
- "Where is my order?"
- "What's my tracking number?"
- "When will it arrive?"

This agent has narrow, focused instructions for tracking questions only.
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tracking Agent Instructions
# ---------------------------------------------------------------------------
tracking_instructions = """\
You are James, handling ONLY tracking questions for shipmecarton.com.

You will receive structured context with client profile, conversation state,
conversation history, and policy rules. Use ALL of this context to write your reply.

RESPONSES YOU CAN GIVE:
- If tracking number is in context: "Your tracking number is {X}. You can track it at usps.com"
- If order was shipped: "Your order was shipped on {date}"
- If no tracking yet: "We'll get the tracking info and email it to you shortly"
- If order not found: "We'll check on your order and get back to you"

Follow the POLICY RULES section strictly.
Style: casual, friendly. End with "Thank you!"
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
tracking_agent = Agent(
    id="tracking-handler",
    name="Tracking Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=tracking_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_tracking(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle tracking questions with specialized agent."""
    ctx = build_context(classification, result, email_text)
    prompt = format_context_for_prompt(ctx) + "\n\nWrite a reply about their tracking question:"

    logger.info("Tracking handler for client=%s", result["client_email"])

    response = tracking_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result
