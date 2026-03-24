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

from agents.formatters import compose_classifier_context, format_client_order_context, format_thread_for_classifier
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

When available, CONVERSATION STATE, THREAD HISTORY, and CLIENT ORDER HISTORY
are prepended before "--- NEW EMAIL ---". Use them to understand context —
especially for detecting followups and customer intent.

## CLIENT ORDER HISTORY

When provided, this section contains:
- Last order: the products and quantities from the customer's most recent order
- Profile: AI-generated summary of customer ordering patterns

USE THIS CONTEXT ONLY WHEN:
- The customer explicitly refers to a previous order ("same order", "as usual", "repeat")
- The customer modifies a previous order ("same order but add 2 blue")

DO NOT use this context to override explicit product requests.
Example: "send me 4 Green" → use what they said, NOT their last order.
Example: "same order please" → use Last order data for order_items.
Example: "same order but add 2 blue" → start from Last order, ADD the modification.
Example: "where is my tracking?" → ignore Last order, classify as tracking.

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
  KEY RULE: product name + quantity + ordering intent = new_order.
  "How much for 5 boxes?" → price_question (asking price, NOT ordering).
  "Send me 5 boxes" → new_order (clear ordering intent).
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
- "payment_received" — confirms payment was sent.
  IMAGE SIGNAL: If email has image attachments AND context suggests payment
  (awaiting_payment status, or we sent Zelle/payment info), treat as payment confirmation.
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
  - "declines_alternative" — rejects suggestion ("no thanks", "I'll pass", "cancel").
    ONLY when customer rejects WITHOUT specifying a new product+quantity.
    IMPORTANT: "decline + new order" = agrees_to_alternative, NOT declines_alternative.
    If customer says "No thanks" BUT ALSO specifies product+quantity
    ("No thanks, just send 10 Sienna", "I'll pass, give me 5 Green instead",
    "I want russet instead of sienna"), the customer is declining the SPECIFIC
    alternatives we offered but placing a CHANGED ORDER.
    Classify as agrees_to_alternative and extract the new order_items.
    Pattern: decline words + product + quantity → agrees_to_alternative
    Pattern: "instead of X" + new product → agrees_to_alternative
    Pattern: decline words WITHOUT new product → declines_alternative
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
- region_preference: ALWAYS fill when region is known. Ordered list ["EU","ME","JAPAN"].
  null ONLY when the customer does NOT specify or imply any region.
  "Turquoise EU" → region_preference=["EU"] (extract region even if it's in the name).
  "Turquoise, ME ok if no EU" → region_preference=["EU","ME"], strict_region=false.
  "Turquoise EU only" → region_preference=["EU"], strict_region=true.
  "3 green please" (no region) → region_preference=null.
- Thread/state hint: if no region stated in the message but CONVERSATION STATE
  or THREAD HISTORY shows a specific region (e.g. "Terea Yellow ME", or
  ordered_items: "Terea Green EU x5"), use that as region_preference.
- strict_region: true only for "only"/"exclusively" language, false otherwise.
- NEVER guess or hallucinate a region — if the customer didn't mention one
  and there is no region in state/history, leave region_preference=null.

### Situation-specific rules
- new_order: extract products, quantities, regions from the order.
- payment_received: ALWAYS extract. Use current message first; if none,
  extract from CONVERSATION STATE/THREAD HISTORY (what customer is paying for).
- price_question: extract products being asked about.
- stock_question: each product/region = separate OrderItem (qty=1).
  Region queries → base_flavor = region name ("Japan", "Europe").
  General queries ("What do you have?") → order_items=null.
- oos_followup:
  agrees_to_alternative → extract confirmed items from message + CONVERSATION STATE.
  This includes ORDER CHANGES: if customer declines offered alternatives but specifies
  different products ("No thanks, just send 10 Sienna", "I want russet instead"),
  extract those as the new order_items.
  For "instead of X" without explicit quantity → use quantity=1 (handler will inherit).
  asks_question → extract the product(s) being asked about (qty=1 if not stated).
  declines_alternative / provides_info → order_items=null.

### Conditional / optional items
- "if you have", "also if available", "and maybe", "if possible",
  "only if in stock" → set optional=true for that item.
- "6 green, and if you have blue I'll take 6 of those too"
  → Green: optional=false, Blue: optional=true
- "I want 6 green and 6 blue" → both optional=false (no conditional language).
- Default is false.

### Fallback / substitution items (new_order ONLY)
- "if not X, Y instead", "if unavailable, Y is fine", "otherwise Y",
  "or Y if you don't have it", "if not, Y" → the REPLACEMENT item gets
  fallback_for set to the 0-based index of the primary item.
- Fallback inherits primary's quantity unless customer explicitly states different qty.
- Example: "3 Tropical please. If not, Black is fine." →
  [{"base_flavor":"Tropical","quantity":3,"fallback_for":null},
   {"base_flavor":"Black","quantity":3,"fallback_for":0}]
- DO NOT set optional=true for fallback items.
  optional = additive ("also add if available").
  fallback_for = replacement ("instead of primary").
- Only use for new_order. Other situations → null.
- One fallback per primary. No chains (fallback_for always points to
  a primary with fallback_for=null).
- Default is null.

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
     "region_preference": null, "strict_region": false, "optional": false, "fallback_for": null}
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
    override_state: dict | None = None,
    override_thread_history: list | None = None,
    override_other_thread_states: list | None = None,
) -> tuple[str, dict | None, dict | None]:
    """Build context string for the classifier from DB state and thread history.

    Override params (Phase D): when set, skip the corresponding DB query
    and use the provided value instead. Sentinel: None = not set (use DB),
    [] = explicitly empty.

    Returns:
        (context_str, pre_state_record, last_order) where context_str is
        prepended to the email text for the classifier, pre_state_record is
        reused in classify_and_process to avoid a duplicate DB query, and
        last_order is passed to run_classification for deterministic reorder.
    """
    pre_state_record = None
    state_dict = None
    thread_history = None
    other_thread_states = None

    if override_state is not None:
        state_dict = override_state
        # pre_state_record not rebuilt — caller already sanitized it
    elif gmail_thread_id:
        try:
            pre_state_record = get_state(gmail_thread_id)
            if pre_state_record and pre_state_record.get("state"):
                state_dict = pre_state_record["state"]
        except Exception as e:
            logger.warning("Failed to get conversation state for classifier: %s", e)

    if override_thread_history is not None:
        thread_history = override_thread_history or None
    elif gmail_thread_id:
        # Thread history (full messages — Classifier needs complete context)
        try:
            thread_history = get_full_thread_history(
                gmail_thread_id, max_results=15, gmail_account=gmail_account,
            ) or None
        except Exception as e:
            logger.warning("Failed to get thread history for classifier: %s", e)

    # Determine sender email (used for cross-thread context AND client order context)
    sender_email = _extract_sender_email(email_text)
    if not sender_email and pre_state_record:
        sender_email = pre_state_record.get("client_email")

    if override_other_thread_states is not None:
        other_thread_states = override_other_thread_states or None
    else:
        # Cross-thread context: other active threads for same client
        if sender_email:
            try:
                other_thread_states = get_client_states(sender_email, limit=4) or None
            except Exception as e:
                logger.warning("Failed to get cross-thread context: %s", e)

    # Client order context (gated: Last order only when reorder hint in body)
    client_order_context = None
    last_order = None
    _sender = sender_email
    if _sender:
        try:
            from db.order_items import get_last_order
            from db.clients import get_client as _get_client
            last_order = get_last_order(_sender)
            _client = _get_client(_sender)
            _llm_summary = _client.get("llm_summary") if _client else None

            # Gate: inject Last order ONLY when body contains reorder hint.
            # Profile (llm_summary) always injected.
            _inject_last_order = _body_has_reorder_hint(email_text)

            client_order_context = format_client_order_context(
                last_order=last_order if _inject_last_order else None,
                llm_summary=_llm_summary,
            )
        except Exception as e:
            logger.warning("Failed to get client order context: %s", e)

    conversation_context = compose_classifier_context(
        conversation_state=state_dict,
        thread_history=thread_history,
        other_thread_states=other_thread_states,
        exclude_thread_id=gmail_thread_id,
        client_order_context=client_order_context,
    )

    return conversation_context, pre_state_record, last_order


