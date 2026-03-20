"""
State Updater — Deterministic + LLM (feature-flagged)
-----------------------------------------------------

Maintains conversation_state JSON per Gmail thread.
Deterministic Python builder replaces GPT-5.2 LLM when USE_LLM_STATE_UPDATER != "true".

Feature flag (env var USE_LLM_STATE_UPDATER):
  "true"   — LLM path (original, default for safe rollout)
  "shadow" — Python primary + LLM for comparison logging
  "false"  — Pure Python, no LLM call
"""

import json
import logging
import os
import re
from copy import deepcopy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature flag — read at runtime, not at import
# ---------------------------------------------------------------------------
_VALID_MODES = {"true", "false", "shadow"}


def _use_llm() -> str:
    mode = os.environ.get("USE_LLM_STATE_UPDATER", "true").strip().lower()
    if mode not in _VALID_MODES:
        logger.warning("Invalid USE_LLM_STATE_UPDATER=%r, falling back to 'true'", mode)
        return "true"
    return mode


# ---------------------------------------------------------------------------
# Empty state template
# ---------------------------------------------------------------------------
def _empty_state() -> dict:
    """Return an empty state structure."""
    return {
        "status": "new",
        "topic": "general",
        "facts": {
            "order_id": None,
            "ordered_items": [],
            "oos_items": [],
            "offered_alternatives": [],
            "price": None,
            "final_price": None,
            "discount_applied": None,
            "payment_method": None,
            "payment_request_sent": None,
            "payment_confirmed": None,
            "shipped_at": None,
            "tracking_number": None,
        },
        "promises": [],
        "last_exchange": {
            "we_said": None,
            "they_said": None,
        },
        "open_questions": [],
        "summary": "",
    }


# ═══════════════════════════════════════════════════════════════════════════
# LLM path (kept for feature flag "true" and "shadow")
# ═══════════════════════════════════════════════════════════════════════════

# Lazy-init to avoid import errors when agno is not available (tests)
_llm_agent = None

state_updater_instructions = """\
You are a conversation state updater for shipmecarton.com email system.

Your job is to maintain a compact JSON state that tracks the conversation.

## Input

You receive:
1. CURRENT STATE (JSON) — the existing state, or {} for new threads
2. NEW MESSAGE — the email that just arrived (inbound or outbound)
3. CLASSIFICATION — situation type and metadata

## Output

Return ONLY a valid JSON object. No explanation, no markdown, no code fences.

## State Structure

{
  "status": "awaiting_payment" | "awaiting_oos_decision" | "shipped" | "delivered" | "resolved" | "pending_response" | "new",
  "topic": "new_order" | "tracking" | "payment" | "discount" | "shipping" | "general",
  "facts": {
    "order_id": "#12345" or null,
    "ordered_items": ["Green x2", "Silver x3"],
    "oos_items": ["Green"],
    "offered_alternatives": ["Turquoise from Armenia"],
    "price": "$220" or null,
    "final_price": "$209" or null,
    "discount_applied": "5%" or null,
    "payment_method": "Zelle" or null,
    "payment_request_sent": true/false or null,
    "payment_confirmed": true/false or null,
    "shipped_at": "2024-01-15" or null,
    "tracking_number": "9400111..." or null
  },
  "promises": ["delivery in 3-5 days", "ship today after payment"],
  "last_exchange": {
    "we_said": "Summary of our last message",
    "they_said": "Summary of their last message"
  },
  "open_questions": ["Which alternative do you prefer?"],
  "summary": "Returning client, new order with OOS. Client agreed to Turquoise alternative."
}

## Rules

1. PRESERVE all existing facts — never delete information, only add or update
2. UPDATE status based on conversation flow:
   - new → awaiting_payment (after we send payment info)
   - awaiting_payment → shipped (after payment confirmed)
   - awaiting_oos_decision → new (after client chooses alternative)
3. PRESERVE payment_request_sent and payment_confirmed flags — these are set by pipeline, do not modify
4. EXTRACT facts from emails:
   - Order IDs, prices, items from order notifications
   - Tracking numbers from shipping confirmations
   - Payment confirmations
5. TRACK promises we make — these are important for consistency
6. IDENTIFY open questions — things the customer asked that we haven't answered
7. Keep summary under 100 words — it's for quick context
8. If direction is "outbound", update "we_said" in last_exchange
9. If direction is "inbound", update "they_said" in last_exchange
10. Do NOT invent facts — if you don't know something, leave it null

## Example

Input state: {}
New message (inbound): "Order #12345 placed, $220, Green x2"
Classification: new_order

Output:
{
  "status": "new",
  "topic": "new_order",
  "facts": {
    "order_id": "#12345",
    "ordered_items": ["Green x2"],
    "oos_items": [],
    "offered_alternatives": [],
    "price": "$220",
    "final_price": null,
    "discount_applied": null,
    "payment_method": null,
    "shipped_at": null,
    "tracking_number": null
  },
  "promises": [],
  "last_exchange": {
    "we_said": null,
    "they_said": "Placed order #12345 for Green x2, total $220"
  },
  "open_questions": [],
  "summary": "New order #12345 for Green x2, $220. Awaiting our response."
}
"""


