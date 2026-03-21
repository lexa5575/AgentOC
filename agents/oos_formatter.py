"""
OOS Alternatives Formatter
--------------------------

Small LLM agent that formats out-of-stock alternative suggestions
into 1-3 lines of customer-facing text. Does NOT select alternatives
(that's done by select_best_alternatives) — only formats them.

Also contains a local validator that checks LLM output before it
reaches the customer draft. This is critical because the template
path bypasses the global checker in pipeline.py.
"""

import logging
import re

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM Agent
# ---------------------------------------------------------------------------

_FORMATTER_INSTRUCTIONS = """\
You are a formatting assistant for customer service emails at an online store.

You receive a list of out-of-stock items and their pre-selected alternatives.
Your ONLY job is to format them into 1-3 lines of plain text.

You will be told which format_mode to use. Follow the rules for that mode EXACTLY.

## format_mode = "single_item"
- List up to 3 alternatives, NO quantities.
- If an alternative has reason "same_flavor", add "(same product, different region)" after it.
- Output is ONE line starting with "We have alternatives:".
- Example: We have alternatives: Terea Amber ME (same product, different region), Terea Silver ME, Terea Sof Fuse EU

## format_mode = "all_same_flavor_grouped"
- 1 alternative per item, WITH quantities (use missing_qty).
- All on ONE line, with "(same product, different region)" at the end.
- Example: We have alternatives: 1 x Terea Amber ME, 2 x Terea Yellow ME, 1 x Terea Silver ME (same product, different region)

## format_mode = "per_item_mapping"
- 1 alternative per item, WITH quantities (use missing_qty).
- Each item on its own line: "For {oos_name}: {qty} x {alt_name}"
- If the alternative has reason "same_flavor", add "(same product, different region)" after the alt name.
- First line is "We have alternatives:"
- Example:
  We have alternatives:
     For Terea Mauve EU: 1 x Terea Purple Japan
     For Terea Amber EU: 3 x Terea Amber ME (same product, different region)

## format_mode = "hybrid_mixed"
- same_flavor items: grouped on ONE line with quantities + "(same product, different region)" at end.
- Other items: each on its own line "For {oos_name}: {qty} x {alt_name}"
- First line is "We have alternatives:"
- Example:
  We have alternatives:
     1 x Terea Amber ME, 2 x Terea Yellow ME (same product, different region)
     For Terea Mauve EU: 1 x Terea Purple Japan

## CRITICAL RULES
- Output ONLY the formatted lines. No greeting, no explanation, no signature.
- Use product names EXACTLY as provided. Do not abbreviate or modify them.
- Use quantities EXACTLY as provided (missing_qty field).
- Do not add products that are not in the input.
- Do not reorder products — keep the same order as input.
"""