# ---------------------------------------------------------------------------
# Deterministic payment-ack detection (Fix 2)
# ---------------------------------------------------------------------------

# Whitelist of normalized ack phrases — only completed-action or pure gratitude.
_ACK_PHRASES = frozenset({
    "thank you", "thanks", "thank u", "thx", "ty",
    "thank you so much", "thanks so much", "many thanks",
    "sent", "done", "paid", "sent it", "just sent", "i sent it",
    "payment sent", "money sent", "i paid", "i sent",
    "sent via zelle", "zelle sent", "here you go",
    # Combined phrases
    "done thanks", "done thank you", "done thx",
    "paid thanks", "paid thank you", "paid thx",
    "sent thanks", "sent thank you",
})

# Generic signature stripping: cut everything after closing marker
_CLOSING_MARKER = re.compile(
    r"\b(regards|best regards|best|cheers|sincerely|warm regards|kind regards)\b.*",
    re.IGNORECASE | re.DOTALL,
)
# iOS/mobile dash-name signature: "- George K.", "— Mike S.", "-- Anna"
# Strict: 1-3 capitalized name tokens only (no common words/verbs).
_DASH_SIGNATURE = re.compile(
    r"\s*[-\u2013\u2014]{1,2}\s*"        # dash(es): hyphen, en-dash, em-dash
    r"([A-Z][a-z]+\.?"                    # first name (capitalized, optional dot)
    r"(?:\s+[A-Z][a-z]*\.?){0,2})"       # 0-2 more name tokens
    r"\s*$",                              # end of string
)
# Greeting/filler words stripped before matching
_STRIP_FILLER = re.compile(
    r"\b(hi|hello|hey|dear|sent from my iphone|sent from my android)\b",
    re.IGNORECASE,
)
# Hard reject: digits, product names, action words, question marks
_REJECT_PATTERN = re.compile(
    r"\d|[?]|"
    r"\b(silver|gold|green|amber|beige|turquoise|bronze|purple|black|"
    r"mauve|blue|yellow|menthol|regular|terea|heets|tera|carton|box|"
    r"please|pls|add|change|cancel|instead|also|another|address|"
    r"tracking|ship|when|order|more)\b",
    re.IGNORECASE,
)