def _get_llm_agent():
    """Lazy-init LLM agent (avoids import errors in test environments)."""
    global _llm_agent
    if _llm_agent is None:
        from agno.agent import Agent
        from agno.models.openai import OpenAIResponses
        _llm_agent = Agent(
            id="state-updater",
            name="State Updater",
            model=OpenAIResponses(id="gpt-5.2"),
            instructions=state_updater_instructions,
            markdown=False,
        )
    return _llm_agent


def _run_llm_state_updater(
    current_state, email_text, situation, direction,
    client_email, order_id, price,
) -> dict:
    """Original LLM-based state updater."""
    current_json = json.dumps(current_state or {}, ensure_ascii=False, indent=2)
    email_preview = email_text[:2000] if len(email_text) > 2000 else email_text

    prompt = f"""CURRENT STATE:
{current_json}

NEW MESSAGE ({direction}):
{email_preview}

CLASSIFICATION:
- situation: {situation}
- client_email: {client_email or "unknown"}
- order_id: {order_id or "unknown"}
- price: {price or "unknown"}

Update the state JSON. Return ONLY the JSON object:"""

    try:
        agent = _get_llm_agent()
        response = agent.run(prompt)
        raw = response.content
        json_str = re.sub(r"^```json?\s*|\s*```$", "", raw.strip())
        updated_state = json.loads(json_str)
        logger.info(
            "LLM state updated: status=%s, topic=%s",
            updated_state.get("status"), updated_state.get("topic"),
        )
        return updated_state
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM state updater response: %s", e)
        return current_state or _empty_state()
    except Exception as e:
        logger.error("LLM state updater failed: %s", e, exc_info=True)
        return current_state or _empty_state()


# ═══════════════════════════════════════════════════════════════════════════
# Deterministic path — helpers
# ═══════════════════════════════════════════════════════════════════════════

_TOPIC_MAP = {
    "new_order": "new_order",
    "oos_followup": "new_order",
    "price_question": "new_order",
    "tracking": "tracking",
    "payment_received": "payment",
    "payment_question": "payment",
    "discount_request": "discount",
    "shipping_timeline": "shipping",
}


def _derive_topic(situation: str) -> str:
    return _TOPIC_MAP.get(situation, "general")


def _derive_status(situation: str, current_status: str | None) -> str:
    """Conservative: preserve current by default, set only for unambiguous cases."""
    if current_status and current_status != "new":
        return current_status
    # New thread or status=="new" — set based on situation
    if situation == "oos_followup":
        return "awaiting_oos_decision"
    return current_status or "new"


def _extract_body_preview(email_text: str, limit: int = 200) -> str | None:
    """Extract clean body preview for last_exchange."""
    marker = "Body:"
    idx = email_text.find(marker)
    body = email_text[idx + len(marker):].strip() if idx >= 0 else email_text.strip()
    if not body:
        return None
    try:
        from tools.email_parser import _strip_quoted_text
        cleaned = _strip_quoted_text(body)
    except (ImportError, AttributeError):
        # Fallback: simple > line removal
        lines = [l for l in body.split("\n") if not l.strip().startswith(">")]
        cleaned = "\n".join(lines).strip()
    return cleaned[:limit] if cleaned else body[:limit]


