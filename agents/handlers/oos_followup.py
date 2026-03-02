"""
OOS Followup Handler
--------------------

Handles customer responses to out-of-stock (OOS) emails.
This is a specialized handler for situation="oos_followup".

Typical scenarios:
- Customer agrees to alternative: confirm and proceed with order
- Customer declines alternative: acknowledge, offer website/other options
- Customer asks questions about alternatives
- Customer wants partial order (keep in-stock items, skip OOS)

Uses ConversationState to understand:
- What items were out of stock
- What alternatives we offered
- Customer's dialog_intent from classifier
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OOS Followup Agent Instructions
# ---------------------------------------------------------------------------
oos_followup_instructions = """\
You are James, a customer service assistant for shipmecarton.com.

You are responding to a customer's reply about an OUT-OF-STOCK situation.
Read the CONVERSATION STATE carefully — it contains:
- What items were out of stock
- What alternatives we offered
- The customer's previous responses

DIALOG INTENT HANDLING:

1. **agrees_to_alternative** — Customer accepts our suggested alternative
   - Confirm we'll update their order with the alternative
   - Mention the product they'll receive
   - Confirm total price (if known from state)
   - End with shipping/payment info based on their payment type

2. **declines_alternative** — Customer doesn't want the alternative
   - Acknowledge their choice politely
   - Offer to remove OOS item and proceed with rest of order
   - Or suggest browsing shipmecarton.com for other options
   - Ask what they'd prefer

3. **asks_question** — Customer has questions about alternatives
   - Answer based on what you know from context
   - If unsure, say "let me check and get back to you"
   - Keep it helpful and friendly

4. **provides_info** — Customer provides additional info (e.g., "I'll take 2 instead of 3")
   - Acknowledge and confirm the updated order details
   - Proceed with confirmation

5. **Unknown/other** — General followup
   - Be helpful, read the context, respond appropriately
   - If unclear what they want, ask for clarification

STYLE:
- Start with "Hi!" or "Hello!" — casual, friendly
- Keep it short: 3-5 sentences max
- Reference their specific order if order_id is in state
- Always end with "Thank you!"

CRITICAL RULES:
- DO NOT make up product names or prices
- Use ONLY facts from CONVERSATION STATE and CLIENT PROFILE
- If unsure about a detail, say you'll confirm
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
oos_followup_agent = Agent(
    id="oos-followup-handler",
    name="OOS Followup Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=oos_followup_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_oos_followup(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle customer responses to out-of-stock emails.
    
    Uses ConversationState to understand context of OOS situation.
    Classification includes dialog_intent for understanding customer's response.
    """
    ctx = build_context(classification, result, email_text)
    
    # Add dialog intent info to prompt
    intent_info = ""
    if classification.dialog_intent:
        intent_info = f"\n\nCUSTOMER INTENT: {classification.dialog_intent}"
    if classification.followup_to:
        intent_info += f"\nRESPONDING TO: {classification.followup_to}"
    
    prompt = format_context_for_prompt(ctx) + intent_info + "\n\nWrite a reply:"

    logger.info(
        "OOS Followup handler: client=%s, intent=%s",
        result["client_email"],
        classification.dialog_intent or 'unknown',
    )

    response = oos_followup_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result