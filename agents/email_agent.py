"""
Email Agent
-----------

An agent that classifies incoming emails for shipmecarton.com,
looks up client data, and generates reply drafts.

Architecture:
- Classifier agent: LLM returns structured JSON (Pydantic validated)
- Python processing: lookup client + fill template (0 tokens)
- Fallback agent: LLM generates reply only when no template exists

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
    OUT_OF_STOCK_GUIDE,
    format_email_history,
    format_result,
    process_classified_email,
)
from db import get_postgres_db
from db.memory import get_full_email_history, save_email, save_order_items
from tools.web_search import get_search_tools
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
# Agent 2: Fallback Reply Writer (only used when no template exists)
# ---------------------------------------------------------------------------
fallback_instructions = """\
You are James, a customer service assistant for shipmecarton.com.

Write a short reply to the customer email. You will receive context about the situation, client, and conversation history.

STYLE — MATCH HISTORY:
- Study the [WE SENT] messages in conversation history — that is YOUR voice
- Copy the exact wording, phrasing, and structure from those messages
- If history shows we use specific phrases (e.g., payment instructions, greeting patterns), reuse them verbatim
- If no history is available: start with "Hi {name}," / "Hello,", 2-5 sentences, casual tone
- Always end with exactly "Thank you!" — nothing after it, no name, no signature
  EXCEPTION: For OUT OF STOCK replies, end with "Please let us know what you think" (as per template)

WHAT YOU CAN DO:
- Reference information provided in the context and conversation history
- Use conversation history to maintain continuity (e.g., reference previous orders, ongoing discussions)
- Say "we'll check and get back to you" for things you can't verify

WEB SEARCH:
- You have a web search tool — use it when the customer asks about a product,
  device, or topic you don't have information about
- Search in English
- Use search results to give a helpful, informed answer
- Always cite what you found naturally (e.g., "Based on what we found...")
- Do NOT paste raw search results — summarize in 1-2 sentences
- If search doesn't help, fall back to "we'll check and get back to you"

WHAT YOU CANNOT DO:
- Invent prices, tracking numbers, delivery dates, or stock levels
- Offer discounts or change payment terms
- Tell customer to check the website — WE always check for them
  EXCEPTION: For OUT OF STOCK situations, you MUST include the website link
  https://shipmecarton.com as option 2 (this is part of the out-of-stock template)
- Reveal you are AI
- Write multiple reply variants — only ONE reply

OUT-OF-STOCK REPLIES:
- Follow the TEMPLATE GUIDE structure CLOSELY
- Use only products from SELECTED ALTERNATIVES — do NOT choose different products
- If the reason is "history", mention naturally: "We have Green which you've enjoyed before"
- If the reason is "same_flavor", mention the region: "We have Turquoise from Armenia"
- If the reason is "fallback", just suggest it normally without extra explanation
- If no alternative available, skip option 1 and only keep option 2 (website)
- For PARTIAL SHORTAGE: mention "we have X available" + suggest replacement for remainder
- For option 1, you may include up to 3 alternatives when provided
- ONLY mention products from SELECTED ALTERNATIVES — NEVER invent or suggest other items
- Conversation history is for STYLE/TONE only — NOT for stock information
- Always keep option 2 (website link) exactly as in template

