"""
Alternatives Agent
------------------

LLM-powered out-of-stock alternative selector.

Given an OOS flavor, available stock, and client context (order history +
profile), asks gpt-4o-mini to pick the best alternatives from the provided
stock list. Uses CATEGORY|PRODUCT_NAME compound keys to avoid ambiguity
(same flavor name can exist in multiple categories).

Never raises — all exceptions are caught and logged; caller falls back to
a quantity-based heuristic.
"""

import json
import logging
import re

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent instructions
# ---------------------------------------------------------------------------
_INSTRUCTIONS = """\
You are a product recommendation specialist for an IQOS tobacco product store.

## Product regions and customer-facing names
We have sticks from different production regions. The INTERNAL category names
in the stock list map to CUSTOMER-FACING region labels as follows:
- TEREA_EUROPE → "EU" (made in Europe)
- ARMENIA → "ME" (Middle East)
- KZ_TEREA → "ME" (Middle East)
- TEREA_JAPAN → "made in Japan"
- УНИКАЛЬНАЯ_ТЕРЕА → "made in Japan" (unique Japan flavors)

IMPORTANT: ARMENIA and KZ_TEREA are BOTH "Middle East" (ME) for the customer.
The customer sees NO difference between Armenia and KZ products — they are
the same region from their perspective. Treat them as interchangeable.

## Product types
STICKS (tobacco consumables for IQOS devices):
- Terea EU: Turquoise (menthol/fresh), Green (strong menthol), Silver (classic tobacco),
  Purple Wave (berry), Warm Regular (warm tobacco), Amber (rich), Bronze (mild)
- Terea ME (Armenia + KZ): same flavor profiles as EU, different production region
- Terea Japan: T Mint (strong mint), T Silver (classic), T Purple (grape)
- Menthol family: Turquoise, Green, T Mint — all cooling/fresh
- Classic family: Silver, Warm Regular, Amber, Bronze — no menthol

DEVICES (IQOS hardware, NOT consumables):
- ONE, STND, PRIME — available in colors (Green, Red, Black, Silver, etc.)
- Only suggest device alternatives when the OOS item is also a device.

## Selection rules
- ONLY use keys EXACTLY as listed in AVAILABLE STOCK — never invent new ones
- Never suggest the out-of-stock flavor itself
- Never suggest keys listed in EXCLUDED
- Priority order:
  1) SAME FLAVOR from a different region (e.g. if Amber EU is OOS, suggest
     Amber from ARMENIA or KZ_TEREA first — it's the same taste, just
     different production origin)
  2) Items the customer has ordered before
  3) Items matching the customer's taste profile (same flavor family)
  4) Popular available items by quantity
- For customers with no history or profile: prefer the same flavor family
  (menthol → menthol, classic → classic)
- Return up to {max_options} choices
"""

_PROMPT_TEMPLATE = """\
## OOS flavor
{oos_flavor}

## Available stock  (format: "CATEGORY|PRODUCT_NAME  qty: N")
{stock_lines}

## Customer order history  (most ordered first)
{history_text}

## Customer profile
{profile_text}

## Already suggested for other OOS flavors in this order (exclude these)
{excluded_text}

Return ONLY a JSON array of keys from the available stock list above.
Example: ["TEREA_EUROPE|Green", "ARMENIA|Turquoise"]
If nothing fits, return an empty array: []
"""

# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def get_llm_alternatives(
    oos_flavor: str,
    available_items: list[dict],
    order_history: list[dict],
    client_summary: str,
    max_options: int = 3,
    excluded_products: set[str] | None = None,
) -> list[dict]:
    """Return up to max_options stock item dicts chosen by LLM.

    Args:
        oos_flavor: The base flavor that is out of stock.
        available_items: List of stock item dicts (product_name, category, quantity, ...).
        order_history: List of {base_flavor, order_count} sorted by frequency.
        client_summary: Client's llm_summary text (may be empty for new clients).
        max_options: Maximum number of alternatives to return.
        excluded_products: Product names already suggested for other OOS flavors
            in the same order. Prevents identical suggestions across multiple flavors.

    Returns:
        List of stock item dicts (same shape as available_items entries).
        Empty list on any error or when LLM finds nothing suitable.
        NEVER raises an exception.
    """
    if not available_items:
        return []

    _excluded = excluded_products or set()

    try:
        # Build compound-key → item map (unique even for same name in diff categories)
        key_to_item: dict[str, dict] = {
            f"{it['category']}|{it['product_name']}": it
            for it in available_items
        }

        # Format stock list for prompt
        stock_lines = "\n".join(
            f"  {key}  qty: {it['quantity']}"
            for key, it in key_to_item.items()
        )

        # Format history
        if order_history:
            history_text = ", ".join(
                f"{h['base_flavor']} ({h['order_count']}x)" for h in order_history
            )
        else:
            history_text = "No order history available."

        profile_text = client_summary.strip() or "No profile available."

        # Format excluded as CATEGORY|PRODUCT_NAME keys so LLM understands exactly
        excluded_keys = [k for k, it in key_to_item.items() if it["product_name"] in _excluded]
        excluded_text = ", ".join(sorted(excluded_keys)) if excluded_keys else "None"

        prompt = _PROMPT_TEMPLATE.format(
            oos_flavor=oos_flavor,
            stock_lines=stock_lines,
            history_text=history_text,
            profile_text=profile_text,
            excluded_text=excluded_text,
            max_options=max_options,
        )

        # Inject max_options into instructions
        instructions = _INSTRUCTIONS.format(max_options=max_options)

        agent = Agent(
            id="alternatives-selector",
            name="Alternatives Selector",
            model=OpenAIResponses(id="gpt-4.1"),
            instructions=instructions,
            markdown=False,
        )
        response = agent.run(prompt)
        raw = response.content.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            logger.warning("LLM alternatives: unexpected response type for '%s': %r", oos_flavor, raw)
            return []

        # Validate each key — case-insensitive fallback
        key_to_item_lower = {k.lower(): v for k, v in key_to_item.items()}
        result: list[dict] = []
        seen_keys: set[str] = set()

        for key in parsed:
            if not isinstance(key, str):
                continue

            item = key_to_item.get(key)
            if item is None:
                # Case-insensitive fallback
                item = key_to_item_lower.get(key.lower())
                if item is None:
                    logger.warning(
                        "LLM alternatives: unknown key '%s' for OOS '%s' — dropped",
                        key, oos_flavor,
                    )
                    continue

            canon_key = f"{item['category']}|{item['product_name']}"
            if canon_key in seen_keys:
                continue
            if item["product_name"] in _excluded:
                continue

            seen_keys.add(canon_key)
            result.append(item)
            if len(result) >= max_options:
                break

        logger.info(
            "LLM alternatives for '%s': %s",
            oos_flavor,
            [f"{it['category']}|{it['product_name']}" for it in result],
        )
        return result

    except Exception as exc:
        logger.warning("LLM alternatives failed for '%s': %s", oos_flavor, exc)
        return []