def _derive_last_exchange(current: dict, email_text: str, direction: str) -> dict:
    exchange = {
        "we_said": current.get("we_said") if isinstance(current, dict) else None,
        "they_said": current.get("they_said") if isinstance(current, dict) else None,
    }
    preview = _extract_body_preview(email_text)
    if direction == "outbound":
        exchange["we_said"] = preview
    else:
        exchange["they_said"] = preview
    return exchange


def _format_stock_items_to_labels(stock_items: list[dict], classification=None) -> list[str]:
    """Region-aware label builder. Replicates pipeline.py:257-281 exactly.

    Used by both _format_ordered_items (Phase 1) and _enrich_state_after_routing (Phase 2).
    """
    from db.catalog import get_display_name, _enrich_display_name_with_region
    from db.stock import extract_variant_id

    client_email = getattr(classification, "client_email", None)
    labels = []
    for item in stock_items:
        display = item.get("display_name")
        if not display:
            cat = ""
            entries = item.get("stock_entries") or []
            if entries:
                cat = entries[0].get("category", "")
            name = item.get("product_name") or item.get("base_flavor", "")
            display = get_display_name(name, cat)
        product_ids = item.get("product_ids")
        if product_ids:
            vid = extract_variant_id(product_ids, client_email=client_email)
            if vid:
                display = _enrich_display_name_with_region(vid, display)
        qty = item.get("ordered_qty") or item.get("quantity", 1)
        labels.append(f"{display} x{qty}" if qty > 1 else display)
    return labels


def _format_ordered_items(classification, result) -> list[str] | None:
    """Format ordered items as region-aware labels."""
    stock_items = (result or {}).get("_stock_check_items") or []
    if stock_items:
        labels = _format_stock_items_to_labels(stock_items, classification)
        if labels:
            return labels

    order_items = getattr(classification, "order_items", None) or []
    if not order_items:
        return None
    labels = []
    for oi in order_items:
        name = getattr(oi, "product_name", None) or getattr(oi, "base_flavor", "")
        qty = getattr(oi, "quantity", 1)
        labels.append(f"{name} x{qty}" if qty and qty > 1 else name)
    return labels or None


def _build_order_items_dicts(classification, result) -> list[dict] | None:
    """Build facts.order_items as list[dict] for stock_question/price_question fallback."""
    stock_items = (result or {}).get("_stock_check_items") or []
    if stock_items:
        return [
            {
                "base_flavor": item.get("base_flavor", ""),
                "product_name": item.get("product_name", ""),
                "quantity": item.get("ordered_qty") or item.get("quantity", 1),
            }
            for item in stock_items if item.get("base_flavor") or item.get("product_name")
        ] or None

    order_items = getattr(classification, "order_items", None) or []
    if not order_items:
        return None
    return [
        {
            "base_flavor": getattr(oi, "base_flavor", ""),
            "product_name": getattr(oi, "product_name", ""),
            "quantity": getattr(oi, "quantity", 1),
        }
        for oi in order_items
    ] or None


def _derive_facts(current_facts: dict, classification, order_id, price, result) -> dict:
    """Merge new facts into current, preserving existing values."""
    facts = deepcopy(current_facts) if current_facts else {}

    # order_id and price from classification (if not None)
    if order_id:
        facts["order_id"] = order_id
    elif classification and getattr(classification, "order_id", None):
        facts["order_id"] = classification.order_id
    facts.setdefault("order_id", None)

    if price:
        facts["price"] = price
    elif classification and getattr(classification, "price", None):
        facts["price"] = classification.price
    facts.setdefault("price", None)

    # ordered_items from resolved stock check or classifier
    new_ordered = _format_ordered_items(classification, result)
    if new_ordered:
        facts["ordered_items"] = new_ordered
    facts.setdefault("ordered_items", [])

    # order_items as dicts
    new_order_items = _build_order_items_dicts(classification, result)
    if new_order_items:
        facts["order_items"] = new_order_items
    facts.setdefault("order_items", [])

    # Preserve all other fields from current — never overwrite with None
    for key in (
        "oos_items", "offered_alternatives", "final_price", "discount_applied",
        "payment_method", "payment_request_sent", "payment_confirmed",
        "shipped_at", "tracking_number",
        "confirmed_order_items", "pending_order_items",
        "pending_oos_resolution",  # CRITICAL: always preserve
    ):
        facts.setdefault(key, current_facts.get(key) if current_facts else None)

    return facts


