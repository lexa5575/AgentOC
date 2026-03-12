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

import json
import logging
import re

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.context import build_context, format_context_for_prompt
from agents.handlers.template_utils import fill_template_reply
from tools.stock_tools import search_stock_tool
from agents.reply_templates import REPLY_TEMPLATES
from db.memory import (
    check_stock_for_order,
    calculate_order_price,
    get_full_thread_history,
    resolve_order_items,
)
from tools.email_parser import _strip_quoted_text
from db.catalog import get_display_name

logger = logging.getLogger(__name__)

# Sources trusted for persistence and fulfillment (plan §3)
TRUSTED_SOURCES = {"thread_extraction", "pending_oos"}

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
                # Only one alternative — auto-pick with region from category
                alt = alts[0]
                alt_pn = alt["product_name"]
                alt_cat = alt.get("category", "")
                region_suffix = _CATEGORY_TO_REGION_SUFFIX.get(alt_cat)
                if region_suffix:
                    alt_pn = f"{alt_pn} {region_suffix}"
                confirmed.append({
                    "base_flavor": alt["product_name"],
                    "product_name": alt_pn,
                    "quantity": item["requested_qty"],
                })
            else:
                # Multiple alternatives — try to match from email text
                matched = _match_alternative_from_text(email_text, alts)
                if matched:
                    m_pn = matched["product_name"]
                    m_cat = matched.get("category", "")
                    region_suffix = _CATEGORY_TO_REGION_SUFFIX.get(m_cat)
                    if region_suffix:
                        m_pn = f"{m_pn} {region_suffix}"
                    confirmed.append({
                        "base_flavor": matched["product_name"],
                        "product_name": m_pn,
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


def _resolve_from_classifier(classification) -> list[dict] | None:
    """Extract confirmed items from classifier's order_items.

    Fallback when pending_oos_resolution is missing — the classifier
    can see conversation history and extract what the customer agreed to.
    """
    order_items = getattr(classification, "order_items", None) or []
    if not order_items:
        return None

    confirmed = []
    for oi in order_items:
        bf = getattr(oi, "base_flavor", None)
        pn = getattr(oi, "product_name", None)
        qty = getattr(oi, "quantity", 1)
        if bf or pn:
            confirmed.append({
                "base_flavor": bf or pn,
                "product_name": pn or bf,
                "quantity": qty or 1,
            })

    return confirmed if confirmed else None


def _build_order_summary(stock_items: list[dict]) -> str:
    """Build order summary string like '2 x Terea Tropical Japan, 1 x Terea Black Japan'.

    Prefers resolved display_name (region-aware from resolver) over
    entries[0].category fallback to avoid wrong region display.
    """
    parts = []
    for item in stock_items:
        display = item.get("display_name")
        if not display:
            cat = ""
            entries = item.get("stock_entries") or []
            if entries:
                cat = entries[0].get("category", "")
            name = item.get("product_name") or item.get("base_flavor", "")
            display = get_display_name(name, cat)
        parts.append(f"{item['ordered_qty']} x {display}")
    return ", ".join(parts)


def _clear_pending_oos(result: dict) -> None:
    """Remove pending_oos_resolution from state facts (persisted by email_agent outbound save)."""
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    facts.pop("pending_oos_resolution", None)


# ---------------------------------------------------------------------------
# v3 helpers: order_id normalization, confirmation flags, thread extraction
# ---------------------------------------------------------------------------

def _normalize_order_id(classification) -> str | None:
    """Normalize order_id: strip whitespace, empty → None (plan §4)."""
    raw = getattr(classification, "order_id", None)
    return (raw or "").strip() or None


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
            logger.warning(
                "OOS agrees (%s): order_id=None → persistence/fulfillment skipped",
                result.get("client_email", "?"),
            )


# Category → region suffix mapping (single source of truth in region_family)
from db.region_family import CATEGORY_REGION_SUFFIX as _CATEGORY_TO_REGION_SUFFIX

# Known region tokens (lowered) → normalized suffix
_REGION_TOKEN_MAP: dict[str, str] = {
    "eu": "EU",
    "europe": "EU",
    "european": "EU",
    "japan": "Japan",
    "japanese": "Japan",
    "jp": "Japan",
    "me": "ME",
    "middle east": "ME",
    "armenia": "ME",
    "armenian": "ME",
    "kz": "KZ",
    "kazakhstan": "KZ",
}


def _detect_region_and_core(text: str) -> tuple[str | None, str]:
    """Detect region suffix and extract core flavor from a single text field.

    Returns (region_suffix, core) where core has brand prefixes stripped.
    """
    if not text:
        return None, ""

    region_suffix = None
    core = text

    # Strip brand prefixes first (case-insensitive)
    core_lower_check = core.lower()
    for prefix in ("terea ", "tera ", "heets ", "t "):
        if core_lower_check.startswith(prefix):
            core = core[len(prefix):]
            break

    # Try suffix/prefix region detection on brand-stripped core
    core_lower = core.lower()
    for token, suffix in sorted(_REGION_TOKEN_MAP.items(), key=lambda x: -len(x[0])):
        if core_lower.endswith(" " + token):
            region_suffix = suffix
            core = core[:len(core) - len(token) - 1].strip()
            break
        elif core_lower.startswith(token + " "):
            region_suffix = suffix
            core = core[len(token) + 1:].strip()
            break

    return region_suffix, core.strip()


def _normalize_extracted_region(items: list[dict]) -> list[dict]:
    """Deterministic post-normalization of extracted items.

    Ensures:
    - base_flavor is core flavor WITHOUT region suffix
    - product_name has normalized region suffix if present

    Region detection priority:
    1. region from product_name
    2. if not found — region from base_flavor
    """
    result = []
    for item in items:
        pn = (item.get("product_name") or "").strip()
        bf = (item.get("base_flavor") or "").strip()
        qty = item.get("quantity", 1)

        # Detect region from product_name (primary)
        pn_region, pn_core = _detect_region_and_core(pn)

        # Detect region from base_flavor (fallback)
        bf_region, bf_core = _detect_region_and_core(bf)

        # Priority: product_name region > base_flavor region
        region_suffix = pn_region or bf_region

        # Use product_name core if available, else base_flavor core
        core = pn_core or bf_core

        # Build normalized names
        clean_bf = core
        clean_pn = f"{core} {region_suffix}" if region_suffix else core

        result.append({
            "base_flavor": clean_bf,
            "product_name": clean_pn,
            "quantity": max(1, int(qty)) if qty else 1,
        })

    return result


# ---------------------------------------------------------------------------
# Quantity enrichment from pending_oos_resolution (P0 fix)
# ---------------------------------------------------------------------------

_STANDALONE_QTY = re.compile(
    r'\b(\d+)\s*(?:box(?:es)?|carton(?:s)?|block(?:s)?|pack(?:s)?|unit(?:s)?|piece(?:s)?)\b',
    re.IGNORECASE,
)


def _extract_client_qty_for_flavor(inbound_text: str, base_flavor: str) -> int | None:
    """Extract quantity explicitly mentioned by customer near a specific flavor.

    Uses word boundaries to avoid false matches (e.g. "amber" won't match "remember").
    Returns the quantity if found, None otherwise.
    """
    if not base_flavor:
        return None
    escaped = re.escape(base_flavor.strip())
    # Optional brand prefix (Terea/IQOS/Heets) between number and flavor
    _brand = r'(?:terea|iqos|heets)\s+'
    patterns = [
        rf'\b(\d+)\s*x\s+(?:{_brand})?\b{escaped}\b',           # "2 x Terea Bronze" or "2 x Bronze"
        rf'\b(\d+)\s+(?:{_brand})?\b{escaped}\b',                # "1 Terea Bronze" or "1 Bronze"
        rf'\b(?:{_brand})?\b{escaped}\b\s*x\s*(\d+)',            # "Bronze x2" or "Terea Bronze x2"
        rf'\b(\d+)\s*(?:box(?:es)?|carton(?:s)?|block(?:s)?|pack(?:s)?|unit(?:s)?|piece(?:s)?)\s+(?:of\s+)?(?:{_brand})?\b{escaped}\b',
        rf'\b(?:{_brand})?\b{escaped}\b\s+(\d+)\s*(?:box(?:es)?|carton(?:s)?|block(?:s)?|pack(?:s)?|unit(?:s)?|piece(?:s)?)',
    ]
    m = re.search("|".join(patterns), inbound_text, re.IGNORECASE)
    if m:
        for g in m.groups():
            if g and g.isdigit():
                return int(g)
    return None


def _extract_standalone_qty(inbound_text: str) -> int | None:
    """Extract standalone quantity from text (no flavor nearby).

    Used only for single-item orders when no flavor-specific qty found.
    """
    m = _STANDALONE_QTY.search(inbound_text)
    if m:
        for g in m.groups():
            if g and g.isdigit():
                return int(g)
    return None


def _build_pending_qty_map(pending: dict) -> dict[str, int]:
    """Build base_flavor → requested_qty map from pending_oos_resolution.

    Includes direct OOS items, in-stock items, and reverse-mapped alternatives.
    Uses _detect_region_and_core() for proper multi-word flavor extraction.

    Conflict handling: if an alt maps to multiple parents with different qtys,
    that alt is excluded (logged as warning).
    """
    qty_map: dict[str, int] = {}

    for item in pending.get("items", []):
        bf = (item.get("base_flavor") or "").strip().lower()
        if bf:
            qty_map[bf] = item.get("requested_qty", 1)

    for item in pending.get("in_stock_items", []):
        bf = (item.get("base_flavor") or "").strip().lower()
        if bf:
            qty_map[bf] = item.get("ordered_qty", 1)

    # Reverse alternatives: alt_flavor → parent OOS requested_qty
    alt_conflicts: dict[str, set[int]] = {}
    alternatives = pending.get("alternatives", {})
    for oos_flavor, alt_data in alternatives.items():
        oos_bf = oos_flavor.strip().lower()
        parent_qty = qty_map.get(oos_bf)
        if parent_qty is None:
            logger.warning(
                "Reverse-map: OOS flavor '%s' not found in qty_map (keys: %s) — "
                "skipping alt enrichment for this flavor",
                oos_bf, list(qty_map.keys()),
            )
            continue
        for alt in alt_data.get("alternatives", []):
            alt_pn = (alt.get("product_name") or "").strip()
            if not alt_pn:
                continue
            _, alt_core = _detect_region_and_core(alt_pn)
            alt_bf = alt_core.strip().lower()
            if not alt_bf or alt_bf in qty_map:
                continue
            alt_conflicts.setdefault(alt_bf, set()).add(parent_qty)

    for alt_bf, qtys in alt_conflicts.items():
        if len(qtys) == 1:
            qty_map[alt_bf] = qtys.pop()
        else:
            logger.warning(
                "Qty conflict for alt '%s': parents have different qtys %s — skipping enrichment",
                alt_bf, qtys,
            )

    return qty_map


def _extract_base_flavor_from_label(label: str) -> str:
    """Extract base flavor from ordered_items label like 'Tera PURPLE WAVE made in Middle East x2'."""
    import re
    # Remove leading "Tera " / "Terea "
    s = re.sub(r"^(?:Tera|Terea)\s+", "", label, flags=re.IGNORECASE)
    # Remove trailing " xN"
    s = re.sub(r"\s+x\d+$", "", s, flags=re.IGNORECASE)
    # Remove region suffix: "made in ..." or " ME" / " EU" / " Japan" / " KZ"
    s = re.sub(r"\s+made\s+in\s+.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+(?:ME|EU|Japan|KZ|Armenia)$", "", s, flags=re.IGNORECASE)
    return s.strip() or label


def _extract_region_suffix_from_label(label: str) -> str:
    """Extract region suffix from label like 'Tera PURPLE WAVE made in Middle East x2' → 'ME'."""
    import re
    _REGION_MAP = {
        "middle east": "ME", "europe": "EU", "european": "EU",
        "japan": "Japan", "japanese": "Japan",
        "kazakhstan": "KZ", "armenia": "Armenia",
    }
    m = re.search(r"\bmade\s+in\s+(.+?)(?:\s+x\d+)?$", label, flags=re.IGNORECASE)
    if m:
        region_raw = m.group(1).strip().lower()
        return _REGION_MAP.get(region_raw, "")
    return ""


def _extract_qty_from_label(label: str) -> int:
    """Extract quantity from label like 'Tera PURPLE WAVE made in Middle East x2'."""
    import re
    m = re.search(r"\bx(\d+)\s*$", label, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1


def _merge_in_stock_items(
    extracted_items: list[dict],
    result: dict,
) -> list[dict]:
    """Merge in-stock items from pending_oos_resolution into extracted items.

    Thread extraction only returns items the customer mentioned (substitutions).
    Original in-stock items (not OOS) must be preserved in the final order.
    Skips items whose base_flavor already appears in extracted (avoids duplicates).
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")

    in_stock: list[dict] = []
    if pending:
        in_stock = pending.get("in_stock_items", [])
    elif facts.get("ordered_items") and facts.get("oos_items"):
        # Fallback: reconstruct in-stock from ordered_items minus oos_items
        oos_flavors = {
            _extract_base_flavor_from_label(oos).lower()
            for oos in facts["oos_items"]
        }
        for label in facts["ordered_items"]:
            bf = _extract_base_flavor_from_label(label)
            qty = _extract_qty_from_label(label)
            region = _extract_region_suffix_from_label(label)
            pn = f"{bf} {region}" if region else bf
            if bf.lower() not in oos_flavors:
                in_stock.append({
                    "base_flavor": bf,
                    "product_name": pn,
                    "ordered_qty": qty,
                })
                logger.info(
                    "Reconstructed in-stock item '%s' x%d from facts.ordered_items",
                    pn, qty,
                )

    if not in_stock:
        return extracted_items

    # Collect flavors already in extracted (lowercase for matching)
    extracted_flavors = {
        (item.get("base_flavor") or "").strip().lower()
        for item in extracted_items
    }

    merged = list(extracted_items)
    for item in in_stock:
        bf = (item.get("base_flavor") or "").strip()
        if bf.lower() not in extracted_flavors:
            merged.append({
                "base_flavor": bf,
                "product_name": item.get("product_name", bf),
                "quantity": item.get("ordered_qty", 1),
            })
            logger.info(
                "Merged in-stock item '%s' x%d into extraction result",
                bf, item.get("ordered_qty", 1),
            )

    return merged


def _enrich_qty_from_pending(
    extracted_items: list[dict],
    result: dict,
    inbound_text: str = "",
) -> list[dict]:
    """Enrich extracted quantities from pending_oos_resolution (per-item).

    For EACH item: if LLM returned qty=1 (default) and pending knows a higher qty,
    use pending qty — unless customer explicitly specified a qty near that flavor.
    Single-item special case: standalone qty (e.g. "just 1 box") counts as explicit.
    """
    state = result.get("conversation_state") or {}
    facts = state.get("facts") or {}
    pending = facts.get("pending_oos_resolution")
    if not pending:
        return extracted_items

    pending_qty = _build_pending_qty_map(pending)
    if not pending_qty:
        return extracted_items

    enriched = []
    for item in extracted_items:
        item = dict(item)
        bf = (item.get("base_flavor") or "").strip().lower()
        extracted_qty = item.get("quantity", 1)
        original_qty = pending_qty.get(bf)

        if original_qty and extracted_qty == 1 and original_qty > 1:
            client_qty = _extract_client_qty_for_flavor(inbound_text, bf)
            if client_qty is None and len(extracted_items) == 1:
                client_qty = _extract_standalone_qty(inbound_text)

            if client_qty is not None:
                item["quantity"] = client_qty
                logger.info(
                    "Keeping client-specified qty for '%s': %d", bf, client_qty,
                )
            else:
                item["quantity"] = original_qty
                logger.info(
                    "Enriched qty for '%s': 1 → %d (from pending_oos_resolution)",
                    bf, original_qty,
                )

        enriched.append(item)

    return enriched


def _extract_agreed_items_from_thread(
    gmail_thread_id: str,
    inbound_text: str,
    gmail_account: str = "default",
    result: dict | None = None,
) -> list[dict] | None:
    """Extract agreed items from thread history using structured LLM extraction (plan §7.2A).

    Reads outbound proposals from thread + current inbound reply,
    uses gpt-4.1 to extract the FINAL agreed order.
    Returns normalized list[dict] or None on failure/empty.
    """
    import openai as _openai

    try:
        history = get_full_thread_history(gmail_thread_id, gmail_account=gmail_account)

        # Get last 2-3 outbound messages (proposals)
        outbound = [h for h in history if h.get("direction") == "outbound"]
        outbound = outbound[-3:]

        if not outbound:
            logger.info("Thread extraction: no outbound messages in thread %s", gmail_thread_id)
            return None

        outbound_text = "\n---\n".join([
            f"[OUTBOUND {i+1}]\n{h.get('body', '')}"
            for i, h in enumerate(outbound)
        ])

        # Build qty hint from pending_oos_resolution (P1b)
        qty_hint = ""
        if result:
            _state = result.get("conversation_state") or {}
            _pending = (_state.get("facts") or {}).get("pending_oos_resolution")
            if _pending:
                qty_lines = []
                for _item in _pending.get("items", []):
                    qty_lines.append(
                        f"  {_item.get('base_flavor', '?')}: {_item.get('requested_qty', '?')}"
                    )
                for _item in _pending.get("in_stock_items", []):
                    qty_lines.append(
                        f"  {_item.get('base_flavor', '?')}: {_item.get('ordered_qty', '?')}"
                    )
                if qty_lines:
                    qty_hint = (
                        "\n\n=== ORIGINAL REQUESTED QUANTITIES ===\n"
                        + "\n".join(qty_lines)
                        + "\n\nIMPORTANT: Unless the customer explicitly changes the quantity, "
                        "use these original quantities.\n"
                    )

        prompt = (
            "You are an order extraction assistant.\n\n"
            "Below are our recent OUTBOUND proposals to a customer, "
            "followed by their INBOUND reply.\n"
            "Extract the FINAL agreed order items with quantities.\n\n"
            "Rules:\n"
            "- Apply any customer modifications to quantities or flavors\n"
            "- If customer says 'Ok', 'Sounds good', etc. → accept the latest proposal as-is\n"
            "- Return the COMPLETE FINAL order: keep all in-stock items from the original order "
            "AND apply substitutions for out-of-stock items\n"
            "- Each item must have: product_name, base_flavor, quantity\n"
            "- IMPORTANT: preserve the region/origin in product_name as a SUFFIX:\n"
            '  "EU Bronze" or "Bronze EU" → product_name: "Bronze EU"\n'
            '  "Japan Smooth" or "Japanese Smooth" → product_name: "Smooth Japan"\n'
            '  "ME Amber" or "Middle East Amber" → product_name: "Amber ME"\n'
            '  "KZ Silver" or "Kazakhstan Silver" → product_name: "Silver KZ"\n'
            '  "European Bronze" → product_name: "Bronze EU"\n'
            "  If no region is mentioned, omit the suffix.\n"
            "- base_flavor is the core flavor WITHOUT region (e.g. 'Bronze', 'Smooth')\n\n"
            f"=== OUTBOUND PROPOSALS ===\n{outbound_text}\n\n"
            f"=== INBOUND REPLY ===\n{inbound_text}\n"
            f"{qty_hint}\n"
            'Return JSON: {"items": [{"product_name": "...", "base_flavor": "...", '
            '"quantity": N}]}\n'
            'If you cannot determine the agreed items, return {"items": []}.'
        )

        client = _openai.OpenAI()
        response = client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=500,
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        items = data.get("items", [])

        if not items:
            logger.info("Thread extraction: no items extracted for thread %s", gmail_thread_id)
            return None

        raw_items: list[dict] = []
        for item in items:
            bf = item.get("base_flavor", "").strip()
            pn = item.get("product_name", "").strip()
            qty = item.get("quantity", 1)
            if bf or pn:
                raw_items.append({
                    "base_flavor": bf or pn,
                    "product_name": pn or bf,
                    "quantity": max(1, int(qty)) if qty else 1,
                })

        if not raw_items:
            return None

        # Deterministic post-normalization: region suffix in product_name,
        # core flavor in base_flavor (plan §Region Safety)
        return _normalize_extracted_region(raw_items)

    except Exception as e:
        logger.warning("Thread extraction failed for %s: %s", gmail_thread_id, e)
        return None


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