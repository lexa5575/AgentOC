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

from agents.formatters import compose_classifier_context, format_thread_for_classifier
from agents.models import EmailClassification, OrderItem
from db.conversation_state import get_client_states, get_state
from db.memory import get_full_thread_history
from tools.email_parser import (
    REGION_SUFFIXES,
    _extract_base_flavor,
    clean_email_body,
    strip_quoted_text,
    try_parse_order,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent: Classifier (returns structured JSON, no free text)
# ---------------------------------------------------------------------------
# Responsibility split:
#   Python (deterministic): client_email (_extract_sender_email — source of truth),
#     items derivation (_derive_items_text), region_preference normalization
#     (Pydantic validator), quoted text cleanup, website orders (try_parse_order).
#   LLM (interpretation): situation, dialog_intent, followup_to, order_items,
#     needs_reply, order_id, price, address, client_name, multi-intent resolution.
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

## Rules for needs_reply

true: questions, complaints, payment confirmations, product requests, order-related messages.
false: simple acknowledgments with NO new question or request (e.g. "Thanks!", "Got it", "OK").

If the message contains ANY question or request beyond acknowledgment → true.
Example: "Thank you! When will it be shipped?" → true (has a question).

EXCEPTION: oos_followup + customer AGREES ("yes", "yes pls", "sounds good") → ALWAYS true.
We must confirm the order — this is NOT a simple acknowledgment.

## Rules for situation

- "new_order" — customer wants to place an order.
  KEY RULE: product name + quantity = new_order (NOT price_question).
  NOT new_order: "hold/reserve/set aside" → classify as "other".
- "tracking" — asks about delivery status, tracking number, "where is my order?"
- "price_question" — asks how much something costs WITHOUT quantity.
  If quantity present → new_order instead.
- "stock_question" — asks if a product/region is available, WITHOUT ordering intent.
  No quantity, no price query — pure availability. If quantity → new_order.
  In oos_followup thread → use oos_followup instead.
  IMPORTANT: "oos_followup thread" means the SAME thread where OOS was discussed.
  A new email about a different product in a DIFFERENT thread is stock_question,
  even if OTHER ACTIVE THREADS show OOS discussions.
  Region queries: base_flavor = region name ("Japan", "Europe").
  Specific product in region ("japan regular"): base_flavor = "Regular" (the flavor, NOT region).
- "payment_question" — asks WHERE or HOW to pay
- "payment_received" — confirms payment was sent
- "discount_request" — asks for discount (NOT a price quote)
- "shipping_timeline" — asks WHEN order will be shipped
- "oos_followup" — reply in thread where we discussed out-of-stock/alternatives
- "other" — anything that doesn't fit above

## Multi-intent priority

When a message contains MULTIPLE intents, use this priority order:
1. payment_received — if customer confirms payment ("money sent", "I paid", "sent via Zelle")
   AND also asks about tracking/shipping → classify as payment_received, NOT tracking.
   Asking for tracking after paying is natural and the handler will address both.
2. new_order — if customer places an order AND asks something else → new_order wins.
3. For other combinations, pick the intent that requires ACTION from us (not just info).

## Rules for followup detection

Use CONVERSATION STATE and THREAD HISTORY to detect followups.

followup_to: what our previous message was about:
  - "oos_email" / "payment_info" / "tracking_info" / "order_confirmation" / null

dialog_intent (CRITICAL — controls routing for oos_followup):
  - "agrees_to_alternative" — accepts our suggestion OR confirms order in OOS context.
    Includes: "yes", "that works", "ok send me 4 black menthol", "I'll take X".
    KEY: In OOS threads, "send me X" with product+quantity = agrees_to_alternative (NOT provides_info).
  - "declines_alternative" — rejects suggestion ("no thanks", "I'll pass", "cancel")
  - "confirms_payment" — says they paid
  - "asks_question" — asks about products, availability, pricing
  - "provides_info" — gives non-product info (address, phone). NOT for product choices.
  - null — unclear or not a followup

When CONVERSATION STATE mentions out-of-stock/alternatives and customer responds
about products → situation="oos_followup" (NOT "other").
EXCEPTION: If Status is "shipped"/"completed", new product+qty = "new_order".

## Rules for order_items

Extract order_items for: new_order, payment_received, price_question, stock_question, oos_followup.
Set order_items to null for other situations or when no clear product list exists.

### Product name parsing
- product_name: full name as stated (e.g. "Tera Green made in Middle East")
- base_flavor: strip "Tera"/"Terea"/"Heets" prefix and region suffix.
  Keep compound flavors INTACT: "Yellow Menthol", "Black Purple Menthol" (single flavors, do NOT split).
  Non-Tera brands stay intact: "ONE Green" → "ONE Green".
- quantity: number of units (default 1)

### Region preferences
- region_preference: ordered list ["EU","ME","JAPAN"] for SOFT preferences only.
  null when no preference or when region is already in product_name.
  "Turquoise EU" → region_preference=null (region IN name).
  "Turquoise, ME ok if no EU" → region_preference=["EU","ME"], strict_region=false.
  "Turquoise EU only" → region_preference=["EU"], strict_region=true.
- Thread hint: if no region stated but THREAD HISTORY shows a specific region
  variant (e.g. "Terea Yellow ME"), use that as region_preference.
- strict_region: true only for "only"/"exclusively" language, false otherwise.

### Situation-specific rules
- new_order: extract products, quantities, regions from the order.
- payment_received: ALWAYS extract. Use current message first; if none,
  extract from CONVERSATION STATE/THREAD HISTORY (what customer is paying for).
- price_question: extract products being asked about.
- stock_question: each product/region = separate OrderItem (qty=1).
  Region queries → base_flavor = region name ("Japan", "Europe").
  General queries ("What do you have?") → order_items=null.
- oos_followup: extract order_items ONLY when dialog_intent=agrees_to_alternative
  (confirmed items from message + CONVERSATION STATE). For declines/provides_info → null.

## Output format

Return ONLY this JSON (no markdown, no code fences):

{
  "needs_reply": true,
  "situation": "new_order",
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
- client_email: customer email from "From" or "Email" field (Python verifies; best effort)
- client_name: customer full name or null
- price: include $ sign, or null
- customer_street: street address only, or null
- customer_city_state_zip: "City, State Zip", or null
- items: what was ordered as free text, or null
- order_items: structured list (for new_order, payment_received, price_question, stock_question, oos_followup), or null
- followup_to: "oos_email" / "payment_info" / "tracking_info" / "order_confirmation" / null
- dialog_intent: "agrees_to_alternative" / "declines_alternative" / "confirms_payment" / "asks_question" / "provides_info" / null
- order_items[].region_preference: list of "EU"/"ME"/"JAPAN" or null. Only for soft preferences.
- order_items[].strict_region: boolean, default false

CRITICAL: Return a FLAT JSON object with exactly these field names. No extra nesting beyond order_items array.
"""

classifier_agent = Agent(
    id="email-classifier",
    name="Email Classifier",
    model=OpenAIResponses(id="gpt-5-mini"),
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


def _derive_items_text(order_items: list[OrderItem] | None) -> str | None:
    """Derive free-text items summary from structured order_items.

    Format matches email_parser.py try_parse_order output:
    "product_name x quantity, product_name x quantity"
    """
    if not order_items:
        return None
    return ", ".join(f"{oi.product_name} x {oi.quantity}" for oi in order_items)


# Canonical mapping: region suffix (lowered) → region_preference code
_SUFFIX_TO_REGION: dict[str, str] = {
    " made in middle east": "ME",
    " made in armenia": "ME",
    " made in europe": "EU",
    " eu": "EU",
    " japan": "JAPAN",
    " kz": "ME",
}


def _parse_region_from_product_string(s: str) -> tuple[str | None, str]:
    """Parse a product string like "Terea Green EU x5" → (region_code, base_flavor).

    Uses _extract_base_flavor from email_parser for core flavor extraction
    and REGION_SUFFIXES for region detection. Handles "made in" forms.

    Returns (region_code, base_flavor) where region_code is "EU"/"ME"/"JAPAN"/None.
    """
    # Strip " x5" quantity suffix
    s_clean = re.sub(r"\s*x\s*\d+\s*$", "", s.strip())
    if not s_clean:
        return None, ""

    # Detect region from suffix (check "made in X" first, then bare suffixes)
    s_lower = s_clean.lower()
    region_code = None
    # Extended suffixes not in email_parser.REGION_SUFFIXES but common in state strings
    _EXTENDED_SUFFIXES = {
        " made in japan": "JAPAN",
        " me": "ME",
        **_SUFFIX_TO_REGION,
    }
    for suffix, code in sorted(_EXTENDED_SUFFIXES.items(), key=lambda x: -len(x[0])):
        if s_lower.endswith(suffix):
            region_code = code
            break

    # Extract base flavor (strips brand + region)
    base_flavor = _extract_base_flavor(s_clean)

    # Post-fix: strip region remnants that _extract_base_flavor didn't handle
    # (e.g. "Silver ME" when REGION_SUFFIXES has " made in Middle East" but not " ME")
    if region_code:
        bf_lower = base_flavor.lower()
        for suffix in _EXTENDED_SUFFIXES:
            if bf_lower.endswith(suffix.strip()):
                base_flavor = base_flavor[:len(base_flavor) - len(suffix.strip())].strip()
                break
        if bf_lower.endswith(" made in"):
            base_flavor = base_flavor[:-len(" made in")].strip()

    return region_code, base_flavor


def _infer_region_from_state(classification, conversation_state: dict | None) -> None:
    """Fill missing region_preference from conversation_state.facts.

    Source-by-situation matrix (strict):
    - payment_received → facts.ordered_items ONLY
    - oos_followup + agrees_to_alternative → pending_oos_resolution.alternatives first,
      then facts.offered_alternatives ONLY
    - all other situations/intents → skip
    """
    if not classification.order_items or not conversation_state:
        return

    situation = classification.situation
    intent = classification.dialog_intent

    # Gate: strict situation+intent filter
    if situation == "oos_followup" and intent != "agrees_to_alternative":
        return
    if situation not in ("payment_received", "oos_followup"):
        return

    facts = conversation_state.get("facts", {})
    region_map: dict[str, str] = {}  # {"green": "EU", "bright menthol": "EU", ...}

    # Priority 1 (oos_followup only): structured pending_oos_resolution
    if situation == "oos_followup":
        pending = facts.get("pending_oos_resolution", {})
        alts = pending.get("alternatives") if isinstance(pending, dict) else None
        if isinstance(alts, list):
            for alt in alts:
                if isinstance(alt, dict) and alt.get("base_flavor") and alt.get("region_preference"):
                    rp = alt["region_preference"]
                    if isinstance(rp, list) and rp:
                        region_map[alt["base_flavor"].lower()] = rp[0]

    # Priority 2: string parsing — situation-specific source keys
    if not region_map:
        if situation == "payment_received":
            source_keys = ("ordered_items",)
        else:  # oos_followup + agrees
            source_keys = ("offered_alternatives",)

        for key in source_keys:
            val = facts.get(key)
            strings = val if isinstance(val, list) else ([val] if isinstance(val, str) else [])
            for s in strings:
                region_code, base_flavor = _parse_region_from_product_string(s)
                if region_code and base_flavor:
                    region_map[base_flavor.lower()] = region_code

    if not region_map:
        return

    for item in classification.order_items:
        # Guard: don't override existing region_preference
        if item.region_preference is not None:
            continue
        # Guard: don't fill if product_name already has region suffix
        if item.product_name:
            pn_lower = item.product_name.lower()
            if any(pn_lower.endswith(s.lower()) for s in REGION_SUFFIXES):
                continue
        # Guard: don't touch strict_region
        region = region_map.get(item.base_flavor.lower())
        if region:
            item.region_preference = [region]


_SYSTEM_SENDERS = ("noreply@", "no-reply@", "@shipmecarton.com")


def _extract_sender_email(email_text: str) -> str | None:
    """Extract real sender email from email text.

    Priority:
      1. Reply-To header (real client in order notifications)
      2. From header (skip system addresses)
      3. Body "Email:" field — ONLY when From/Reply-To are system senders.
         Searched in unquoted body only (via strip_quoted_text) to avoid
         matching quoted/forwarded "Email:" from old messages.
    """
    header_section = email_text.split("\nBody:", 1)[0] if "\nBody:" in email_text else email_text[:500]

    # Priority 1: Reply-To (real customer in noreply@ order notifications)
    for line in header_section.splitlines():
        if line.lower().startswith("reply-to:"):
            match = _EMAIL_RE.search(line)
            if match:
                return match.group(0).lower()

    # Priority 2: From (skip system addresses)
    from_email = None
    for line in header_section.splitlines():
        if line.lower().startswith("from:"):
            match = _EMAIL_RE.search(line)
            if match:
                email = match.group(0).lower()
                if not any(skip in email for skip in _SYSTEM_SENDERS):
                    return email
                from_email = email  # remember system sender for Priority 3

    # Priority 3: Body "Email:" field — ONLY if From was a system sender
    if from_email:
        body_section = email_text.split("\nBody:", 1)[1] if "\nBody:" in email_text else ""
        unquoted_body = strip_quoted_text(body_section)
        m = re.search(r"(?:^|\n)\s*Email:\s*(.+)", unquoted_body)
        if m:
            match = _EMAIL_RE.search(m.group(1))
            if match:
                return match.group(0).lower()

    return None


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
    pre_state_record = None
    state_dict = None
    thread_history = None
    other_thread_states = None

    if gmail_thread_id:
        try:
            pre_state_record = get_state(gmail_thread_id)
            if pre_state_record and pre_state_record.get("state"):
                state_dict = pre_state_record["state"]
        except Exception as e:
            logger.warning("Failed to get conversation state for classifier: %s", e)

        # Thread history (full messages — Classifier needs complete context)
        try:
            thread_history = get_full_thread_history(
                gmail_thread_id, max_results=15, gmail_account=gmail_account,
            ) or None
        except Exception as e:
            logger.warning("Failed to get thread history for classifier: %s", e)

    # Cross-thread context: other active threads for same client
    sender_email = _extract_sender_email(email_text)
    if not sender_email and pre_state_record:
        sender_email = pre_state_record.get("client_email")

    if sender_email:
        try:
            other_thread_states = get_client_states(sender_email, limit=4) or None
        except Exception as e:
            logger.warning("Failed to get cross-thread context: %s", e)

    conversation_context = compose_classifier_context(
        conversation_state=state_dict,
        thread_history=thread_history,
        other_thread_states=other_thread_states,
        exclude_thread_id=gmail_thread_id,
    )

    return conversation_context, pre_state_record


# ---------------------------------------------------------------------------
# Classification runner
# ---------------------------------------------------------------------------

def run_classification(
    email_text: str,
    context_str: str,
    conversation_state: dict | None = None,
) -> EmailClassification:
    """Run deterministic parser or LLM classifier to produce an EmailClassification.

    Args:
        email_text: Original email text (used by deterministic parser).
        context_str: Classifier context prepended before the new email.
        conversation_state: Structured state dict for Python post-corrections
            (region inference). Optional — backwards compatible.

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
        followup_to=_find_value(data, "followup_to"),
        dialog_intent=_find_value(data, "dialog_intent"),
    )

    # Python client_email is source of truth — always overrides LLM extraction.
    # LLM may return wrong non-system email; Python uses deterministic header/body parsing.
    python_email = _extract_sender_email(email_text)
    if python_email:
        classification.client_email = python_email

    # Python region inference from conversation state (fills null region_preference).
    _infer_region_from_state(classification, conversation_state)

    # Derive items text from order_items when LLM omitted it
    # (used only by notifier.py for Telegram display).
    if not classification.items and classification.order_items:
        classification.items = _derive_items_text(classification.order_items)

    logger.info(
        "Classified: email=%s, situation=%s, needs_reply=%s",
        classification.client_email, classification.situation, classification.needs_reply,
    )
    return classification
