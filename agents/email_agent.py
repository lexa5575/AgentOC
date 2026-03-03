"""
Email Agent
-----------

An agent that classifies incoming emails for shipmecarton.com,
looks up client data, and generates reply drafts.

Architecture:
- Classifier agent: LLM returns structured JSON (Pydantic validated)
- Python preprocessing: lookup client + stock context (0 tokens)
- Router + handlers: each situation resolved by specialized handler

Run with test data:
    python -m agents.email_agent
"""

import json
import logging
import re

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.reply_templates import (
    EmailClassification,
    OrderItem,
    format_result,
    format_thread_for_classifier,
    process_classified_email,
)
from tools.email_parser import try_parse_order, clean_email_body
from agents.checker import CheckResult, check_reply, format_check_result_for_telegram
from agents.context import load_policy
from agents.router import route_to_handler
from agents.state_updater import update_conversation_state
from db import get_postgres_db
from db.conversation_state import get_client_states, get_state, save_state
from db.memory import get_full_thread_history, save_email, save_order_items, update_client
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
agent_db = get_postgres_db()

# ---------------------------------------------------------------------------
# Agent 1: Classifier (returns structured JSON, no free text)
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

Extract order_items for new_order AND price_question situations.
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
# Main Email Agent (orchestrates the workflow, visible in AgentOS UI)
# ---------------------------------------------------------------------------
email_agent_instructions = """\
You are an email processing assistant for shipmecarton.com.

When a user gives you an email to process:
1. Call the `classify_and_process` tool.
2. Copy the tool output to the user EXACTLY as-is. Do not change a single character.

ABSOLUTE RULES:
- Copy the ENTIRE tool output verbatim — every line, every symbol, every space.
- Do NOT rephrase, summarize, reformat, or restructure the output.
- Do NOT add greetings, commentary, or explanations before or after.
- Do NOT change "===" separators to other formatting.
- Do NOT merge lines or split lines differently.
- The tool output IS your response. Nothing more, nothing less.
"""


def _build_oos_telegram(classification, result: dict) -> str:
    """Build Telegram notification text for out-of-stock situations."""
    insufficient = result["stock_issue"]["stock_check"]["insufficient_items"]
    best_alts = result["stock_issue"].get("best_alternatives", {})

    oos_lines = []
    for item in insufficient:
        partial = f" (частично: {item['total_available']} шт)" if item["total_available"] > 0 else ""
        oos_lines.append(
            f"{item['base_flavor']} (заказано {item['ordered_qty']}, на складе {item['total_available']}){partial}"
        )

    alt_lines = []
    for flavor, decision in best_alts.items():
        options = decision.get("alternatives", [])
        if not options:
            alt_lines.append(f"{flavor}: не найдена")
            continue
        rendered = []
        for opt in options[:3]:
            alt = opt["alternative"]
            reason = opt.get("reason", "fallback")
            reason_ru = {
                "same_flavor": "тот же вкус",
                "history": f"из истории ({opt.get('order_count', '?')} заказов)",
                "profile": "из профиля",
                "fallback": "из наличия",
            }.get(reason, reason)
            rendered.append(f"{alt['category']} / {alt['product_name']} [{reason_ru}]")
        alt_lines.append(f"{flavor}: " + "; ".join(rendered))

    return (
        f"\u26a0\ufe0f <b>Нет на складе!</b>\n\n"
        f"<b>Клиент:</b> {classification.client_email}\n"
        f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
        f"<b>Нет в наличии:</b>\n" + "\n".join(oos_lines) + "\n\n"
        f"<b>Альтернатива:</b>\n" + "\n".join(alt_lines) + "\n\n"
        f"Ответ заполнен по шаблону."
    )


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