def _derive_summary(facts: dict, situation: str | None = None) -> str:
    """Deterministic summary from facts."""
    parts = []
    if situation:
        parts.append(situation.replace("_", " "))
    if facts.get("order_id"):
        parts.append(f"order {facts['order_id']}")
    if facts.get("ordered_items"):
        parts.append(", ".join(facts["ordered_items"][:3]))
    if facts.get("oos_items"):
        parts.append(f"OOS: {', '.join(str(i) for i in facts['oos_items'][:2])}")
    if facts.get("offered_alternatives"):
        parts.append(f"alts: {', '.join(str(a) for a in facts['offered_alternatives'][:2])}")
    if facts.get("pending_oos_resolution"):
        parts.append("pending OOS resolution")
    if facts.get("tracking_number"):
        parts.append(f"tracking {facts['tracking_number']}")
    return "; ".join(parts)


def _build_deterministic_state(
    current_state, email_text, situation, direction,
    order_id, price, classification, result,
) -> dict:
    """Build conversation state deterministically (0 LLM tokens)."""
    state = deepcopy(current_state) if current_state else _empty_state()
    state["topic"] = _derive_topic(situation)
    state["status"] = _derive_status(situation, state.get("status"))
    state["facts"] = _derive_facts(
        state.get("facts", {}), classification, order_id, price, result,
    )
    state["last_exchange"] = _derive_last_exchange(
        state.get("last_exchange", {}), email_text, direction,
    )
    state["summary"] = _derive_summary(state["facts"], situation)
    # Preserve promises and open_questions from current — never overwrite
    state.setdefault("promises", [])
    state.setdefault("open_questions", [])
    return state


# ═══════════════════════════════════════════════════════════════════════════
# Shadow comparison
# ═══════════════════════════════════════════════════════════════════════════

