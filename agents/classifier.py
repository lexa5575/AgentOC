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

CRITICAL: Classify based on the CURRENT email content (after "--- NEW EMAIL ---"),
NOT the thread subject or history. Customers often reply to old threads with
completely new requests. Thread history is BACKGROUND CONTEXT only — the situation
must reflect what the customer is saying in THIS message.
Example: customer replies to "PAYMENT REMINDER" thread with "please send 4 cartons
of Silver" → this is new_order, NOT payment_received.

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
- "Yes pls. Ty" in an oos_followup thread (agreeing to alternative → need to confirm order)

IMPORTANT: When situation is oos_followup and customer AGREES to an alternative
("yes", "yes pls", "sounds good", "that works"), needs_reply is ALWAYS true —
we must confirm the order details. These are NOT simple acknowledgments.

## Rules for situation

- "new_order" — customer wants to place an order. Use this when:
  - Direct order: "I want to order X", "Please send me X", "I'll take X"
  - Question with specific product AND quantity: "Is it possible to order 2 boxes of X?",
    "Can I get 4 cartons of Y?", "Could you send 1 box of Z?"
  KEY RULE: if the customer specifies both a product name AND a quantity (number of boxes/cartons/units),
  classify as new_order — not price_question. Specific quantities = purchase intent.
  NOT new_order: "hold for me", "reserve", "save", "set aside", "keep for me", "hold on to" —
  these are requests to reserve product for a FUTURE order, not an actual order. Classify as "other".
- "tracking" — asks about delivery status, tracking number, "where is my order?"
- "price_question" — asks HOW MUCH something costs WITHOUT specifying quantity, requests a price quote
  ("how much for Green?", "what's the price of Blue?", "can you give me a price?")
  Only use this when no specific quantity is mentioned. If quantity is present → use new_order instead.
- "stock_question" — asks WHETHER a product or product region is available/in stock, WITHOUT ordering intent.
  Specific product: "do you have Tropical?", "is Blue available?", "do you carry Silver?"
  Region/category: "which Japan do you have?", "what terea japan ship from CA?",
  "do you have EU?", "what's available from Armenia?", "any Japanese sticks?"
  KEY: no quantity, no price query — pure availability question. If quantity is present → new_order.
  If it's inside an oos_followup thread → use oos_followup instead.
  For PURE region queries (no specific flavor), set base_flavor = region name (e.g. "Japan", "EU", "Armenia").
  For specific product within a region (e.g. "japan regular", "EU silver"), set base_flavor = the FLAVOR
  (e.g. "Regular", "Silver"), NOT the region. The region is context, the flavor is what they're asking about.
  Examples:
  - "what Japan do you have?" → base_flavor = "Japan" (pure region query, list everything)
  - "do you have japan regular?" → base_flavor = "Regular" (specific product, not a region dump)
  - "any European available?" → base_flavor = "Europe" (pure region query)
  - "is EU silver in stock?" → base_flavor = "Silver" (specific product)
- "payment_question" — asks WHERE or HOW to pay ("how do I pay?", "what's the Zelle?")
- "payment_received" — confirms payment was sent ("I paid via Zelle", "sent CashApp")
- "discount_request" — asks for discount or better price (NOT a price quote request)
- "shipping_timeline" — asks WHEN order will be shipped ("when do you ship?")
- "oos_followup" — reply in a thread where we discussed out-of-stock or alternatives.
  Use when customer responds about product availability, alternatives, or substitutions.
  Examples: "Yes, I'll take the green", "Do you have silver?", "That works for me",
  "Yes, that is perfect", "Please send final total"
- "other" — anything that doesn't fit above (general questions, complaints, etc.)

## Multi-intent priority

When a message contains MULTIPLE intents, use this priority order:
1. payment_received — if customer confirms payment ("money sent", "I paid", "sent via Zelle")
   AND also asks about tracking/shipping → classify as payment_received, NOT tracking.
   Asking for tracking after paying is natural and the handler will address both.
2. new_order — if customer places an order AND asks something else → new_order wins.
3. For other combinations, pick the intent that requires ACTION from us (not just info).

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
  - "agrees_to_alternative" — accepts our suggestion OR confirms a specific order
    in the context of an OOS discussion. Examples:
    Simple agreement: "yes", "that works", "I'll take it", "sounds good"
    Order confirmation: "ok send me 4 black menthol", "please send me the 4 boxes",
      "I'll take 4 of those", "ok. Please send me the 4 black menthol please"
    KEY RULE: In an OOS/alternatives thread, if the customer says "send me X" or
    "I'll take X" with specific product+quantity, this is agrees_to_alternative
    (NOT provides_info). The customer is confirming what they want shipped.
  - "declines_alternative" — rejects our suggestion ("no thanks", "I'll pass", "cancel")
  - "confirms_payment" — says they paid (overlaps with payment_received situation)
  - "asks_question" — asks about products, availability, pricing
  - "provides_info" — gives us information we asked for (address, phone, etc.)
    NOT for product/quantity choices — those are agrees_to_alternative.
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
- base_flavor: flavor/variant name — strip "Tera"/"Terea"/"Heets" prefix and
  "EU"/"made in Middle East"/etc. region suffix. Keep compound flavor names INTACT:
  "Yellow Menthol", "Purple Menthol", "Bright Menthol", "Fusion Menthol",
  "Black Purple Menthol", "Black Ruby Menthol", "Black Tropical Menthol" — these are
  SINGLE flavors, do NOT split them.
  Examples: "Tera Green made in Middle East" → "Green", "Tera Turquoise EU" → "Turquoise",
  "yellow menthol" → "Yellow Menthol", "black purple menthol" → "Black Purple Menthol"
  Keep non-Tera brands intact: "ONE Green" → "ONE Green", "PRIME Black" → "PRIME Black"
