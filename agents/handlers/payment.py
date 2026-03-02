"""
Payment Handler
---------------

Handles payment-related questions:
- "How do I pay?"
- "What's your Zelle?"
- "Can I pay with Cash App?"

This agent has narrow, focused instructions for payment questions only.
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payment Agent Instructions
# ---------------------------------------------------------------------------
payment_instructions = """\
You are James, handling ONLY payment questions for shipmecarton.com.

You will receive structured context with client profile (including payment type
and Zelle address), conversation state, conversation history, and policy rules.
Use ALL of this context to write your reply.

Use the client's payment type (prepay/postpay) from CLIENT PROFILE to determine
the correct payment instructions. Use the Zelle address from CLIENT PROFILE.

Follow the POLICY RULES section strictly.
Style: helpful, clear. End with "Thank you!"
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
payment_agent = Agent(
    id="payment-handler",
    name="Payment Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=payment_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_payment(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle payment questions with specialized agent."""
    ctx = build_context(classification, result, email_text)
    prompt = format_context_for_prompt(ctx) + "\n\nWrite a reply about payment:"

    logger.info(
        "Payment handler for client=%s, payment_type=%s",
        result["client_email"], ctx.payment_type,
    )

    response = payment_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result
