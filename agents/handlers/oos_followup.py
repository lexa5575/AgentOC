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
from db.memory import (
    check_stock_for_order,
    calculate_order_price,
    resolve_order_items,
)
from tools.email_parser import _strip_quoted_text

logger = logging.getLogger(__name__)

from agents.handlers.oos_constants import TRUSTED_SOURCES

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
   - IMPORTANT: If the customer asks about specific products in an OOS context
     (e.g. "Do you have Amber ME and Balanced?"), this is likely their CHOICE of
     replacement. If all requested products are in stock, treat this as an order
     confirmation: confirm the items, calculate total, and provide payment/shipping
     info based on their payment type. Do NOT just say "yes we have them" and wait.
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

REGION IN PRODUCT NAMES — MANDATORY:
- ALWAYS include the region suffix (ME, EU, or Japan) when mentioning a product. \
  Say "Terea Silver ME", NOT just "Terea Silver". \
  Say "Terea Yellow EU", NOT just "Terea Yellow". \
  This is critical — without region, the system can't process the order correctly.

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
    model=OpenAIResponses(id="gpt-5-mini"),
    instructions=oos_followup_instructions,
    tools=[search_stock_tool],
    markdown=False,
)


# ---------------------------------------------------------------------------
# OOS Agreement Resolution Helpers
# ---------------------------------------------------------------------------

from agents.handlers.oos_agreement import (
    _match_alternative_from_text,
    _resolve_oos_agreement,
    _build_clarification_reply,
    _resolve_from_classifier,
    _build_order_summary,
    _clear_pending_oos,
    _normalize_order_id,
)


# ---------------------------------------------------------------------------
# v3 helpers: confirmation flags, thread extraction
# ---------------------------------------------------------------------------


def _apply_confirmation_flags(
    result: dict,
    stock_result: dict,
    resolved_items: list[dict],
    source: str,
    order_id_norm: str | None,
) -> None:
    """Set confirmation source and eligibility flags on result dict (plan §6).

    Sets effective_situation="new_order" ONLY when source is trusted AND order_id exists
    AND no ambiguous variants detected.
    """
    from db.stock import has_ambiguous_variants

    result["confirmation_source"] = source
    result["canonical_confirmed_items"] = stock_result["items"]
    result["_stock_check_items"] = resolved_items

    # Phase 3 ambiguity gate: block fulfillment if any item has
    # multiple product_ids (plan §9.5, rule §4.3).
    ambiguous = has_ambiguous_variants(
        resolved_items, client_email=result.get("client_email"),
    )
    if ambiguous:
        result["fulfillment_blocked"] = True
        result["ambiguous_flavors"] = ambiguous
        logger.warning(
            "OOS agrees (%s): ambiguous variants %s — fulfillment blocked",
            result.get("client_email", "?"), ambiguous,
        )
        # Do NOT set effective_situation — blocks persistence + fulfillment
        return

    if source in TRUSTED_SOURCES and order_id_norm:
        result["effective_situation"] = "new_order"
    else:
        if not order_id_norm:
            from utils.telegram import send_telegram

            client_email = result.get("client_email", "?")
            logger.warning(
                "OOS agrees (%s): order_id=None → persistence/fulfillment skipped",
                client_email,
            )
            summary = result.get("order_summary", "")
            price = result.get("calculated_price")
            price_str = f"${price:.2f}" if price else "N/A"
            send_telegram(
                f"\u26a0\ufe0f <b>Fulfillment скипнут!</b>\n\n"
                f"<b>Клиент:</b> {client_email}\n"
                f"<b>Причина:</b> order_id=None (классификатор не нашёл номер заказа)\n"
                f"<b>Источник:</b> {source}\n"
                f"<b>Сумма:</b> {price_str}\n"
                f"<b>Товары:</b> {summary or 'N/A'}\n\n"
                f"Черновик создан, но заказ НЕ записан в БД и склад НЕ обновлён.\n"
                f"Обработай вручную или добавь order_id."
            )


from agents.handlers.oos_text_utils import (
    _detect_region_and_core,
    _normalize_extracted_region,
    _STANDALONE_QTY,
    _extract_client_qty_for_flavor,
    _extract_standalone_qty,
    _extract_base_flavor_from_label,
    _extract_region_suffix_from_label,
    _extract_qty_from_label,
)


from agents.handlers.oos_qty_utils import (
    _build_pending_qty_map,
    _merge_in_stock_items,
    _enrich_qty_from_pending,
)