READING HISTORY — PRIORITIZE:
- [WE SENT] messages are your style reference — replicate their tone and wording
- Most recent messages carry more weight than older ones
- Do NOT use history to determine what is in stock — only use the data provided above
"""

fallback_agent = Agent(
    id="email-fallback",
    name="Email Fallback",
    model=OpenAIResponses(id="gpt-5.2"),
    instructions=fallback_instructions,
    tools=[get_search_tools()],
    markdown=False,
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
        f"AI генерирует ответ."
    )


def _build_oos_fallback_prompt(
    result: dict, client_info: str, history_text: str, email_text: str,
) -> str:
    """Build the LLM fallback prompt for out-of-stock situations."""
    stock_data = result["stock_issue"]
    stock_check = stock_data["stock_check"]
    best_alternatives = stock_data.get("best_alternatives", {})

    insufficient_lines = []
    for item in stock_check["insufficient_items"]:
        partial = ""
        if item["total_available"] > 0:
            partial = f" — PARTIAL SHORTAGE (have {item['total_available']})"
        insufficient_lines.append(
            f"- {item['product_name']} (flavor: {item['base_flavor']}): "
            f"ordered {item['ordered_qty']}, available {item['total_available']}{partial}"
        )
    insufficient_text = "\n".join(insufficient_lines)

    alt_lines = []
    for flavor, decision in best_alternatives.items():
        options = decision.get("alternatives", [])
        if options:
            alt_lines.append(f"For {flavor}:")
            for i, opt in enumerate(options[:3], 1):
                alt = opt["alternative"]
                reason = opt.get("reason", "fallback")
                reason_detail = reason
                if reason == "history" and opt.get("order_count"):
                    reason_detail = (
                        f"customer ordered flavor like {alt['product_name']} "
                        f"{opt['order_count']} times before"
                    )
                elif reason == "same_flavor":
                    reason_detail = f"same flavor from {alt['category']}"
                elif reason == "fallback":
                    reason_detail = "available in stock"
                alt_lines.append(
                    f"  {i}. {alt['category']} / {alt['product_name']} (qty: {alt['quantity']}) "
                    f"— reason: {reason_detail}"
                )
        else:
            alt_lines.append(f"For {flavor}: - No alternative available")
    alternatives_text = "\n".join(alt_lines)

    prompt = (
        f"Situation: OUT OF STOCK (new order with insufficient stock)\n"
        f"Client: {client_info}\n"
        f"Client name: {result['client_name'] or 'unknown'}\n\n"
        f"INSUFFICIENT ITEMS:\n{insufficient_text}\n\n"
        f"SELECTED ALTERNATIVES FOR OPTION 1 (up to 3 per missing flavor):\n"
        f"{alternatives_text}\n\n"
    )
    if history_text:
        prompt += f"{history_text}\n\n"
    prompt += (
        f"Original email:\n{email_text}\n\n"
        f"TEMPLATE GUIDE (follow this structure closely):\n"
        f"{OUT_OF_STOCK_GUIDE}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Follow the template guide structure above\n"
        f"- Replace {{FLAVOR_LIST}} with the actual out-of-stock flavor names\n"
        f"- For option 1, use only products from SELECTED ALTERNATIVES\n"
        f"- You may include up to 3 alternatives in option 1 if provided\n"
        f"- If reason is 'history', mention naturally they ordered it before\n"
        f"- If partial shortage, mention 'we have X available'\n"
        f"- Keep the website link https://shipmecarton.com and option 2 exactly as shown\n"
        f"- End with 'Please let us know what you think'\n"
        f"Write the reply:"
    )
    return prompt


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


def classify_and_process(email_text: str, gmail_message_id: str | None = None) -> str:
    """Classify an incoming email and generate a reply draft.
    Handles classification (LLM), client lookup, and template filling (Python).
    Returns formatted result with classification, client data, and draft reply.

    Args:
        email_text: The full email text including From, Subject, Body etc.
        gmail_message_id: Optional Gmail message ID for deduplication.

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

        # Step 3: If no template, ask fallback agent (with conversation history)
        if result["needs_ai_fallback"] and result["needs_reply"]:
            client_info = ""
            if result["client_found"]:
                c = result["client_data"]
                client_info = (
                    f"Known client: {c.get('name', 'unknown')}, "
                    f"payment type: {c['payment_type']}"
                )
            else:
                client_info = "NEW CLIENT — not in our database"

            # Fetch conversation history: local DB + Gmail (merged)
            history = get_full_email_history(result["client_email"], max_results=10)
            history_text = format_email_history(history)
            logger.info(
                "AI fallback for situation=%s, history=%d messages",
                result["situation"], len(history),
            )

            # Build fallback prompt: enriched for out-of-stock, generic otherwise
            if result.get("stock_issue"):
                fallback_prompt = _build_oos_fallback_prompt(
                    result, client_info, history_text, email_text,
                )
            else:
                fallback_prompt = (
                    f"Situation: {result['situation']}\n"
                    f"Client: {client_info}\n"
                    f"Client name: {result['client_name'] or 'unknown'}\n\n"
                )
                if history_text:
                    fallback_prompt += f"{history_text}\n\n"
                fallback_prompt += (
                    f"Original email:\n{email_text}\n\n"
                    f"Write a reply:"
                )

            fallback_response = fallback_agent.run(fallback_prompt)
            result["draft_reply"] = fallback_response.content
            result["needs_ai_fallback"] = False

            # Send Telegram with draft for OOS (deferred from above)
            if result.get("stock_issue") and tg_msg:
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
        )
        if result["needs_reply"] and result.get("draft_reply"):
            save_email(
                client_email=classification.client_email,
                direction="outbound",
                subject=f"Re: {subject}" if subject else "",
                body=result["draft_reply"],
                situation=classification.situation,
            )

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
