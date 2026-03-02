"""
Shipping Handler
----------------

Handles shipping timeline questions:
- "When will my order ship?"
- "How long does shipping take?"
- "Do you offer expedited shipping?"

This agent has narrow, focused instructions for shipping questions only.
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shipping Agent Instructions
# ---------------------------------------------------------------------------
shipping_instructions = """\
You are James, handling ONLY shipping timeline questions for shipmecarton.com.

You will receive structured context with client profile (including payment type),
conversation state, conversation history, and policy rules.
Use ALL of this context to write your reply.

Use the client's payment type from CLIENT PROFILE to explain when their order ships:
- Prepay: ships after payment confirmed
- Postpay: ships immediately

Follow the POLICY RULES section strictly.
Style: informative, reassuring. End with "Thank you!"
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
shipping_agent = Agent(
    id="shipping-handler",
    name="Shipping Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=shipping_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_shipping(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle shipping timeline questions with specialized agent."""
    ctx = build_context(classification, result, email_text)
    prompt = format_context_for_prompt(ctx) + "\n\nWrite a reply about shipping:"

    logger.info(
        "Shipping handler for client=%s, payment_type=%s",
        result["client_email"], ctx.payment_type,
    )

    response = shipping_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result