from agents.handlers.oos_thread_extraction import _extract_agreed_items_from_thread


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
        order_id_norm = _normalize_order_id(classification)

        # Guard: prepay without zelle_address → don't send template with blank address
        if payment_type == "prepay" and not client.get("zelle_address"):
            logger.warning(
                "OOS agrees/prepay but no zelle_address for %s — fallback to LLM",
                classification.client_email,
            )
        else:
            gmail_thread_id = result.get("gmail_thread_id")
            gmail_account = result.get("gmail_account", "default")

            # --- PRIMARY: Thread extraction (plan §5 / §7.2A) ---
            if gmail_thread_id:
                extracted = _extract_agreed_items_from_thread(
                    gmail_thread_id, clean_text, gmail_account,
                    result=result,
                )
                if extracted:
                    try:
                        extracted = _enrich_qty_from_pending(extracted, result, clean_text)
                        extracted = _merge_in_stock_items(extracted, result)
                        resolved, _ = resolve_order_items(extracted)
                        stock_result = check_stock_for_order(resolved)
                        if stock_result["all_in_stock"]:
                            calc_price = calculate_order_price(stock_result["items"])
                            if calc_price is not None:
                                result["calculated_price"] = calc_price
                                result["order_summary"] = _build_order_summary(stock_result["items"])
                                result, template_found = fill_template_reply(
                                    classification=classification,
                                    result=result,
                                    situation="new_order",
                                )
                                if template_found:
                                    _apply_confirmation_flags(
                                        result, stock_result, resolved,
                                        "thread_extraction", order_id_norm,
                                    )
                                    _clear_pending_oos(result)
                                    logger.info(
                                        "OOS agrees → extraction template for %s ($%.2f)",
                                        classification.client_email, calc_price,
                                    )
                                    return result
                    except Exception as e:
                        logger.warning(
                            "OOS extraction resolution failed for %s: %s",
                            classification.client_email, e,
                        )

            # --- FALLBACK A: Pending OOS with mandatory resolve (plan §7.2D) ---
            confirmed_items, status = _resolve_oos_agreement(result, clean_text)

            if status == "ok" and confirmed_items:
                try:
                    resolved, _ = resolve_order_items(confirmed_items)
                    stock_result = check_stock_for_order(resolved)
                    if stock_result["all_in_stock"]:
                        calc_price = calculate_order_price(stock_result["items"])
                        if calc_price is not None:
                            result["calculated_price"] = calc_price
                            result["order_summary"] = _build_order_summary(stock_result["items"])
                            result, template_found = fill_template_reply(
                                classification=classification,
                                result=result,
                                situation="new_order",
                            )
                            if template_found:
                                _apply_confirmation_flags(
                                    result, stock_result, resolved,
                                    "pending_oos", order_id_norm,
                                )
                                _clear_pending_oos(result)
                                logger.info(
                                    "OOS agrees → pending template for %s ($%.2f)",
                                    classification.client_email, calc_price,
                                )
                                return result
                except Exception as e:
                    logger.warning(
                        "OOS pending resolution failed for %s: %s",
                        classification.client_email, e,
                    )

            # --- FALLBACK B: Ambiguous → clarification reply ---
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

            # --- FALLBACK C: Classifier (NOT trusted for persistence/fulfillment) ---
            confirmed_from_classifier = _resolve_from_classifier(classification)
            if confirmed_from_classifier:
                confirmed_from_classifier = _merge_in_stock_items(
                    confirmed_from_classifier, result,
                )
                try:
                    resolved, _ = resolve_order_items(confirmed_from_classifier)
                    stock_result = check_stock_for_order(resolved)
                    if stock_result["all_in_stock"]:
                        calc_price = calculate_order_price(stock_result["items"])
                        if calc_price is not None:
                            result["calculated_price"] = calc_price
                            result["order_summary"] = _build_order_summary(stock_result["items"])
                            result, template_found = fill_template_reply(
                                classification=classification,
                                result=result,
                                situation="new_order",
                            )
                            if template_found:
                                _apply_confirmation_flags(
                                    result, stock_result, resolved,
                                    "classifier", order_id_norm,
                                )
                                _clear_pending_oos(result)
                                logger.info(
                                    "OOS agrees → classifier template for %s ($%.2f)",
                                    classification.client_email, calc_price,
                                )
                                return result
                except Exception as e:
                    logger.warning(
                        "OOS classifier resolution failed for %s: %s",
                        classification.client_email, e,
                    )

            # --- FALLBACK D: LLM ---
            logger.info(
                "OOS agrees: no template path succeeded for %s — LLM fallback",
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
    # Fallback: if no order_items, inject stock for all offered_alternatives from
    # conversation state so LLM never hallucinates about availability.
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
    else:
        # No order_items extracted — inject stock for previously offered alternatives
        # so the LLM has accurate availability instead of guessing.
        _state = result.get("conversation_state") or {}
        _alts = (_state.get("facts") or {}).get("offered_alternatives") or []
        if _alts:
            stock_lines = []
            for _alt in _alts:
                _base = _alt.replace(" ME", "").replace(" EU", "").strip()
                stock_lines.append(search_stock_tool(_base))
            stock_context = "\n\n=== LIVE STOCK CHECK (offered alternatives) ===\n" + "\n---\n".join(stock_lines)
            logger.info(
                "OOS Followup: injected stock for %d alternatives, client=%s",
                len(_alts), result["client_email"],
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