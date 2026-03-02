"""
State Updater Agent
--------------------

LLM agent that updates conversation state JSON after each email.
This is the 2nd LLM call in the pipeline (after Classifier).

The state contains:
- status: current conversation status
- topic: what the conversation is about
- facts: extracted facts (order_id, items, prices, etc.)
- promises: what we've promised to the customer
- last_exchange: summary of last inbound/outbound
- open_questions: unanswered customer questions
- summary: brief human-readable summary
"""

import json
import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State Updater Instructions
# ---------------------------------------------------------------------------
state_updater_instructions = """\
You are a conversation state updater for shipmecarton.com email system.

Your job is to maintain a compact JSON state that tracks the conversation.

## Input

You receive:
1. CURRENT STATE (JSON) — the existing state, or {} for new threads
2. NEW MESSAGE — the email that just arrived (inbound or outbound)
3. CLASSIFICATION — situation type and metadata

## Output

Return ONLY a valid JSON object. No explanation, no markdown, no code fences.

## State Structure

{
  "status": "awaiting_payment" | "awaiting_oos_decision" | "shipped" | "delivered" | "resolved" | "pending_response" | "new",
  "topic": "new_order" | "tracking" | "payment" | "discount" | "shipping" | "general",
  "facts": {
    "order_id": "#12345" or null,
    "ordered_items": ["Green x2", "Silver x3"],
    "oos_items": ["Green"],
    "offered_alternatives": ["Turquoise from Armenia"],
    "price": "$220" or null,
    "final_price": "$209" or null,
    "discount_applied": "5%" or null,
    "payment_method": "Zelle" or null,
    "shipped_at": "2024-01-15" or null,
    "tracking_number": "9400111..." or null
  },
  "promises": ["delivery in 3-5 days", "ship today after payment"],
  "last_exchange": {
    "we_said": "Summary of our last message",
    "they_said": "Summary of their last message"
  },
  "open_questions": ["Which alternative do you prefer?"],
  "summary": "Returning client, new order with OOS. Client agreed to Turquoise alternative."
}

## Rules

1. PRESERVE all existing facts — never delete information, only add or update
2. UPDATE status based on conversation flow:
   - new → awaiting_payment (after we send payment info)
   - awaiting_payment → shipped (after payment confirmed)
   - awaiting_oos_decision → new (after client chooses alternative)
3. EXTRACT facts from emails:
   - Order IDs, prices, items from order notifications
   - Tracking numbers from shipping confirmations
   - Payment confirmations
4. TRACK promises we make — these are important for consistency
5. IDENTIFY open questions — things the customer asked that we haven't answered
6. Keep summary under 100 words — it's for quick context
7. If direction is "outbound", update "we_said" in last_exchange
8. If direction is "inbound", update "they_said" in last_exchange
9. Do NOT invent facts — if you don't know something, leave it null

## Example

Input state: {}
New message (inbound): "Order #12345 placed, $220, Green x2"
Classification: new_order

Output:
{
  "status": "new",
  "topic": "new_order",
  "facts": {
    "order_id": "#12345",
    "ordered_items": ["Green x2"],
    "oos_items": [],
    "offered_alternatives": [],
    "price": "$220",
    "final_price": null,
    "discount_applied": null,
    "payment_method": null,
    "shipped_at": null,
    "tracking_number": null
  },
  "promises": [],
  "last_exchange": {
    "we_said": null,
    "they_said": "Placed order #12345 for Green x2, total $220"
  },
  "open_questions": [],
  "summary": "New order #12345 for Green x2, $220. Awaiting our response."
}
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
state_updater_agent = Agent(
    id="state-updater",
    name="State Updater",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=state_updater_instructions,
    markdown=False,
)


# ---------------------------------------------------------------------------
# Main Function
# ---------------------------------------------------------------------------
def update_conversation_state(
    current_state: dict | None,
    email_text: str,
    situation: str,
    direction: str,
    client_email: str | None = None,
    order_id: str | None = None,
    price: str | None = None,
) -> dict:
    """Update conversation state after a new message.

    Args:
        current_state: Current state JSON dict, or None for new thread.
        email_text: The email text (body).
        situation: Classification situation (new_order, tracking, etc.).
        direction: "inbound" or "outbound".
        client_email: Client email (for context).
        order_id: Order ID if known.
        price: Price if known.

    Returns:
        Updated state dict.
    """
    # Build prompt
    current_json = json.dumps(current_state or {}, ensure_ascii=False, indent=2)

    # Truncate email if too long
    email_preview = email_text[:2000] if len(email_text) > 2000 else email_text

    prompt = f"""CURRENT STATE:
{current_json}

NEW MESSAGE ({direction}):
{email_preview}

CLASSIFICATION:
- situation: {situation}
- client_email: {client_email or "unknown"}
- order_id: {order_id or "unknown"}
- price: {price or "unknown"}

Update the state JSON. Return ONLY the JSON object:"""

    try:
        response = state_updater_agent.run(prompt)
        raw = response.content

        # Parse JSON (strip code fences if present)
        import re
        json_str = re.sub(r"^```json?\s*|\s*```$", "", raw.strip())
        updated_state = json.loads(json_str)

        logger.info(
            "State updated: status=%s, topic=%s",
            updated_state.get("status"), updated_state.get("topic"),
        )
        return updated_state

    except json.JSONDecodeError as e:
        logger.error("Failed to parse state updater response: %s", e)
        # Return current state unchanged
        return current_state or _empty_state()

    except Exception as e:
        logger.error("State updater failed: %s", e, exc_info=True)
        return current_state or _empty_state()


def _empty_state() -> dict:
    """Return an empty state structure."""
    return {
        "status": "new",
        "topic": "general",
        "facts": {
            "order_id": None,
            "ordered_items": [],
            "oos_items": [],
            "offered_alternatives": [],
            "price": None,
            "final_price": None,
            "discount_applied": None,
            "payment_method": None,
            "shipped_at": None,
            "tracking_number": None,
        },
        "promises": [],
        "last_exchange": {
            "we_said": None,
            "they_said": None,
        },
        "open_questions": [],
        "summary": "",
    }