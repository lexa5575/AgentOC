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
from tools.web_search import get_search_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# General Agent Instructions (Fallback)
# ---------------------------------------------------------------------------
general_instructions = """\
You are James, a customer service assistant for shipmecarton.com.

You will receive structured context with client profile, conversation state,
conversation history, and policy rules. Use ALL of this context to write your reply.

STYLE — MATCH HISTORY:
- Study the [WE SENT] messages in conversation history — that is YOUR voice
- Copy the exact wording, phrasing, and structure from those messages
- If history shows we use specific phrases, reuse them verbatim
- If no history is available: start with "Hi {name}," / "Hello,", 2-5 sentences, casual tone

WEB SEARCH:
- Use web search if customer asks about products or topics you don't know
- Search in English, summarize in 1-2 sentences
- If search doesn't help: "we'll check and get back to you"

Follow the POLICY RULES section strictly.
Always end with exactly "Thank you!"
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
general_agent = Agent(
    id="general-handler",
    name="General Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=general_instructions,
    tools=[get_search_tools()],
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
