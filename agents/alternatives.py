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

## Product regions
- TEREA_EUROPE → "EU"
- ARMENIA, KZ_TEREA → "ME" (Middle East) — interchangeable for the customer
- TEREA_JAPAN, УНИКАЛЬНАЯ_ТЕРЕА → "made in Japan"

## Available stock format
Each line in the stock list:
  KEY: CATEGORY|PRODUCT_NAME  family: FLAVOR_FAMILY  qty: N

The KEY is CATEGORY|PRODUCT_NAME (e.g. "ARMENIA|Silver").
The family tag tells you the taste profile — suggest from the SAME family.
Return ONLY the KEY part (CATEGORY|PRODUCT_NAME), nothing else.

## Flavor families (taste taxonomy)
- tobacco       — pure tobacco, no menthol, no fruit (e.g. Amber, Bronze, Silver, Teak)
- menthol       — pure menthol/mint, no significant fruit (e.g. Green, Turquoise, Blue)
- menthol_fruit — menthol + significant fruit/berry/citrus (e.g. Fusion Menthol, MAUVE, Willow, Black Purple Menthol)
- fruit         — fruity, no menthol (e.g. Purple, Ruby, Yellow, Summer, Oasis)
- capsule       — Pearl/click products with a flavor capsule (e.g. Sun Pearl, Abore Pearl, Starling)
- device        — IQOS hardware, never suggest for sticks

## Product types
DEVICES (IQOS hardware): ONE, STND, PRIME — only suggest devices for devices.
All other items are STICKS (tobacco consumables).

## CRITICAL RULE — flavor family matching
Look at the OOS item's flavor family. Then look at the "family:" tag on each available item.
You MUST pick alternatives from the SAME family as the OOS item.

FORBIDDEN cross-family substitutions (examples):
- tobacco OOS → suggesting menthol, fruit, or capsule = WRONG
- menthol OOS → suggesting tobacco or fruit = WRONG
- capsule OOS → suggesting anything except capsule = WRONG
- menthol_fruit OOS → suggesting fruit or menthol = WRONG (must be menthol_fruit)

If NO items from the same family are available, ONLY THEN you may pick from the closest family.
Closeness: tobacco↔fruit (both non-menthol), menthol↔menthol_fruit (both minty). capsule has no close family.

## Selection rules
- ONLY use keys EXACTLY as listed in AVAILABLE STOCK — never invent new ones
- Never suggest the out-of-stock flavor itself
- Never suggest keys listed in EXCLUDED
- Priority order (ALWAYS within the same flavor family first):
  1) SAME FLAVOR from a different region (e.g. Amber EU OOS → Amber ME)
  2) Items the customer has ordered before that match the flavor family
  3) Other items from the SAME flavor family
  4) ONLY if no same-family items exist: closest family, then popular by quantity
- Return up to {max_options} choices
"""

_PROMPT_TEMPLATE = """\
## OOS flavor
{oos_flavor} (flavor family: {oos_family})

## Available stock  (format: "KEY: CATEGORY|PRODUCT_NAME  family: FLAVOR  qty: N")
{stock_lines}

## Customer order history  (most ordered first)
{history_text}

## Customer profile
{profile_text}

## Already suggested for other OOS flavors in this order (exclude these)
{excluded_text}
{region_constraint}
Return ONLY a JSON array of KEY values (CATEGORY|PRODUCT_NAME) from the list above.
Do NOT include family, qty, or any other text in the keys.
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
    oos_flavor_family: str | None = None,
    region_preference: list[str] | None = None,
    strict_region: bool = False,
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
        oos_flavor_family: Flavor family of the OOS product (e.g. "classic", "menthol").

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

        # Format stock list for prompt — KEY separate from metadata
        stock_lines = "\n".join(
            f"  KEY: {key}  family: {it.get('flavor_family') or 'unknown'}  qty: {it['quantity']}"
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

        # Build region constraint block for prompt (strict vs soft)
        region_constraint = ""
        if region_preference:
            from db.region_family import REGION_FAMILIES
            allowed_cats = set()
            for region in region_preference:
                cats = REGION_FAMILIES.get(region)
                if cats:
                    allowed_cats |= cats
            if allowed_cats:
                region_names = ", ".join(region_preference)
                cat_names = ", ".join(sorted(allowed_cats))
                is_strict = strict_region
                if is_strict:
                    region_constraint = (
                        f"\n## Region constraint\n"
                        f"Customer requested: {region_names}\n"
                        f"Allowed categories: {cat_names}\n"
                        f"STRICT: only suggest from these categories. "
                        f"Do NOT suggest products from other regions."
                    )
                else:
                    region_constraint = (
                        f"\n## Region preference\n"
                        f"Customer prefers: {region_names}\n"
                        f"Preferred categories: {cat_names}\n"
                        f"PREFER these categories first, but other regions are acceptable "
                        f"if no good alternatives exist in the preferred region."
                    )

        prompt = _PROMPT_TEMPLATE.format(
            oos_flavor=oos_flavor,
            oos_family=oos_flavor_family or "unknown",
            stock_lines=stock_lines,
            history_text=history_text,
            profile_text=profile_text,
            excluded_text=excluded_text,
            region_constraint=region_constraint,
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

            # Strip any trailing parenthesized text LLM might add
            # e.g. "KZ_TEREA|Amber (classic)" → "KZ_TEREA|Amber"
            clean_key = re.sub(r"\s*\(.*?\)\s*$", "", key).strip()

            item = key_to_item.get(clean_key)
            if item is None:
                # Case-insensitive fallback
                item = key_to_item_lower.get(clean_key.lower())
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
