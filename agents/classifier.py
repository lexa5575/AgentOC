"""
Email Classifier
----------------

LLM classifier agent, context builder, and classification runner.
"""

import json
import logging
import re

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.formatters import format_thread_for_classifier
from agents.models import EmailClassification, OrderItem
from db.conversation_state import get_client_states, get_state
from db.memory import get_full_thread_history
from tools.email_parser import clean_email_body, try_parse_order

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent: Classifier (returns structured JSON, no free text)
# ---------------------------------------------------------------------------
classifier_instructions = """\
You are an email classifier for shipmecarton.com.
Analyze the email and return ONLY a flat JSON object. No text before or after.

## What you receive

The email body has been PRE-CLEANED: quoted reply blocks ("On ... wrote:"),
signatures ("Sent from my iPhone"), and ">" quoted lines are already removed.
Focus on the customer's actual message only.

When available, CONVERSATION STATE and THREAD HISTORY are prepended before
"--- NEW EMAIL ---". Use them to understand context — especially for detecting
followups and customer intent.

## Sender identification

If the email is from @shipmecarton.com, noreply@, or no-reply@ — this is a system notification.
The REAL customer is in: "Email:" field in body, or "Reply-To:" header, or "Firstname:" field.
For all other emails, the From address IS the real customer.

## Rules for needs_reply

true: questions, complaints, payment confirmations, product requests, order-related messages
false: simple acknowledgments with NO new question or request

Examples of needs_reply=false:
- "Thank you!" / "Thank you James." / "Thanks!"
- "Got it" / "Perfect" / "OK" / "Sounds good"
- "Great, thanks!" / "Appreciate it"
- Marketing emails, spam, automated notifications

Examples of needs_reply=true (even if starts with "thank you"):
- "Thank you! When will it be shipped?" (has a question)
- "Got it. Can I also add 2 boxes of Green?" (has a request)

## Rules for situation

- "new_order" — customer wants to place an order. Use this when:
  - Direct order: "I want to order X", "Please send me X", "I'll take X"
  - Question with specific product AND quantity: "Is it possible to order 2 boxes of X?",
    "Can I get 4 cartons of Y?", "Could you send 1 box of Z?"
  KEY RULE: if the customer specifies both a product name AND a quantity (number of boxes/cartons/units),
  classify as new_order — not price_question. Specific quantities = purchase intent.
- "tracking" — asks about delivery status, tracking number, "where is my order?"
- "price_question" — asks HOW MUCH something costs WITHOUT specifying quantity, requests a price quote
  ("how much for Green?", "what's the price of Blue?", "can you give me a price?")
  Only use this when no specific quantity is mentioned. If quantity is present → use new_order instead.
- "stock_question" — asks WHETHER a specific product is available/in stock, WITHOUT ordering intent
  ("do you have Tropical?", "is Blue available?", "do you carry Silver?", "any Turquoise?")
  KEY: no quantity, no price query — pure availability question. If quantity is present → new_order.
  If it's inside an oos_followup thread → use oos_followup instead.
- "payment_question" — asks WHERE or HOW to pay ("how do I pay?", "what's the Zelle?")
- "payment_received" — confirms payment was sent ("I paid via Zelle", "sent CashApp")
- "discount_request" — asks for discount or better price (NOT a price quote request)
- "shipping_timeline" — asks WHEN order will be shipped ("when do you ship?")
- "oos_followup" — reply in a thread where we discussed out-of-stock or alternatives.
  Use when customer responds about product availability, alternatives, or substitutions.
  Examples: "Yes, I'll take the green", "Do you have silver?", "That works for me",
  "Yes, that is perfect", "Please send final total"
- "other" — anything that doesn't fit above (general questions, complaints, etc.)

## Rules for followup detection

Use CONVERSATION STATE and THREAD HISTORY to detect followups.

is_followup: true if this is a response to our previous message
followup_to: what our message was about:
  - "oos_email" — we told them something was out of stock or offered alternatives
  - "payment_info" — we sent payment instructions
  - "tracking_info" — we sent tracking number
  - "order_confirmation" — we confirmed their order
  - null — not a followup or unknown

dialog_intent: what the customer wants:
  - "agrees_to_alternative" — accepts our suggestion ("yes", "that works", "I'll take it",
    "sounds good", "that will be fine", "that is perfect")
  - "declines_alternative" — rejects our suggestion ("no thanks", "I'll pass", "cancel")
  - "confirms_payment" — says they paid (overlaps with payment_received situation)
  - "asks_question" — asks about products, availability, pricing
  - "provides_info" — gives us information we asked for
  - null — unclear or not a followup

IMPORTANT: When CONVERSATION STATE mentions out-of-stock or alternatives,
and the customer responds with agreement/product choice/question about products,
use situation="oos_followup" (NOT "other").

EXCEPTION: If CONVERSATION STATE Status is "shipped" or "completed",
the previous order cycle is FINISHED. A new product request with specific
product + quantity (e.g. "can I have 2 terea sienna") is a NEW ORDER,
not a followup to the old OOS discussion. Classify as "new_order".

## Rules for order_items

Most website orders are parsed automatically before reaching you.
You only need to extract order_items for rare non-standard orders.

If the email contains a clear product list/table, extract:
- product_name: full name (e.g. "Tera Green made in Middle East")
- base_flavor: flavor/color only — strip "Tera"/"Terea"/"Heets" prefix and
  "EU"/"made in Middle East"/etc. suffix.
  Examples: "Tera Green made in Middle East" → "Green", "Tera Turquoise EU" → "Turquoise"
  Keep non-Tera brands intact: "ONE Green" → "ONE Green", "PRIME Black" → "PRIME Black"
- quantity: number of units (default 1)

Extract order_items for new_order, price_question, AND stock_question situations.
For stock_question: extract the product being asked about (quantity defaults to 1).
If no clear product list → set order_items to null.

## Output format

Return ONLY this JSON (no markdown, no code fences):

{
  "needs_reply": true,
  "situation": "new_order",
  "is_followup": false,
  "followup_to": null,
  "dialog_intent": null,
  "client_email": "customer@example.com",
  "client_name": "John Smith",
  "order_id": "12345",
  "price": "$220.00",
  "customer_street": "123 Main St",
  "customer_city_state_zip": "Chicago, Illinois 60601",
  "items": "Tera Green made in Middle East x 2",
  "order_items": [
    {"product_name": "Tera Green made in Middle East", "base_flavor": "Green", "quantity": 2}
  ]
}

Field rules:
- client_email: ALWAYS the real customer email (never noreply@, never system email)
- client_name: customer full name or null
- price: include $ sign, or null
- customer_street: street address only, or null
- customer_city_state_zip: "City, State Zip", or null
- items: what was ordered as free text, or null
- order_items: structured list (for new_order and price_question), or null
- is_followup: true/false
- followup_to: "oos_email" / "payment_info" / "tracking_info" / "order_confirmation" / null
- dialog_intent: "agrees_to_alternative" / "declines_alternative" / "confirms_payment" / "asks_question" / "provides_info" / null

CRITICAL: Return a FLAT JSON object with exactly these field names. No extra nesting beyond order_items array.
"""

