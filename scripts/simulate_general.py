"""
Simulate the general handler's GPT response for Justin's complaint.

Rebuilds the exact prompt format from context.py + formatters.py,
uses the NEW general_instructions, calls GPT-5.2, prints the result.

Usage:
    cd "ag infra up"
    OPENAI_API_KEY=... python scripts/simulate_general.py
"""

import os
import sys

from openai import OpenAI

# ── New general_instructions (copy from general.py) ──────────────────
SYSTEM_PROMPT = """\
You are James, customer service at shipmecarton.com — an online store selling \
Terea and IQOS heated tobacco sticks. We ship from USA via USPS (2-4 days). \
We accept Zelle (preferred) and Cash App.

You receive full context: client profile, conversation history from this thread, \
conversation state, and policy rules. USE them.

THINK BEFORE YOU REPLY:
- Read the conversation history carefully — it shows what was ordered, what was \
  discussed, what we promised, and what actually happened.
- If the customer has a problem, ANALYZE the history to understand what went wrong. \
  Explain what you see. Be specific — reference actual products, dates, messages.
- Don't dodge with "we'll check" if the answer is visible in the conversation. \
  Give a direct, helpful response based on the facts you have.
- If something truly requires checking warehouse/systems (tracking status, \
  stock levels, internal records) — then say you'll check.

TOOLS:
- If customer asks about product availability → call search_stock_tool
- For topics outside our product line → use web search

STYLE:
- Casual, friendly — like texting a business contact
- 2-5 sentences. No formality, no signature, no "Best regards"
- Match the tone from previous [WE SENT] messages if available
- Always end with exactly "Thank you!"

Follow the POLICY RULES section strictly.
"""

# ── Reconstruct the prompt exactly like format_context_for_prompt ─────

CLIENT_PROFILE = """\
=== CLIENT PROFILE ===
Name: Justin Scheper
Payment type: prepay
Discount: 5% for next 3 orders
Status: active
Summary: Returning customer (multiple orders since late Jan 2026), \
generally ordering ILUMA PRIME devices and bulk Terea cartons. \
Prefers Mint (Japan) and Green Turquoise/Green (Middle East), \
typically around 6 cartons per order. Pays promptly via Cash App."""

# History: exactly what GPT would see (300-char truncated bodies, oldest first)
# Excludes the operator's manual response (id 505) — that's what we're simulating
CONVERSATION_HISTORY = """\
=== CONVERSATION HISTORY ===

[CLIENT WROTE] 2026-03-02 | Re: Shipmecarton - Order 13705
Hello-
May I place an order for 3 mint and 3 green turquoise (Middle East), please?
Best, Justin
---
[WE SENT] 2026-03-02 | Re: Re: Shipmecarton - Order 13705
Hello Justin
How are you?
We can send you 3 Terea Mint, made in Japan, and 3 Terea Green, made in the Middle East
Please let us know if this will work for you.
---
[CLIENT WROTE] 2026-03-02 | Re: Shipmecarton - Order 13705
Hello-
Yes, that is perfect. Please send final total amount and I will send $$.
Thank you.
Best, Justin
---
[WE SENT] 2026-03-02 | Re: Re: Shipmecarton - Order 13705
Thank you very much for ordering with us !!!
Total is $690 FREE shipping
- FOR CASH APP
if want to use CASH APP here is instructions:
!!! In Memo or Comments don't put anything please !!!
Use id below:
$Honestbuy777
---
[CLIENT WROTE] 2026-03-02 | Re: Shipmecarton - Order 13705
Thanks, $$ sent.
Best, Justin
---
[WE SENT] 2026-03-03 | Re: Re: Shipmecarton - Order 13705
Hello
Thank you very much for a prompt payment!
Nice doing business with you!!!
We will ship your order Today
Here is USPS tracking number:
https://tools.usps.com/go/TrackConfirmAction.action?tLabels=9405536106196283040435
*
Justin Scheper
446 Bertling Street
Cape Girardeau MO 63701
---"""

POLICY_RULES = """\
=== POLICY RULES ===
[Tone & Style]
- You are James from shipmecarton.com
- Casual, friendly tone — like texting a business contact
- Always end with exactly 'Thank you!' — nothing after it, no name, no signature
- Keep replies 2-5 sentences max
- If history has [WE SENT] messages — match that exact wording and style
- Most recent messages carry more weight than older ones
- Start with 'Hi {name},' or 'Hello,' if name available
- If replying to existing thread, skip greeting
- NEVER write multiple reply variants — only ONE reply
- NEVER add a signature line or 'Best regards'
- NEVER use corporate/formal language

[Absolute Constraints]
(MANDATORY — never violate)
- NEVER invent tracking numbers, prices, delivery dates, or stock levels
- NEVER reveal that you are AI or automated
- NEVER tell customer to check the website — WE always check for them
- NEVER mention competitors
- NEVER change payment terms (prepay/postpay) without authorization
- NEVER offer discounts not already in the system
- NEVER promise something not confirmed in the context
- If something requires checking warehouse, internal systems, or data NOT in your context — say you'll check and get back
- If the answer IS visible in the conversation history or context — give it directly, don't dodge
- Only reference facts provided in the context — do not hallucinate"""

CUSTOMER_EMAIL = """\
=== CUSTOMER'S EMAIL ===
From: Justin Scheper <justinscheper@gmail.com>
Subject: Re: Shipmecarton - Order 13705
Body: Hello-

Just received order and I received green from Middle East. In my original email I stated green turquoise.

How can we fix this?


Best,
Justin

Sent from my iPhone. Please excuse brevity and grammatical errors."""

# ── Build the full prompt ─────────────────────────────────────────────
USER_PROMPT = "\n\n".join([
    CLIENT_PROFILE,
    CONVERSATION_HISTORY,
    POLICY_RULES,
    CUSTOMER_EMAIL,
]) + "\n\nWrite a reply:"


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: Set OPENAI_API_KEY environment variable")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    print("=" * 60)
    print("SYSTEM PROMPT (new general_instructions):")
    print("=" * 60)
    print(SYSTEM_PROMPT)
    print()
    print("=" * 60)
    print("USER PROMPT (context + email):")
    print("=" * 60)
    print(USER_PROMPT)
    print()
    print("=" * 60)
    print("GPT-5.2 RESPONSE:")
    print("=" * 60)

    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        temperature=0.3,
    )

    reply = response.choices[0].message.content
    print(reply)
    print()
    print(f"Tokens: {response.usage.prompt_tokens} prompt + {response.usage.completion_tokens} completion")


if __name__ == "__main__":
    main()
