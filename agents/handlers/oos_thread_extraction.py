"""OOS Thread Extraction
------------------------

Extracts agreed order items from Gmail thread history using a direct
OpenAI API call (not via Agno). Isolated here to keep the heavy openai
import lazy and avoid breaking test stubs that can't import the real library.
"""

import json
import logging

from db.memory import get_full_thread_history
from agents.handlers.oos_text_utils import _normalize_extracted_region

logger = logging.getLogger(__name__)


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
    import openai as _openai  # lazy — required: tests stub this module without real openai

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