def _log_state_diff(deterministic: dict, llm_result: dict, client_email: str | None) -> None:
    """Compare deterministic vs LLM state and log diffs."""
    diffs = []

    for key in ("status", "topic"):
        d_val = deterministic.get(key)
        l_val = llm_result.get(key)
        if d_val != l_val:
            diffs.append(f"{key}: det={d_val} vs llm={l_val}")

    d_facts = deterministic.get("facts", {})
    l_facts = llm_result.get("facts", {})

    if d_facts.get("order_id") != l_facts.get("order_id"):
        diffs.append(f"facts.order_id: det={d_facts.get('order_id')} vs llm={l_facts.get('order_id')}")

    for key in ("ordered_items", "oos_items"):
        d_set = set(d_facts.get(key) or [])
        l_set = set(l_facts.get(key) or [])
        if d_set != l_set:
            diffs.append(f"facts.{key}: det={sorted(d_set)} vs llm={sorted(l_set)}")

    # Critical: pending_oos_resolution presence
    d_has = bool(d_facts.get("pending_oos_resolution"))
    l_has = bool(l_facts.get("pending_oos_resolution"))
    if d_has != l_has:
        diffs.append(f"facts.pending_oos_resolution: det={'present' if d_has else 'MISSING'} vs llm={'present' if l_has else 'MISSING'}")

    if diffs:
        logger.warning(
            "State diff (client=%s): %s",
            client_email or "unknown", " | ".join(diffs),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 enrichment (called from pipeline.py after routing, before checker)
# ═══════════════════════════════════════════════════════════════════════════

def _enrich_state_after_routing(state: dict, result: dict, classification) -> None:
    """Phase 2: fill facts from stock check + routing result. Called BEFORE checker."""
    if not state:
        return
    facts = state.setdefault("facts", {})
    stock_issue = result.get("stock_issue")

    if stock_issue:
        stock_check = stock_issue.get("stock_check", {})
        facts["oos_items"] = [
            i.get("product_name", i.get("base_flavor", ""))
            for i in stock_check.get("insufficient_items", [])
        ]

        offered = []
        for alt_data in stock_issue.get("best_alternatives", {}).values():
            for a in alt_data.get("alternatives", []):
                pn = a.get("alternative", {}).get("product_name", "")
                cat = a.get("alternative", {}).get("category", "")
                try:
                    from db.region_family import CATEGORY_REGION_SUFFIX
                    region = CATEGORY_REGION_SUFFIX.get(cat, "")
                except ImportError:
                    region = ""
                display = f"{pn} {region}".strip() if region else pn
                if display and display not in offered:
                    offered.append(display)
        facts["offered_alternatives"] = offered

    if result.get("calculated_price") is not None:
        facts["final_price"] = f"${result['calculated_price']:.2f}"

    # pending_order_items for new_order with OOS
    if stock_issue and getattr(classification, "situation", None) == "new_order":
        stock_check = stock_issue.get("stock_check", {})
        facts["pending_order_items"] = [
            {
                "base_flavor": i.get("base_flavor", ""),
                "product_name": i.get("product_name", ""),
                "quantity": i.get("ordered_qty", 1),
            }
            for i in stock_check.get("items", [])
        ]

    # After successful OOS resolve: update items, clear stale OOS context
    if result.get("effective_situation") == "new_order":
        canonical = result.get("canonical_confirmed_items") or []
        stock_check_items = result.get("_stock_check_items") or []
        if canonical:
            confirmed = [
                {
                    "base_flavor": item.get("base_flavor", ""),
                    "product_name": item.get("product_name", ""),
                    "quantity": item.get("ordered_qty", item.get("quantity", 1)),
                }
                for item in canonical
            ]
            facts["confirmed_order_items"] = confirmed
            facts["order_items"] = confirmed
            # Region-aware labels via shared helper
            if stock_check_items:
                facts["ordered_items"] = _format_stock_items_to_labels(
                    stock_check_items, classification,
                )
            else:
                facts["ordered_items"] = [
                    f"{c['product_name'] or c['base_flavor']} x{c['quantity']}"
                    for c in confirmed
                ]
            # Clear stale OOS context
            facts["oos_items"] = []
            facts["offered_alternatives"] = []
            facts["pending_order_items"] = []

    # Recalculate summary after enrichment
    state["summary"] = _derive_summary(
        facts, getattr(classification, "situation", None),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def update_conversation_state(
    current_state: dict | None,
    email_text: str,
    situation: str,
    direction: str,
    client_email: str | None = None,
    order_id: str | None = None,
    price: str | None = None,
    classification=None,
    result: dict | None = None,
) -> dict:
    """Update conversation state after a new message.

    Feature-flagged: deterministic Python or LLM (GPT-5.2).
    """
    mode = _use_llm()

    if mode == "false":
        try:
            state = _build_deterministic_state(
                current_state, email_text, situation, direction,
                order_id, price, classification, result,
            )
        except Exception as e:
            logger.error("Deterministic state builder failed: %s", e, exc_info=True)
            return current_state or _empty_state()
        logger.info(
            "Deterministic state: topic=%s, status=%s, client=%s",
            state.get("topic"), state.get("status"), client_email,
        )
        return state

    elif mode == "shadow":
        try:
            deterministic = _build_deterministic_state(
                current_state, email_text, situation, direction,
                order_id, price, classification, result,
            )
        except Exception as e:
            logger.error("Deterministic state builder failed in shadow mode: %s", e, exc_info=True)
            deterministic = current_state or _empty_state()
        try:
            llm_result = _run_llm_state_updater(
                current_state, email_text, situation, direction,
                client_email, order_id, price,
            )
            _log_state_diff(deterministic, llm_result, client_email)
        except Exception as e:
            logger.warning("Shadow LLM state updater failed: %s", e)
        logger.info(
            "Shadow state (deterministic primary): topic=%s, status=%s, client=%s",
            deterministic.get("topic"), deterministic.get("status"), client_email,
        )
        return deterministic

    else:  # "true" or unknown — original LLM path (safe default)
        return _run_llm_state_updater(
            current_state, email_text, situation, direction,
            client_email, order_id, price,
        )