def classify_and_process(
    email_text: str,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
) -> str:
    """Classify an incoming email and generate a reply draft.
    Handles classification (LLM), context prep (Python), and routed handling.
    Returns formatted result with classification, client data, and draft reply.

    Args:
        email_text: The full email text including From, Subject, Body etc.
        gmail_message_id: Optional Gmail message ID for deduplication.
        gmail_thread_id: Optional Gmail thread ID for thread tracking.

    Returns:
        Formatted classification result with draft reply if template exists.
    """
    try:
        # Step 0.5: Get conversation state + thread history for classifier context
        conversation_context = ""
        pre_state_record = None  # Reused in Step 2.5 to avoid double DB query
        if gmail_thread_id:
            try:
                # Conversation state (structured JSON)
                pre_state_record = get_state(gmail_thread_id)
                state_record = pre_state_record
                if state_record and state_record.get("state"):
                    state = state_record["state"]
                    conversation_context = f"""--- CONVERSATION STATE ---
Status: {state.get('status', 'unknown')}
Topic: {state.get('topic', 'unknown')}
Facts: {json.dumps(state.get('facts', {}), ensure_ascii=False)}
Open questions: {state.get('open_questions', [])}
Summary: {state.get('summary', '')}

"""
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

        # Step 0.9: Try deterministic parsing for website orders (0 tokens)
        parsed_classification = try_parse_order(email_text)
        if parsed_classification:
            logger.info(
                "Order parsed by regex (0 tokens): email=%s, order=%s",
                parsed_classification.client_email,
                parsed_classification.order_id,
            )
            classification = parsed_classification
        else:
            # Clean email body for LLM classifier (remove quoted blocks, signatures)
            cleaned_email = clean_email_body(email_text)

            # Step 1: LLM classifies (returns JSON text)
            logger.info("Classifying email...")
            classifier_input = conversation_context + "--- NEW EMAIL ---\n" + cleaned_email if conversation_context else cleaned_email
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
                # Followup detection fields (Phase 5)
                is_followup=_find_value(data, "is_followup") or False,
                followup_to=_find_value(data, "followup_to"),
                dialog_intent=_find_value(data, "dialog_intent"),
            )

        logger.info(
            "Classified: email=%s, situation=%s, needs_reply=%s",
            classification.client_email, classification.situation, classification.needs_reply,
        )

        # Step 2: Python processes (0 tokens — pure logic)
        result = process_classified_email(classification)

        # Attach gmail_thread_id for downstream context building
        result["gmail_thread_id"] = gmail_thread_id

        # Step 2.5: State Updater LLM — update ConversationState
        if gmail_thread_id:
            try:
                # Reuse state from Step 0.5 (avoid double DB query)
                current_state = pre_state_record.get("state") if pre_state_record else None

                # Update state with new email
                updated_state = update_conversation_state(
                    current_state=current_state,
                    email_text=email_text,
                    situation=classification.situation,
                    direction="inbound",
                    client_email=classification.client_email,
                    order_id=classification.order_id,
                    price=classification.price,
                )

                # Protect pending_oos_resolution from LLM state updater
                if (
                    current_state
                    and current_state.get("facts", {}).get("pending_oos_resolution")
                    and not updated_state.get("facts", {}).get("pending_oos_resolution")
                    and classification.situation == "oos_followup"
                ):
                    updated_state.setdefault("facts", {})["pending_oos_resolution"] = (
                        current_state["facts"]["pending_oos_resolution"]
                    )
                    logger.warning(
                        "Restored pending_oos_resolution stripped by state updater for %s",
                        classification.client_email,
                    )

                # Save updated state
                save_state(
                    gmail_thread_id=gmail_thread_id,
                    client_email=classification.client_email,
                    state_json=updated_state,
                    situation=classification.situation,
                )

                # Add to result for handlers to use
                result["conversation_state"] = updated_state

                logger.info(
                    "Conversation state updated: thread=%s, status=%s",
                    gmail_thread_id, updated_state.get("status"),
                )
            except Exception as e:
                logger.error("Failed to update conversation state: %s", e, exc_info=True)
                result["conversation_state"] = None
        else:
            result["conversation_state"] = None

        # Telegram: notify if new client (not in database)
        if not result["client_found"] and result["needs_reply"]:
            logger.warning("New client not in database: %s", classification.client_email)

            details = []
            if classification.order_id:
                details.append(f"<b>Заказ:</b> #{classification.order_id}")
            if classification.price:
                details.append(f"<b>Сумма:</b> {classification.price}")
            if classification.items:
                details.append(f"<b>Товар:</b> {classification.items}")
            details_text = "\n".join(details)

            send_telegram(
                f"\u26a0\ufe0f <b>Новый клиент написал письмо!</b>\n\n"
                f"<b>От:</b> {classification.client_email}\n"
                f"<b>Имя:</b> {classification.client_name or 'не указано'}\n"
                f"<b>Ситуация:</b> {classification.situation}\n"
                + (f"\n{details_text}\n" if details_text else "") +
                f"\nДобавь клиента в базу через Admin Agent."
            )

        # Telegram: price alerts (mismatch / unmatched)
        price_alert = result.get("price_alert")
        if price_alert:
            alert_type = price_alert["type"]
            if alert_type == "unmatched":
                send_telegram(
                    f"\u26a0\ufe0f <b>Цена не рассчитана!</b>\n\n"
                    f"<b>Клиент:</b> {classification.client_email}\n"
                    f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
                    f"<b>Товары:</b> {', '.join(price_alert['items'])}\n"
                    f"<b>Thread:</b> {gmail_thread_id or 'N/A'}\n\n"
                    f"Товары не сопоставились с каталогом. Шаблон не отправлен, используется LLM."
                )
            elif alert_type == "mismatch":
                send_telegram(
                    f"\u26a0\ufe0f <b>Расхождение цен!</b>\n\n"
                    f"<b>Клиент:</b> {classification.client_email}\n"
                    f"<b>Заказ:</b> #{classification.order_id or 'N/A'}\n"
                    f"<b>Цена сайта:</b> {price_alert['site_price']}\n"
                    f"<b>Цена каталога:</b> {price_alert['calculated_price']}\n"
                    f"<b>Thread:</b> {gmail_thread_id or 'N/A'}\n\n"
                    f"Используется цена сайта."
                )

        tg_msg = None  # Will be set for OOS, sent after AI generates draft

        # Telegram: notify if stock issue detected (enhanced with alternatives + draft)
        if result.get("stock_issue"):
            tg_msg = _build_oos_telegram(classification, result)

        # Step 3: Route to specialized handler
        if result.get("needs_routing") and result["needs_reply"]:
            logger.info(
                "Routing to handler: situation=%s, client=%s",
                classification.situation, classification.client_email,
            )

            # Route to appropriate handler via router
            result = route_to_handler(classification, result, email_text)
            result["needs_routing"] = False
            
            # Step 3.5: Checker — validate the draft (rule-based + LLM)
            checker_obj = None  # Keep full object for Telegram formatting
            if result.get("draft_reply") and not result.get("template_used"):
                try:
                    checker_obj = check_reply(
                        draft=result["draft_reply"],
                        result=result,
                        conversation_state=result.get("conversation_state"),
                        policy_rules=load_policy(classification.situation),
                        run_llm_check=True,
                    )
                    result["check_result"] = {
                        "is_ok": checker_obj.is_ok,
                        "warnings": checker_obj.warnings,
                        "suggestions": checker_obj.suggestions,
                        "rule_violations": checker_obj.rule_violations,
                        "llm_issues": checker_obj.llm_issues,
                    }

                    # Log checker result
                    if not checker_obj.is_ok:
                        logger.warning(
                            "Checker flagged issues: %s",
                            checker_obj.warnings,
                        )
                except Exception as e:
                    logger.error("Checker failed: %s", e, exc_info=True)
                    result["check_result"] = None

            # Send Telegram for OOS situations (with checker warnings if any)
            if tg_msg and result.get("draft_reply"):
                draft_preview = result["draft_reply"][:500]
                checker_msg = ""
                if checker_obj and not checker_obj.is_ok:
                    checker_msg = "\n\n" + format_check_result_for_telegram(checker_obj)
                send_telegram(tg_msg + f"\n--- DRAFT ---\n<pre>{draft_preview}</pre>" + checker_msg)

            # Send Telegram for non-OOS checker issues
            elif checker_obj and not checker_obj.is_ok:
                draft_preview = (result.get("draft_reply") or "")[:500]
                send_telegram(
                    f"\u26a0\ufe0f <b>Checker: проблемы в ответе</b>\n\n"
                    f"<b>Клиент:</b> {classification.client_email}\n"
                    f"<b>Ситуация:</b> {classification.situation}\n\n"
                    + format_check_result_for_telegram(checker_obj)
                    + f"\n\n--- DRAFT ---\n<pre>{draft_preview}</pre>"
                )

        # Step 4: Format the output
        logger.info(
            "Done: email=%s, template=%s, client_found=%s",
            classification.client_email, result["template_used"], result["client_found"],
        )
        formatted = format_result(result)

        # Step 5: Save inbound email + outbound reply to history
        subject = ""
        for line in email_text.split("\n"):
            if line.lower().startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
                break

        save_email(
            client_email=classification.client_email,
            direction="inbound",
            subject=subject,
            body=email_text,
            situation=classification.situation,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
        )
        if result["needs_reply"] and result.get("draft_reply"):
            save_email(
                client_email=classification.client_email,
                direction="outbound",
                subject=f"Re: {subject}" if subject else "",
                body=result["draft_reply"],
                situation=classification.situation,
                gmail_thread_id=gmail_thread_id,
            )

            # Update state with outbound draft (Python, no LLM needed — we know what we wrote)
            if gmail_thread_id and result.get("conversation_state"):
                try:
                    state = result["conversation_state"]
                    state.setdefault("last_exchange", {})["we_said"] = result["draft_reply"][:200]

                    # Save pending_oos_resolution for oos_followup handler
                    if (
                        classification.situation == "new_order"
                        and result.get("stock_issue")
                        and result.get("template_used")
                    ):
                        stock_issue = result["stock_issue"]
                        stock_check = stock_issue["stock_check"]
                        from datetime import datetime, timezone
                        state.setdefault("facts", {})["pending_oos_resolution"] = {
                            "order_id": classification.order_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "items": [
                                {
                                    "base_flavor": i["base_flavor"],
                                    "product_name": i["product_name"],
                                    "requested_qty": i["ordered_qty"],
                                    "available_qty": i["total_available"],
                                }
                                for i in stock_check["insufficient_items"]
                            ],
                            "alternatives": {
                                flavor: {
                                    "alternatives": [
                                        {
                                            "product_name": a["alternative"]["product_name"],
                                            "category": a["alternative"]["category"],
                                        }
                                        for a in alt_data.get("alternatives", [])
                                    ]
                                }
                                for flavor, alt_data in stock_issue.get("best_alternatives", {}).items()
                            },
                            "in_stock_items": [
                                {
                                    "base_flavor": i["base_flavor"],
                                    "product_name": i["product_name"],
                                    "ordered_qty": i["ordered_qty"],
                                }
                                for i in stock_check["items"]
                                if i["is_sufficient"]
                            ],
                        }
                        logger.info(
                            "Saved pending_oos_resolution for %s (thread=%s)",
                            classification.client_email, gmail_thread_id,
                        )

                    save_state(
                        gmail_thread_id=gmail_thread_id,
                        client_email=classification.client_email,
                        state_json=state,
                        situation=classification.situation,
                    )
                except Exception as e:
                    logger.error("Failed to update state for outbound: %s", e)

        # Step 6: Save structured order items for preference tracking
        if (
            classification.situation == "new_order"
            and classification.order_items
            and result["client_found"]
        ):
            save_order_items(
                client_email=classification.client_email,
                order_id=classification.order_id,
                order_items=[
                    {
                        "product_name": oi.product_name,
                        "base_flavor": oi.base_flavor,
                        "quantity": oi.quantity,
                    }
                    for oi in classification.order_items
                ],
            )

        # Step 6.5: Auto-save client address if extracted from email
        if result["client_found"] and (classification.customer_street or classification.customer_city_state_zip):
            address_updates = {}
            if classification.customer_street:
                address_updates["street"] = classification.customer_street
            if classification.customer_city_state_zip:
                address_updates["city_state_zip"] = classification.customer_city_state_zip
            try:
                update_client(classification.client_email, **address_updates)
                logger.info("Auto-saved address for %s: %s", classification.client_email, address_updates)
            except Exception as e:
                logger.warning("Failed to auto-save address: %s", e)

        # Step 7: Auto-refresh client summary if stale
        if result["client_found"]:
            try:
                from agents.client_profiler import maybe_refresh_summary
                maybe_refresh_summary(classification.client_email)
            except Exception as e:
                logger.error("Auto-refresh summary failed: %s", e)

        return formatted

    except Exception as e:
        logger.error("Email processing failed: %s", e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Ошибка обработки email!</b>\n\n"
            f"Ошибка: {e}\n"
            f"Email: {email_text[:200]}...\n\n"
            f"Проверь логи контейнера."
        )
        return f"ERROR: Email processing failed — {e}"


email_agent = Agent(
    id="email-agent",
    name="Email Agent",
    model=OpenAIResponses(id="gpt-5.2"),
    db=agent_db,
    instructions=email_agent_instructions,
    tools=[classify_and_process],
    enable_agentic_memory=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    read_chat_history=True,
    num_history_runs=5,
    markdown=False,
)

# ---------------------------------------------------------------------------
# Test: python -m tests.test_email_agent
# ---------------------------------------------------------------------------
