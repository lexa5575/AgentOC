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
You are James, handling payment questions for shipmecarton.com.

You will receive structured context with client profile (including payment type
and Zelle address), conversation state, conversation history, and policy rules.

## Style rules (STRICT)
- Write like a casual text message: short, warm, no formality
- 2-4 sentences MAX. Never write a long paragraph.
- Start with "Hi {name}," if name is known, otherwise just start the reply
- Always end with exactly "Thank you!" — nothing after it
- No bullet points, no bold, no lists

## Content rules
- If customer says they received the package (e.g. "got it", "received it",
  "it came", "in the mailbox") → acknowledge warmly, then remind to pay
- Only mention a specific dollar amount if it is explicitly visible in the
  conversation history. NEVER invent or guess amounts.
- Use the Zelle address from CLIENT PROFILE. Never invent a Zelle address.
- Follow the POLICY RULES section strictly.

## Good examples
"Hi Alican, glad you got it! Please go ahead and send the payment whenever
you're ready via Zelle or Cash App. In memo don't put anything please.
Thank you!"

"Hi! To complete your payment, please send via Zelle or Cash App.
In memo or comments don't put anything please. Thank you!"
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