- quantity: number of units (default 1)
- region_preference: ordered list of preferred region codes when customer
  expresses a SOFT regional preference in conversational text.
  Valid codes: "EU", "ME", "JAPAN". First element = most preferred.
  null when no preference or when region is already in product_name.
- strict_region: true if customer ONLY wants the specified region
  (e.g. "EU only", "only Japan"), false if alternatives acceptable.

  Examples:
  - "Turquoise EU" → product_name="Turquoise EU", region_preference=null
    (region IN name → resolver handles it, no preference needed)
  - "Turquoise, ME is ok if no EU" → product_name="Turquoise",
    region_preference=["EU","ME"], strict_region=false
  - "Turquoise EU only" → product_name="Turquoise",
    region_preference=["EU"], strict_region=true
  - "Turquoise" (no region mention) → region_preference=null
  - Thread context: if the customer doesn't specify a region but THREAD HISTORY
    shows we previously quoted/offered a specific region variant (e.g. our reply
    had "Terea Yellow ME"), use that region as region_preference.
    Example: customer says "2 yellow again", thread shows "2 x Terea Yellow ME"
    → region_preference=["ME"]

  IMPORTANT: Do NOT set region_preference when region is part of product_name
  (e.g. "Turquoise EU", "Green made in Middle East"). Only for SOFT preferences.

Extract order_items for new_order, payment_received, price_question, stock_question, AND oos_followup situations.
For payment_received: ALWAYS extract order_items when the customer confirms payment.
If the customer mentions specific products in the message, use those.
Otherwise, look at CONVERSATION STATE (ordered_items, last_exchange) and THREAD HISTORY
to identify what the customer is paying for — extract those as order_items.
Example: customer says "Paid.", state has ordered_items=["Terea Yellow x2"],
our previous reply had "2 x Terea Yellow ME" → extract
[{"base_flavor": "Yellow", "quantity": 2, "region_preference": ["ME"]}]
For stock_question: extract ALL products or regions being asked about as separate
order_items (quantity defaults to 1). If the customer asks about multiple categories
(e.g. "any European? and Japan regular?"), create one order_item per category/product.
Example: "any European? and japan regular?" →
  [{"base_flavor": "Europe", ...}, {"base_flavor": "Regular", ...}]
  (Europe = region query; Regular = specific product, NOT "Japan")
For region queries, use the region name as base_flavor (e.g. "Europe", "Japan").
For oos_followup with dialog_intent=agrees_to_alternative: extract the items the customer
is confirming as order_items. Look at CONVERSATION STATE and previous emails to identify
what products and quantities were offered as alternatives — those are the confirmed items.
Example: if our previous email offered "2 Japanese Tropical + 1 Japanese Black" and customer
says "Yes pls", extract:
  [{"base_flavor": "Tropical", "product_name": "Japanese Tropical", "quantity": 2},
   {"base_flavor": "Black", "product_name": "Japanese Black", "quantity": 1}]
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
    {"product_name": "Tera Green made in Middle East", "base_flavor": "Green", "quantity": 2,
     "region_preference": null, "strict_region": false}
  ]
}

Field rules:
- client_email: ALWAYS the real customer email (never noreply@, never system email)
- client_name: customer full name or null
- price: include $ sign, or null
- customer_street: street address only, or null
- customer_city_state_zip: "City, State Zip", or null
- items: what was ordered as free text, or null
- order_items: structured list (for new_order, payment_received, price_question, stock_question, oos_followup), or null
- is_followup: true/false
- followup_to: "oos_email" / "payment_info" / "tracking_info" / "order_confirmation" / null
- dialog_intent: "agrees_to_alternative" / "declines_alternative" / "confirms_payment" / "asks_question" / "provides_info" / null
- order_items[].region_preference: list of "EU"/"ME"/"JAPAN" or null. Only for soft preferences.
- order_items[].strict_region: boolean, default false

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
    gmail_account: str = "default",
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
            thread_history = get_full_thread_history(
                gmail_thread_id, max_results=15, gmail_account=gmail_account,
            )
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
                    region_preference=item.get("region_preference"),
                    strict_region=item.get("strict_region", False),
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