def _looks_like_payment_ack(email_text: str) -> bool:
    """Check if email body is a PURE payment acknowledgment.

    Whitelist approach: normalize body → strip greetings/closing/filler →
    exact match against _ACK_PHRASES. Rejects ANY body with digits, product
    names, question marks, or action words.

    Returns False for attachment-only (empty body) — out of scope v1.
    """
    _body_idx = email_text.find("Body:")
    _body_raw = email_text[_body_idx + 5:].strip() if _body_idx >= 0 else ""
    if not _body_raw:
        return False

    try:
        _body_clean = strip_quoted_text(_body_raw).strip()
    except Exception:
        _body_clean = _body_raw.strip()

    if not _body_clean or len(_body_clean) >= 120:
        return False

    # Strip closing/signature FIRST — names in signature (e.g. "John Green",
    # "Amber Stone") must not trigger product-word reject.
    _pre_sig = _CLOSING_MARKER.sub("", _body_clean)
    _pre_sig = _DASH_SIGNATURE.sub("", _pre_sig)
    _pre_sig = _STRIP_FILLER.sub("", _pre_sig).strip()

    # Hard reject on pre-signature content only
    if _pre_sig and _REJECT_PATTERN.search(_pre_sig):
        return False

    # Normalize: lowercase, strip punctuation, collapse whitespace
    normalized = _pre_sig.lower()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if not normalized:
        # Body was only greetings/signature — check for image attachment
        return "Attachments:" in email_text and "image/" in email_text

    return normalized in _ACK_PHRASES


