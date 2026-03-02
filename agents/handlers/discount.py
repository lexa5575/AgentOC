"""
Discount Handler
----------------

Handles discount-related requests:
- "Can I get a discount?"
- "Do you have any promotions?"
- "Can you lower the price?"

This agent has narrow, focused instructions for discount requests only.
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discount Agent Instructions
# ---------------------------------------------------------------------------
discount_instructions = """\
You are James, handling ONLY discount requests for shipmecarton.com.

You will receive structured context with client profile (including current
discount status), conversation state, conversation history, and policy rules.
Use ALL of this context to write your reply.

Check CLIENT PROFILE for discount info:
- If "Active discount: X% for next N orders" — tell the client
- If "Discount: none" — politely decline per policy

Follow the POLICY RULES section strictly.
Style: polite, appreciative. End with "Thank you!"
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
discount_agent = Agent(
    id="discount-handler",
    name="Discount Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=discount_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_discount(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle discount requests with specialized agent."""
    ctx = build_context(classification, result, email_text)
    prompt = format_context_for_prompt(ctx) + "\n\nWrite a reply about their discount request:"

    logger.info(
        "Discount handler for client=%s, discount=%d%% (%d left)",
        result["client_email"], ctx.discount_percent, ctx.discount_orders_left,
    )

    response = discount_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result
