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
from agents.handlers.template_utils import fill_template_reply
from tools.stock_tools import search_stock_tool
from agents.reply_templates import REPLY_TEMPLATES
from db.memory import check_stock_for_order, calculate_order_price
from tools.email_parser import _strip_quoted_text

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
   This is an ORDER CONFIRMATION. You MUST include:
   - List the specific items + quantities they'll receive
   - Calculate total price using STOCK PRICES (provided in prompt)
   - For postpay: "We will ship your package ASAP", "Pay when received via Zelle or Cash App"
   - For prepay: provide Zelle address for payment
   - Include customer name and address if available from CLIENT PROFILE
   - Format like a real order confirmation, not just "Got it!"

2. **declines_alternative** — Customer doesn't want the alternative
   - Acknowledge their choice politely
   - Offer to remove OOS item and proceed with rest of order
   - Or suggest browsing shipmecarton.com for other options
   - Ask what they'd prefer

3. **asks_question** — Customer has questions about alternatives
   - If the question is about whether a product is available or in stock:
     YOU MUST call search_stock_tool(flavor=X) — non-negotiable, even if conversation
     state mentions alternatives (those may be for a different product or outdated)
   - Answer other questions based on what you know from context
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
- For product availability questions: ALWAYS use search_stock_tool — never guess or say "we'll check"
- For other details: use ONLY facts from CONVERSATION STATE and CLIENT PROFILE
- If unsure about non-stock details, say you'll confirm
"""

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
oos_followup_agent = Agent(
    id="oos-followup-handler",
    name="OOS Followup Handler",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=oos_followup_instructions,
    tools=[search_stock_tool],
    markdown=False,
)


# ---------------------------------------------------------------------------
# OOS Agreement Resolution Helpers
# ---------------------------------------------------------------------------

def _match_alternative_from_text(email_text: str, alternatives: list[dict]) -> dict | None:
    """Try to find which alternative the customer mentioned in their email.

    Returns the match only if exactly 1 product_name found in text.
    """
    email_lower = email_text.lower()
    matches = []
    for alt in alternatives:
        name = alt.get("product_name", "")
        if name and name.lower() in email_lower:
            matches.append(alt)
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_oos_agreement(
    result: dict,
    email_text: str,
) -> tuple[list[dict] | None, str]:
    """Try to resolve OOS items to confirmed items for the order.

    Returns:
        (confirmed_items, "ok") — all items resolved
        (None, "clarify") — ambiguous: >1 alternative, email doesn't name one
        (None, "no_data") — no pending_oos_resolution in state
        (None, "no_alternatives") — full OOS with 0 alternatives
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")
    if not pending:
        return None, "no_data"

    confirmed = []

    # In-stock items from original order — keep as-is
    for item in pending.get("in_stock_items", []):
        confirmed.append({
            "base_flavor": item["base_flavor"],
            "product_name": item.get("product_name", item["base_flavor"]),
            "quantity": item["ordered_qty"],
        })

    # Resolve each OOS item
    for item in pending.get("items", []):
        available = item.get("available_qty", 0)
        if available > 0:
            # Partial OOS — reduce qty to what's available
            confirmed.append({
                "base_flavor": item["base_flavor"],
                "product_name": item.get("product_name", item["base_flavor"]),
                "quantity": available,
            })
        else:
            # Full OOS — need alternative
            flavor = item["base_flavor"]
            alt_data = pending.get("alternatives", {}).get(flavor, {})
            alts = alt_data.get("alternatives", [])

            if not alts:
                return None, "no_alternatives"

            if len(alts) == 1:
                # Only one alternative — auto-pick
                confirmed.append({
                    "base_flavor": alts[0]["product_name"],
                    "product_name": alts[0]["product_name"],
                    "quantity": item["requested_qty"],
                })
            else:
                # Multiple alternatives — try to match from email text
                matched = _match_alternative_from_text(email_text, alts)
                if matched:
                    confirmed.append({
                        "base_flavor": matched["product_name"],
                        "product_name": matched["product_name"],
                        "quantity": item["requested_qty"],
                    })
                else:
                    return None, "clarify"

    return confirmed, "ok"


def _build_clarification_reply(pending_oos: dict) -> str:
    """Build a 0-token clarification reply listing alternatives for ambiguous items."""
    lines = [
        "Hi!",
        "Thank you for getting back to us!",
        "We want to make sure we send you exactly what you'd like.",
        "Could you please confirm which option you'd prefer?",
    ]

    for item in pending_oos.get("items", []):
        if item.get("available_qty", 0) == 0:
            flavor = item["base_flavor"]
            alt_data = pending_oos.get("alternatives", {}).get(flavor, {})
            alts = alt_data.get("alternatives", [])
            if len(alts) > 1:
                lines.append(f"\nFor {flavor}:")
                for i, alt in enumerate(alts, 1):
                    lines.append(f"  {i}. {alt['product_name']}")

    lines.append("\nPlease let us know and we'll update your order right away!")
    lines.append("Thank you!")
    return "\n".join(lines)


def _clear_pending_oos(result: dict) -> None:
    """Remove pending_oos_resolution from state facts (persisted by email_agent outbound save)."""
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    facts.pop("pending_oos_resolution", None)