# ---------------------------------------------------------------------------
# Deterministic reorder detection
# ---------------------------------------------------------------------------

# Exact-match phrases for pure reorder messages (Layer 1, ≤120 chars body).
_REORDER_PHRASES = frozenset({
    "same order", "same order please", "same as last time", "same as before",
    "same again", "repeat order", "repeat my order", "same please",
    "the usual", "the usual order", "as usual", "my usual order",
    "same as usual", "reorder",
    "can i have the same", "can i have the same order",
    "can i please have the same", "can i please have the same order",
    "i want the same", "i want the same order", "id like the same",
    "ill have the same", "send me the same",
    # Russian
    "такой же заказ", "тот же заказ", "повтори заказ", "повторите заказ",
    "как в прошлый раз", "как обычно", "то же самое",
})

# Substring hint regex for modified reorders (Layer 2 gate).
# Narrower than _REORDER_PHRASES: excludes ambiguous bare phrases like
# "same thing", "same one" that could match non-order contexts.
_REORDER_HINT_RE = re.compile(
    r"\b("
    # --- "same order" family (order-intent anchored) ---
    r"same\s+order|"
    r"same\s+please|"
    r"can\s+i(?:\s+please)?\s+have\s+the\s+same|"
    r"i(?:'?d|\s+would)\s+like\s+the\s+same|"
    r"i(?:'?ll)\s+have\s+the\s+same|"
    r"i\s+want\s+the\s+same\s+order|"
    r"send\s+me\s+the\s+same|"
    r"same\s+as\s+(last\s+time|before|usual)|"
    # --- "usual" family ---
    r"as\s+usual|"
    r"the\s+usual(?!\s+(?:tracking|issue|problem|address|way|thing|time|place|stuff|suspects))|"
    r"my\s+usual\s+order|"
    # --- "reorder" family ---
    r"reorder|re-order|"
    r"repeat\s+(order|my\s+order)|"
    # --- Russian ---
    r"как\s+обычно|"
    r"такой\s+же\s+заказ|тот\s+же\s+заказ|"
    r"повтори(те)?\s+заказ|"
    r"то\s+же\s+самое"
    r")",
    re.IGNORECASE,
)


def _body_has_reorder_hint(email_text: str) -> bool:
    """Check if unquoted email body contains reorder-like language (substring match).

    Used as runtime gate for Layer 2 context injection. Returns True when
    the body contains order-intent anchored reorder phrases, even in
    modified form ("same order but add 2 blue").
    """
    _body_idx = email_text.find("Body:")
    _body_raw = email_text[_body_idx + 5:].strip() if _body_idx >= 0 else ""
    if not _body_raw:
        return False
    try:
        _body_clean = strip_quoted_text(_body_raw).strip()
    except Exception:
        _body_clean = _body_raw.strip()
    return bool(_REORDER_HINT_RE.search(_body_clean))


def _looks_like_reorder(email_text: str) -> bool:
    """Check if email body is a PURE reorder request (no modifications).

    Whitelist approach: normalize body → strip greetings/closing/filler →
    exact match against _REORDER_PHRASES. Body must be ≤120 chars after strip.

    Returns False for bodies with modifications ("same order but add blue")
    or explicit product mentions — those go through LLM with Layer 2 context.
    """
    _body_idx = email_text.find("Body:")
    _body_raw = email_text[_body_idx + 5:].strip() if _body_idx >= 0 else ""
    if not _body_raw:
        return False

    try:
        _body_clean = strip_quoted_text(_body_raw).strip()
    except Exception:
        _body_clean = _body_raw.strip()

    if not _body_clean or len(_body_clean) >= 120:
        return False

    # Strip closing/signature and filler (reuse from payment_ack)
    _pre_sig = _CLOSING_MARKER.sub("", _body_clean)
    _pre_sig = _STRIP_FILLER.sub("", _pre_sig).strip()

    # Normalize: lowercase, strip punctuation, collapse whitespace
    normalized = _pre_sig.lower()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if not normalized:
        return False

    return normalized in _REORDER_PHRASES


