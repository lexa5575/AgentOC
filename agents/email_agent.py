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
from datetime import timezone

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.reply_templates import (
    EmailClassification,
    format_email_history,
    format_result,
    process_classified_email,
)
from db import get_postgres_db
from db.memory import get_email_history, get_gmail_thread_history, save_email
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
  "items": null
}

Field rules:
- client_email: ALWAYS the real customer email (never noreply@, never system email)
- client_name: customer full name or null
- price: include $ sign, e.g. "$220.00", or null
- customer_street: street address only, or null
- customer_city_state_zip: "City, State Zip" on one line, or null
- items: what was ordered, or null

CRITICAL: Return a FLAT JSON object with exactly these field names. No nesting. No extra fields.
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

STYLE:
- Start with "Hi {name}," if name is known, otherwise "Hello,"
- 2-5 sentences maximum
- End with exactly "Thank you!" — nothing after it, no name, no signature
- Casual, friendly tone

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
- Reveal you are AI
- Write multiple reply variants — only ONE reply

READING HISTORY — PRIORITIZE:
- Messages where WE discussed stock availability, offered alternatives, or quoted prices — these are most important
- Customer's ordering patterns: what they usually buy, what prices they paid
- Most recent messages carry more weight than older ones
- SKIP over routine "I paid" confirmations and tracking number messages — they are not useful
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
        classification = EmailClassification(
            needs_reply=_find_value(data, "needs_reply") if _find_value(data, "needs_reply") is not None else True,
            situation=_find_value(data, "situation", "classification", "category") or "other",
            client_email=_find_value(data, "client_email", "real_customer_email", "customer_email", "email") or "",
            client_name=_find_value(data, "client_name", "customer_name", "name", "firstname"),
            order_id=_find_value(data, "order_id", "order_number"),
            price=_find_value(data, "price", "payment_amount", "total", "amount"),
            customer_street=_find_value(data, "customer_street", "street", "street_address", "address"),
            customer_city_state_zip=_find_value(data, "customer_city_state_zip", "city_state_zip"),
            items=_find_value(data, "items", "products", "order_items"),
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
            send_telegram(
                f"\u26a0\ufe0f <b>Новый клиент написал письмо!</b>\n\n"
                f"От: {classification.client_email}\n"
                f"Имя: {classification.client_name or 'не указано'}\n"
                f"Ситуация: {classification.situation}\n\n"
                f"Проверь и добавь в базу через Admin Agent."
            )

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

            # Fetch conversation history: local DB first, then Gmail if sparse
            history = get_email_history(result["client_email"])
            if len(history) < 3:
                gmail_history = get_gmail_thread_history(result["client_email"], max_results=10)
                if gmail_history:
                    # Merge: add Gmail messages not already in local DB
                    local_subjects = {(h["subject"], h["direction"]) for h in history}
                    for gh in gmail_history:
                        if (gh["subject"], gh["direction"]) not in local_subjects:
                            history.append(gh)
                    # Sort chronologically (normalize tz for comparison) and limit
                    def _sort_key(h):
                        dt = h["created_at"]
                        if dt.tzinfo is not None:
                            return dt.timestamp()
                        return dt.replace(tzinfo=timezone.utc).timestamp()
                    history.sort(key=_sort_key)
                    history = history[-10:]

            history_text = format_email_history(history)
            logger.info(
                "AI fallback for situation=%s, history=%d messages",
                result["situation"], len(history),
            )

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
# Test with sample data (like "pin data" in n8n)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        (
            "PREPAY client (template)",
            "Process this email:\n\n"
            "From: noreply@shipmecarton.com\n"
            "Reply-To: client1@example.com\n"
            "Subject: Shipmecarton - Order 23432\n\n"
            "Payment amount: $220.00\n"
            "Order ID: 23432\n"
            "Firstname: Test Client One\n"
            "Street address1: 123 Main St\n"
            "Town/City: Springfield\n"
            "State: Illinois\n"
            "Postcode/Zip: 62701\n"
            "Email: client1@example.com",
        ),
        (
            "DISCOUNT client 5% (template)",
            "Process this email:\n\n"
            "From: noreply@shipmecarton.com\n"
            "Reply-To: client3@example.com\n"
            "Subject: Shipmecarton - Order 23600\n\n"
            "Payment amount: $200.00\n"
            "Order ID: 23600\n"
            "Firstname: Test Client Three\n"
            "Email: client3@example.com",
        ),
        (
            "POSTPAY client (template)",
            "Process this email:\n\n"
            "From: noreply@shipmecarton.com\n"
            "Reply-To: client2@example.com\n"
            "Subject: Shipmecarton - Order 23551\n\n"
            "Payment amount: $180.00\n"
            "Order ID: 23551\n"
            "Firstname: Test Client Two\n"
            "Street address1: 456 Oak Ave\n"
            "Town/City: Chicago\n"
            "State: Illinois\n"
            "Postcode/Zip: 60601\n"
            "Email: client2@example.com",
        ),
        (
            "TRACKING question (AI fallback)",
            "Process this email:\n\n"
            "From: client2@example.com\n"
            "Subject: Re: Order 23551\n"
            "Body: Hey, when will my order be shipped? I need it by Friday.",
        ),
        (
            "THANK YOU (no reply needed)",
            "Process this email:\n\n"
            "From: client2@example.com\n"
            "Subject: Re: Order 23551\n"
            "Body: Thank you so much!",
        ),
    ]

    for name, prompt in tests:
        print("\n" + "=" * 60)
        print(f"TEST: {name}")
        print("=" * 60)
        email_agent.print_response(prompt, stream=True)