classifier_agent = Agent(
    id="email-classifier",
    name="Email Classifier",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=classifier_instructions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_value(data: dict, *keys: str):
    """Search for a value by multiple possible key names, including nested dicts."""
    # First: check top-level keys
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    # Fallback: search one level deep in nested dicts
    for v in data.values():
        if isinstance(v, dict):
            for key in keys:
                if key in v and v[key] is not None:
                    return v[key]
    return None


_EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')


def _extract_sender_email(email_text: str) -> str | None:
    """Extract real sender email from email headers.

    Priority: Reply-To (real client in order notifications) > From.
    Only parses headers (before 'Body:' line) to avoid matching
    quoted/forwarded content.
    """
    header_section = email_text.split("\nBody:", 1)[0] if "\nBody:" in email_text else email_text[:500]

    # Priority 1: Reply-To (real customer in noreply@ order notifications)
    for line in header_section.splitlines():
        if line.lower().startswith("reply-to:"):
            match = _EMAIL_RE.search(line)
            if match:
                return match.group(0).lower()

    # Priority 2: From (skip system addresses)
    for line in header_section.splitlines():
        if line.lower().startswith("from:"):
            match = _EMAIL_RE.search(line)
            if match:
                email = match.group(0).lower()
                if not any(skip in email for skip in ("noreply@", "no-reply@", "@shipmecarton.com")):
                    return email

    return None


def _format_other_threads(states: list[dict], exclude_thread_id: str | None) -> str:
    """Format conversation states from other threads as context for classifier."""
    other = [s for s in states if s.get("gmail_thread_id") != exclude_thread_id]
    if not other:
        return ""
    lines = ["--- OTHER ACTIVE THREADS ---"]
    for s in other[:3]:
        state = s.get("state", {})
        situation = s.get("last_situation", "unknown")
        lines.append(f"Thread ({situation}):")
        if state.get("facts"):
            lines.append(f"  Facts: {json.dumps(state['facts'], ensure_ascii=False)}")
        if state.get("summary"):
            lines.append(f"  Summary: {state['summary']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_classifier_context(
    gmail_thread_id: str | None,
    email_text: str,
) -> tuple[str, dict | None]:
    """Build context string for the classifier from DB state and thread history.

    Returns:
        (context_str, pre_state_record) where context_str is prepended to the
        email text for the classifier, and pre_state_record is reused in
        classify_and_process to avoid a duplicate DB query.
    """
    conversation_context = ""
    pre_state_record = None

    if gmail_thread_id:
        try:
            pre_state_record = get_state(gmail_thread_id)
            state_record = pre_state_record
            if state_record and state_record.get("state"):
                state = state_record["state"]
                conversation_context = (
                    f"--- CONVERSATION STATE ---\n"
                    f"Status: {state.get('status', 'unknown')}\n"
                    f"Topic: {state.get('topic', 'unknown')}\n"
                    f"Facts: {json.dumps(state.get('facts', {}), ensure_ascii=False)}\n"
                    f"Open questions: {state.get('open_questions', [])}\n"
                    f"Summary: {state.get('summary', '')}\n\n"
                )
        except Exception as e:
            logger.warning("Failed to get conversation state for classifier: %s", e)

        # Thread history (full messages — Classifier needs complete context)
        try:
            thread_history = get_full_thread_history(gmail_thread_id, max_results=15)
            if thread_history:
                conversation_context += format_thread_for_classifier(thread_history) + "\n\n"
        except Exception as e:
            logger.warning("Failed to get thread history for classifier: %s", e)

    # Cross-thread context: other active threads for same client
    sender_email = _extract_sender_email(email_text)
    if not sender_email and pre_state_record:
        sender_email = pre_state_record.get("client_email")

    if sender_email:
        try:
            all_client_states = get_client_states(sender_email, limit=4)
            cross_thread = _format_other_threads(all_client_states, gmail_thread_id)
            if cross_thread:
                conversation_context += cross_thread + "\n\n"
        except Exception as e:
            logger.warning("Failed to get cross-thread context: %s", e)

    return conversation_context, pre_state_record


# ---------------------------------------------------------------------------
# Classification runner
# ---------------------------------------------------------------------------

def run_classification(
    email_text: str,
    context_str: str,
) -> EmailClassification:
    """Run deterministic parser or LLM classifier to produce an EmailClassification.

    Args:
        email_text: Original email text (used by deterministic parser).
        context_str: Classifier context prepended before the new email.

    Returns:
        Validated EmailClassification instance.
    """
    # Step 0.9: Try deterministic parsing for website orders (0 tokens)
    parsed_classification = try_parse_order(email_text)
    if parsed_classification:
        logger.info(
            "Order parsed by regex (0 tokens): email=%s, order=%s",
            parsed_classification.client_email,
            parsed_classification.order_id,
        )
        return parsed_classification

    # Clean email body for LLM classifier (remove quoted blocks, signatures)
    cleaned_email = clean_email_body(email_text)

    # Step 1: LLM classifies (returns JSON text)
    logger.info("Classifying email...")
    classifier_input = (
        context_str + "--- NEW EMAIL ---\n" + cleaned_email
        if context_str
        else cleaned_email
    )
    response = classifier_agent.run(classifier_input)
    raw = response.content

    # Parse JSON from LLM response (strip markdown code fences if present)
    json_str = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
    data = json.loads(json_str)

    # Robust field extraction: try expected names + common LLM variations + nested
    # Parse order_items (structured list) before building classification
    raw_order_items = _find_value(data, "order_items", "structured_items")
    order_items_parsed = None
    if raw_order_items and isinstance(raw_order_items, list):
        try:
            order_items_parsed = [
                OrderItem(
                    product_name=item.get("product_name", ""),
                    base_flavor=item.get("base_flavor", ""),
                    quantity=item.get("quantity", 1),
                )
                for item in raw_order_items
                if item.get("base_flavor")
            ]
            if not order_items_parsed:
                order_items_parsed = None
        except Exception as e:
            logger.warning("Failed to parse order_items: %s", e)
            order_items_parsed = None

    classification = EmailClassification(
        needs_reply=_find_value(data, "needs_reply") if _find_value(data, "needs_reply") is not None else True,
        situation=_find_value(data, "situation", "classification", "category") or "other",
        client_email=_find_value(data, "client_email", "real_customer_email", "customer_email", "email") or "",
        client_name=_find_value(data, "client_name", "customer_name", "name", "firstname"),
        order_id=_find_value(data, "order_id", "order_number"),
        price=_find_value(data, "price", "payment_amount", "total", "amount"),
        customer_street=_find_value(data, "customer_street", "street", "street_address", "address"),
        customer_city_state_zip=_find_value(data, "customer_city_state_zip", "city_state_zip"),
        items=_find_value(data, "items", "products"),
        order_items=order_items_parsed,
        is_followup=_find_value(data, "is_followup") or False,
        followup_to=_find_value(data, "followup_to"),
        dialog_intent=_find_value(data, "dialog_intent"),
    )

    logger.info(
        "Classified: email=%s, situation=%s, needs_reply=%s",
        classification.client_email, classification.situation, classification.needs_reply,
    )
    return classification