# ---------------------------------------------------------------------------
# Handler Function
# ---------------------------------------------------------------------------
def handle_oos_followup(
    classification,
    result: dict,
    email_text: str,
) -> dict:
    """Handle customer responses to out-of-stock emails.

    Routes by dialog_intent:
    - agrees_to_alternative → template (0 tokens)
    - declines_alternative → template (0 tokens)
    - asks_question / provides_info / unknown → LLM
    """
    intent = classification.dialog_intent

    # Strip quoted/history text to avoid false matches on alternative names
    clean_text = _strip_quoted_text(email_text)

    # === agrees_to_alternative → resolve and send new_order template ===
    if intent == "agrees_to_alternative" and result["client_found"]:
        client = result["client_data"]
        payment_type = client.get("payment_type", "unknown")

        # Guard: prepay without zelle_address → don't send template with blank address
        if payment_type == "prepay" and not client.get("zelle_address"):
            logger.warning(
                "OOS agrees/prepay but no zelle_address for %s — fallback to LLM",
                classification.client_email,
            )
        else:
            # --- Outcome A: Resolve OOS → new_order template with price ---
            confirmed_items, status = _resolve_oos_agreement(result, clean_text)

            if status == "ok" and confirmed_items:
                try:
                    stock_result = check_stock_for_order(confirmed_items)
                    if stock_result["all_in_stock"]:
                        calc_price = calculate_order_price(stock_result["items"])
                        if calc_price is not None:
                            result["calculated_price"] = calc_price
                            result, template_found = fill_template_reply(
                                classification=classification,
                                result=result,
                                situation="new_order",
                            )
                            if template_found:
                                _clear_pending_oos(result)
                                logger.info(
                                    "OOS agrees → new_order template for %s ($%.2f, 0 tokens)",
                                    classification.client_email, calc_price,
                                )
                                return result
                except Exception as e:
                    logger.warning(
                        "OOS agrees resolution failed for %s: %s — fallback",
                        classification.client_email, e,
                    )

            # --- Outcome B: Ambiguous → clarification reply ---
            if status == "clarify":
                state = result.get("conversation_state") or {}
                pending = (state.get("facts") or {}).get("pending_oos_resolution", {})
                result["draft_reply"] = _build_clarification_reply(pending)
                result["template_used"] = True
                result["needs_routing"] = False
                logger.info(
                    "OOS agrees → clarification for %s (0 tokens)",
                    classification.client_email,
                )
                return result

            # --- Outcome C: No pending_oos_resolution → fall through to LLM ---
            # Don't use the generic oos_agrees template (no price, no items).
            # Let the LLM generate a proper reply using conversation state context.
            logger.info(
                "OOS agrees: no pending_oos_resolution for %s — LLM fallback",
                classification.client_email,
            )

    # === declines_alternative → decline template ===
    if intent == "declines_alternative":
        template = REPLY_TEMPLATES.get(("oos_declines", "any"))
        if template:
            result["draft_reply"] = template
            result["template_used"] = True
            result["needs_routing"] = False
            logger.info(
                "OOS declines → template for %s (0 tokens)",
                classification.client_email,
            )
            return result

    # === asks_question / provides_info / unknown / agrees fallback → LLM ===
    ctx = build_context(classification, result, email_text)

    # Inject live stock data if customer is asking about a specific product.
    # Uses classifier-extracted order_items (deterministic, no regex needed).
    stock_context = ""
    order_items = getattr(classification, "order_items", None) or []
    if order_items:
        item = order_items[0]
        asked_flavor = getattr(item, "base_flavor", None) or getattr(item, "product_name", None)
        if asked_flavor:
            stock_info = search_stock_tool(asked_flavor)
            stock_context = f"\n\n=== LIVE STOCK CHECK ===\n{stock_info}"
            logger.info(
                "OOS Followup: injected live stock data for flavor=%s, client=%s",
                asked_flavor, result["client_email"],
            )

    intent_info = ""
    if classification.dialog_intent:
        intent_info = f"\n\nCUSTOMER INTENT: {classification.dialog_intent}"
    if classification.followup_to:
        intent_info += f"\nRESPONDING TO: {classification.followup_to}"

    # Payment type constraint so LLM doesn't mix up prepay/postpay
    payment_type_hint = ""
    if result.get("client_found") and result.get("client_data"):
        pt = result["client_data"].get("payment_type", "unknown")
        if pt in ("prepay", "postpay"):
            other = "postpay" if pt == "prepay" else "prepay"
            payment_type_hint = (
                f"\n\nIMPORTANT: This client is {pt.upper()}. "
                f"Use ONLY {pt} payment and shipping rules. "
                f"IGNORE all {other} rules."
            )

    # For agrees_to_alternative: inject pricing info so LLM can calculate total
    pricing_context = ""
    if intent == "agrees_to_alternative":
        from db.stock import CATEGORY_PRICES
        price_lines = [f"- {cat}: ${p:.0f}/box" for cat, p in sorted(CATEGORY_PRICES.items())]
        pricing_context = (
            "\n\n=== STOCK PRICES ===\n"
            + "\n".join(price_lines)
            + "\n\nUse these prices to calculate the total for the confirmed items."
            + "\nJapanese products (TEREA_JAPAN) and unique terea (УНИКАЛЬНАЯ_ТЕРЕА) have the same price."
        )

    write_instruction = "\n\nWrite a reply:"
    if intent == "agrees_to_alternative":
        write_instruction = (
            "\n\nWrite an ORDER CONFIRMATION reply. Include:"
            "\n- The specific items + quantities the customer confirmed"
            "\n- Calculate total price (qty × price per box)"
            "\n- Shipping and payment info based on their payment type"
            "\n- Customer name and address if available"
        )

    prompt = (
        format_context_for_prompt(ctx)
        + stock_context
        + pricing_context
        + intent_info
        + payment_type_hint
        + write_instruction
    )

    logger.info(
        "OOS Followup LLM: client=%s, intent=%s",
        result["client_email"],
        intent or "unknown",
    )

    response = oos_followup_agent.run(prompt)
    result["draft_reply"] = response.content
    result["template_used"] = False
    result["needs_routing"] = False
    return result