_PROMPT_TEMPLATE = """\
format_mode: {format_mode}
total_oos_items_in_order: {total_oos_count}

Items and alternatives:
{items_block}

Format the alternatives according to the rules for "{format_mode}" mode.
"""


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def format_alternatives_line(
    oos_items: list[dict],
    format_mode: str,
    total_oos_count: int,
) -> str | None:
    """Format OOS alternatives into 1-3 lines of customer-facing text.

    Args:
        oos_items: List of dicts, each with:
            - display_name: customer-facing OOS item name
            - ordered_qty: how many customer ordered
            - total_available: how many we have
            - missing_qty: how many we're short
            - alternatives: list of {display_name, reason}
        format_mode: One of "single_item", "all_same_flavor_grouped",
            "per_item_mapping", "hybrid_mixed".
        total_oos_count: Total number of OOS items in the order
            (including those without alternatives).

    Returns:
        Formatted plain text string, or None on any error.
    """
    if not oos_items:
        return None

    try:
        # Build structured text for the LLM
        lines = []
        for item in oos_items:
            alts_text = []
            for alt in item["alternatives"]:
                alts_text.append(
                    f"  - {alt['display_name']} [reason: {alt['reason']}]"
                )
            lines.append(
                f"- {item['display_name']} "
                f"(ordered {item['ordered_qty']}, "
                f"have {item['total_available']}, "
                f"missing_qty {item['missing_qty']}):\n"
                + "\n".join(alts_text)
            )
        items_block = "\n".join(lines)

        prompt = _PROMPT_TEMPLATE.format(
            format_mode=format_mode,
            total_oos_count=total_oos_count,
            items_block=items_block,
        )

        agent = Agent(
            id="oos-formatter",
            name="OOS Formatter",
            model=OpenAIResponses(id="gpt-4.1-mini"),
            instructions=_FORMATTER_INSTRUCTIONS,
            markdown=False,
        )
        response = agent.run(prompt)
        raw = response.content.strip()

        if not raw:
            logger.warning("OOS formatter returned empty response")
            return None

        # Validate before returning
        validated = _validate_formatter_output(raw, oos_items, format_mode)
        return validated

    except Exception as exc:
        logger.warning("OOS formatter failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

# Allowed structural tokens (not product names)
_STRUCTURAL_TOKENS = {
    "we", "have", "alternatives:", "alternatives",
    "for", "x", ",", "same", "product", "different",
    "region", "(same", "region)",
    "(same product, different region)",
}

_GREETING_PATTERNS = re.compile(
    r"\b(hi|hello|hey|thank you|thanks|best regards|sincerely|cheers)\b",
    re.IGNORECASE,
)


def _validate_formatter_output(
    raw_output: str,
    formatter_input: list[dict],
    format_mode: str,
) -> str | None:
    """Validate LLM formatter output against expected format and data.

    Validator does NOT compute format_mode — receives it as argument
    (single source of truth from _build_formatter_input).
    formatter_input is used only for semantic validation.

    Returns validated output string, or None if validation fails.
    """
    # Level 1: structural checks
    if not raw_output or not raw_output.strip():
        logger.warning("Formatter validation failed (mode=%s): empty output", format_mode)
        return None

    lines = [l for l in raw_output.strip().split("\n") if l.strip()]
    if len(lines) > 5:
        logger.warning(
            "Formatter validation failed (mode=%s): too many lines (%d)",
            format_mode, len(lines),
        )
        return None

    if _GREETING_PATTERNS.search(raw_output):
        logger.warning(
            "Formatter validation failed (mode=%s): contains greeting/signature",
            format_mode,
        )
        return None

    # Collect all known names for level 3
    all_known_names: set[str] = set()
    for item in formatter_input:
        all_known_names.add(item["display_name"])
        for alt in item["alternatives"]:
            all_known_names.add(alt["display_name"])

    # Level 2: per-mode semantic checks
    if format_mode == "single_item":
        result = _validate_single_item(raw_output, formatter_input)
    elif format_mode == "all_same_flavor_grouped":
        result = _validate_all_same_flavor(raw_output, formatter_input)
    elif format_mode == "per_item_mapping":
        result = _validate_per_item_mapping(raw_output, formatter_input)
    elif format_mode == "hybrid_mixed":
        result = _validate_hybrid_mixed(raw_output, formatter_input)
    else:
        logger.warning("Formatter validation failed: unknown mode '%s'", format_mode)
        return None

    if result is None:
        return None

    # Level 3: grammar-based check for unknown content
    check = _check_no_unknown_names(raw_output, all_known_names)
    if not check:
        return None

    return raw_output.strip()


def _validate_single_item(output: str, formatter_input: list[dict]) -> str | None:
    """Validate single_item mode: all alt names present, no For:, no qty required."""
    item = formatter_input[0]

    # Must not contain "For ... :"
    if re.search(r"For\s+\S.*:", output):
        logger.warning(
            "Formatter validation failed (mode=single_item): contains 'For ...:'",
        )
        return None

    # All alternative display names must be present
    for alt in item["alternatives"]:
        if alt["display_name"] not in output:
            logger.warning(
                "Formatter validation failed (mode=single_item): "
                "missing alt '%s' in output",
                alt["display_name"],
            )
            return None

    return output


def _validate_all_same_flavor(output: str, formatter_input: list[dict]) -> str | None:
    """Validate all_same_flavor_grouped: qty x alt for each, no For:, has region note."""
    # Must not contain "For ... :"
    if re.search(r"For\s+\S.*:", output):
        logger.warning(
            "Formatter validation failed (mode=all_same_flavor_grouped): "
            "contains 'For ...:'",
        )
        return None

    # "(same product, different region)" must be present
    if "(same product, different region)" not in output:
        logger.warning(
            "Formatter validation failed (mode=all_same_flavor_grouped): "
            "missing region note",
        )
        return None

    # For each item: {missing_qty} x {alt_display} must be present
    for item in formatter_input:
        alt = item["alternatives"][0]
        qty = item["missing_qty"]
        pattern = rf"{qty}\s*x\s*{re.escape(alt['display_name'])}"
        if not re.search(pattern, output):
            logger.warning(
                "Formatter validation failed (mode=all_same_flavor_grouped): "
                "missing '%d x %s'",
                qty, alt["display_name"],
            )
            return None

    return output


def _validate_per_item_mapping(output: str, formatter_input: list[dict]) -> str | None:
    """Validate per_item_mapping: For {oos}: {qty} x {alt} per item, check pairs."""
    for item in formatter_input:
        oos_name = item["display_name"]
        alt = item["alternatives"][0]
        alt_name = alt["display_name"]
        qty = item["missing_qty"]

        # Find line with "For {oos_name}:"
        pattern = rf"For\s+{re.escape(oos_name)}:\s*{qty}\s*x\s*{re.escape(alt_name)}"
        if not re.search(pattern, output):
            logger.warning(
                "Formatter validation failed (mode=per_item_mapping): "
                "missing or wrong pair for '%s' -> '%d x %s'",
                oos_name, qty, alt_name,
            )
            return None

        # same_flavor items must have region note on their line
        if alt["reason"] == "same_flavor":
            # Find the specific line for this item
            for line in output.split("\n"):
                if oos_name in line and alt_name in line:
                    if "(same product, different region)" not in line:
                        logger.warning(
                            "Formatter validation failed (mode=per_item_mapping): "
                            "same_flavor item '%s' missing region note",
                            oos_name,
                        )
                        return None
                    break

    return output


def _validate_hybrid_mixed(output: str, formatter_input: list[dict]) -> str | None:
    """Validate hybrid_mixed: grouped same_flavor line + For lines for others."""
    same_flavor_items = [
        i for i in formatter_input
        if i["alternatives"][0]["reason"] == "same_flavor"
    ]
    other_items = [
        i for i in formatter_input
        if i["alternatives"][0]["reason"] != "same_flavor"
    ]

    # Validate grouped same_flavor part
    if same_flavor_items:
        if "(same product, different region)" not in output:
            logger.warning(
                "Formatter validation failed (mode=hybrid_mixed): "
                "missing region note for same_flavor group",
            )
            return None
        for item in same_flavor_items:
            alt = item["alternatives"][0]
            qty = item["missing_qty"]
            pattern = rf"{qty}\s*x\s*{re.escape(alt['display_name'])}"
            if not re.search(pattern, output):
                logger.warning(
                    "Formatter validation failed (mode=hybrid_mixed): "
                    "missing '%d x %s' in grouped line",
                    qty, alt["display_name"],
                )
                return None

    # Validate per-item part for other items
    for item in other_items:
        oos_name = item["display_name"]
        alt = item["alternatives"][0]
        alt_name = alt["display_name"]
        qty = item["missing_qty"]

        pattern = rf"For\s+{re.escape(oos_name)}:\s*{qty}\s*x\s*{re.escape(alt_name)}"
        if not re.search(pattern, output):
            logger.warning(
                "Formatter validation failed (mode=hybrid_mixed): "
                "missing or wrong pair for '%s' -> '%d x %s'",
                oos_name, qty, alt_name,
            )
            return None

    return output


def _check_no_unknown_names(output: str, all_known_names: set[str]) -> bool:
    """Check that output doesn't contain unknown product names.

    Extracts all product-like mentions and verifies they're in the known set.
    Returns True if valid, False if unknown names found.
    """
    # Strip known structural parts to isolate product mentions
    cleaned = output
    # Remove known structural phrases
    for phrase in [
        "We have alternatives:",
        "(same product, different region)",
        "We have",
    ]:
        cleaned = cleaned.replace(phrase, "")

    # Remove "For ... :" prefixes (the OOS item names are known)
    cleaned = re.sub(r"For\s+", "", cleaned)
    # Remove quantity patterns like "1 x", "2 x"
    cleaned = re.sub(r"\d+\s*x\s*", "", cleaned)
    # Remove punctuation
    cleaned = re.sub(r"[,:()]", " ", cleaned)

    # Split into tokens and look for product-like sequences
    # Product names typically start with "Terea", "ONE", "STND", "PRIME", "Heets"
    # We check each known name is accounted for, and flag unknown sequences
    remaining = cleaned.strip()
    for name in sorted(all_known_names, key=len, reverse=True):
        remaining = remaining.replace(name, "")

    # After removing all known names, only whitespace should remain
    remaining = remaining.strip()
    if remaining and len(remaining) > 10:
        # Allow small residue (punctuation artifacts, "alternatives" word, etc.)
        # But flag if there's substantial unknown text
        logger.warning(
            "Formatter validation failed: unknown content in output: '%s'",
            remaining[:100],
        )
        return False

    return True
