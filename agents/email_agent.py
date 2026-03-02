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
    process_classified_email,
)
from agents.router import route_to_handler
from agents.state_updater import update_conversation_state
from db import get_postgres_db
from db.conversation_state import get_state, save_state
from db.memory import save_email, save_order_items
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

## Rules for identifying the real sender

If the email is from @shipmecarton.com, noreply@, or no-reply@ — this is an ORDER NOTIFICATION.
The REAL customer is NOT the sender. Extract the real customer from:
- "Email:" field in the body
- "Reply-To:" header (fallback)
- "Firstname:" field for the name

For all other emails, the From address IS the real customer.

## Rules for needs_reply

true: orders, customer questions, complaints, payment confirmations
false: marketing, spam, simple "Thank you!" / "Got it" / "Perfect"

## Rules for situation (use exactly one value)

- "new_order" — new order or order notification from system
- "tracking" — asks about delivery status or tracking number
- "payment_question" — asks WHERE or HOW to pay
- "payment_received" — confirms payment was sent
- "discount_request" — asks for discount or better price
- "shipping_timeline" — asks WHEN order will be shipped
- "other" — anything else

## Rules for order_items (ONLY when situation is "new_order")

When the email contains a product table or product list, extract each item:
- product_name: full name as shown (e.g. "Tera Green made in Middle East")
- base_flavor: ONLY the flavor/color word. Strip brand prefixes ("Tera", "Terea", "Heets")
  and region suffixes ("made in Middle East", "EU", "Japan", "KZ").
  Examples: "Tera Green made in Middle East" → "Green", "Tera Turquoise EU" → "Turquoise",
  "Tera Silver" → "Silver", "ONE Green" → "ONE Green", "PRIME Black" → "PRIME Black"
- quantity: number of units from "Qnt" column or "x 2" notation. Default 1.

If no product details found in the email, set order_items to null.

## Output format

Return ONLY this exact JSON structure (no markdown, no code fences, no explanation):

{
  "needs_reply": true,
  "situation": "new_order",
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
- price: include $ sign, e.g. "$220.00", or null
- customer_street: street address only, or null
- customer_city_state_zip: "City, State Zip" on one line, or null
- items: what was ordered as free text, or null
- order_items: structured list of items (only for new_order), or null

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
        # Step 1: LLM classifies (returns JSON text)
        logger.info("Classifying email...")
        response = classifier_agent.run(email_text)
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
        )

        logger.info(
            "Classified: email=%s, situation=%s, needs_reply=%s",
            classification.client_email, classification.situation, classification.needs_reply,
        )

        # Step 2: Python processes (0 tokens — pure logic)
        result = process_classified_email(classification)

        # Step 2.5: State Updater LLM — update ConversationState
        if gmail_thread_id:
            try:
                # Get current state
                current_state_record = get_state(gmail_thread_id)
                current_state = current_state_record.get("state") if current_state_record else None

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
            
            # Send Telegram for OOS situations
            if tg_msg and result.get("draft_reply"):
                draft_preview = result["draft_reply"][:500]
                send_telegram(tg_msg + f"\n--- DRAFT ---\n<pre>{draft_preview}</pre>")

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
