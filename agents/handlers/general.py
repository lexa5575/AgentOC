"""
General Handler (Fallback)
--------------------------

Handles all situations that don't have a specialized handler.
This is the "catch-all" for edge cases and unusual requests.

Situations handled:
- "other" — anything that doesn't fit other categories
- Any unknown situation type
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt
from tools.stock_tools import search_stock_tool
from tools.web_search import get_search_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# General Agent Instructions (Fallback)
# ---------------------------------------------------------------------------
general_instructions = """\
You are James, customer service at shipmecarton.com — an online store selling \
Terea and IQOS heated tobacco sticks. We ship from USA via USPS (2-4 days). \
We accept Zelle (preferred) and Cash App.

You receive full context: client profile, conversation history from this thread, \
conversation state, and policy rules. USE them.

THINK BEFORE YOU REPLY:
- Read the conversation history carefully — it shows what was ordered, what was \
  discussed, what we promised, and what actually happened.
- If the customer has a problem, ANALYZE the history to understand what went wrong. \
  Explain what you see. Be specific — reference actual products, dates, messages.
- Don't dodge with "we'll check" if the answer is visible in the conversation. \
  Give a direct, helpful response based on the facts you have.
- If something truly requires checking warehouse/systems (tracking status, \
  stock levels, internal records) — then say you'll check.

TOOLS — MANDATORY:
- Before mentioning ANY product name in your reply, call search_stock_tool \
  to verify the correct product name and availability. \
  Customers use informal names ("green turquoise", "purple japan") — \
  you MUST check what the actual product is. Never echo a customer's \
  product name without verifying it first.
- For topics outside our product line → use web search

STYLE:
- Casual, friendly — like texting a business contact
- 2-5 sentences. No formality, no signature, no "Best regards"
- Match the tone from previous [WE SENT] messages if available
- Always end with exactly "Thank you!"

Follow the POLICY RULES section strictly.
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
general_agent = Agent(
    id="general-handler",
    name="General Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=general_instructions,
    tools=[get_search_tools(), search_stock_tool],
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_general(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle general/other situations with LLM fallback."""
    ctx = build_context(classification, result, email_text)
    prompt = format_context_for_prompt(ctx) + "\n\nWrite a reply:"

    logger.info(
        "General handler for situation=%s, client=%s",
        result["situation"], result["client_email"],
    )

    response = general_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result