def _build_order_items_from_last_order(last_order: dict) -> list[OrderItem]:
    """Build OrderItems from last order, resolving region via variant_id → catalog.

    Primary: variant_id → ProductCatalog → category → CATEGORY_REGION_SUFFIX.
    Fallback: parse from display_name_snapshot or product_name.
    """
    from db.catalog import get_catalog_products
    from db.region_family import CATEGORY_REGION_SUFFIX

    _REGION_NORMALIZE = {"Japan": "JAPAN"}
    catalog = get_catalog_products()
    vid_to_region: dict[int, str] = {}
    for entry in catalog:
        suffix = CATEGORY_REGION_SUFFIX.get(entry["category"])
        if suffix:
            vid_to_region[entry["id"]] = _REGION_NORMALIZE.get(suffix, suffix)

    items = []
    for item in last_order["items"]:
        region = None
        vid = item.get("variant_id")
        if vid and vid in vid_to_region:
            region = vid_to_region[vid]
        else:
            source = item.get("display_name_snapshot") or item["product_name"]
            region, _ = _parse_region_from_product_string(source)
        items.append(OrderItem(
            product_name=item["product_name"],
            base_flavor=item["base_flavor"],
            quantity=item["quantity"],
            region_preference=[region] if region else None,
        ))
    return items


# ---------------------------------------------------------------------------
# Classification runner
# ---------------------------------------------------------------------------

def run_classification(
    email_text: str,
    context_str: str,
    conversation_state: dict | None = None,
    last_order: dict | None = None,
) -> EmailClassification:
    """Run deterministic parser or LLM classifier to produce an EmailClassification.

    Args:
        email_text: Original email text (used by deterministic parser).
        context_str: Classifier context prepended before the new email.
        conversation_state: Structured state dict for Python post-corrections
            (region inference). Optional — backwards compatible.
        last_order: Last order dict from get_last_order(), used for
            deterministic reorder detection. Optional — backwards compatible.

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

    # Step 0.95: Deterministic payment-ack when awaiting_payment (0 tokens)
    if conversation_state and conversation_state.get("status") == "awaiting_payment":
        _facts = conversation_state.get("facts") or {}
        if (
            _facts.get("payment_request_sent")
            and not _facts.get("payment_confirmed")
            and _looks_like_payment_ack(email_text)
        ):
            _sender = _extract_sender_email(email_text) or ""
            _order_id = _facts.get("order_id")
            logger.info(
                "Deterministic: awaiting_payment + ack -> payment_received (%s, order=%s)",
                _sender, _order_id,
            )
            return EmailClassification(
                needs_reply=True,
                situation="payment_received",
                client_email=_sender,
                order_id=_order_id,
                dialog_intent="confirms_payment",
                followup_to="payment_info",
            )

    # Step 0.96: Deterministic reorder detection (0 tokens)
    if _looks_like_reorder(email_text):
        _sender = _extract_sender_email(email_text) or ""
        if _sender and last_order and last_order.get("items"):
            _order_items = _build_order_items_from_last_order(last_order)
            logger.info(
                "Deterministic: reorder detected -> new_order (%s, last_order=%s)",
                _sender, last_order["order_id"],
            )
            return EmailClassification(
                needs_reply=True,
                situation="new_order",
                client_email=_sender,
                order_items=_order_items,
                items=_derive_items_text(_order_items),
                # parser_used=False (default) — pipeline uses calculated_price
            )
        # No sender or no order history → fall through to LLM

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
                    optional=item.get("optional", False),
                    fallback_for=item.get("fallback_for"),
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

    # Strip fallback_for outside new_order — prompt says "ONLY new_order"
    # but LLM may ignore; enforce in code to protect payment_received path.
    if classification.situation != "new_order" and classification.order_items:
        for oi in classification.order_items:
            if oi.fallback_for is not None:
                oi.fallback_for = None

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
