"""
Email formatting utilities.

Pure functions — no external dependencies (no DB, no Gmail, no agno).
Used by both production code and eval runner.
"""

import json


def format_email_history(history: list[dict]) -> str:
    """Format email history for inclusion in the fallback LLM prompt."""
    if not history:
        return ""

    lines = ["=== CONVERSATION HISTORY ===", ""]
    for msg in history:
        ts = msg["created_at"].strftime("%Y-%m-%d") if msg.get("created_at") else "unknown"
        if msg["direction"] == "inbound":
            prefix = f"[CLIENT WROTE] {ts} | {msg.get('subject', '')}"
        else:
            prefix = f"[WE SENT] {ts} | {msg.get('subject', '')}"

        body = msg.get("body", "")
        if len(body) > 300:
            body = body[:300] + "..."

        lines.append(prefix)
        lines.append(body)
        lines.append("---")

    return "\n".join(lines)


def format_thread_for_classifier(history: list[dict]) -> str:
    """Format thread history for the Classifier prompt — full body, no truncation.

    Classifier accuracy depends on seeing the complete conversation context.
    """
    if not history:
        return ""

    lines = ["--- THREAD HISTORY ---"]
    for msg in history:
        ts = msg["created_at"].strftime("%Y-%m-%d") if msg.get("created_at") else "unknown"
        if msg["direction"] == "inbound":
            prefix = f"[CLIENT] {ts} | {msg.get('subject', '')}"
        else:
            prefix = f"[WE SENT] {ts} | {msg.get('subject', '')}"

        lines.append(prefix)
        lines.append(msg.get("body", ""))
        lines.append("---")

    return "\n".join(lines)


def format_conversation_state_for_classifier(state: dict) -> str:
    """Format conversation state dict for the classifier prompt.

    Used by build_classifier_context() and eval runner — single source of truth.
    """
    return (
        f"--- CONVERSATION STATE ---\n"
        f"Status: {state.get('status', 'unknown')}\n"
        f"Topic: {state.get('topic', 'unknown')}\n"
        f"Facts: {json.dumps(state.get('facts', {}), ensure_ascii=False)}\n"
        f"Open questions: {state.get('open_questions', [])}\n"
        f"Summary: {state.get('summary', '')}\n\n"
    )


def format_combined_email_text(candidates: list[dict]) -> str:
    """Combine multiple same-thread messages into one text with dates.

    Moved from tools/gmail_poller.py to avoid googleapiclient import dependency.
    Messages are expected to be sorted chronologically (oldest first).
    Each candidate is a dict with 'msg' (message dict) and 'created_at' (datetime).
    """
    newest = candidates[-1]["msg"]
    from_addr = newest.get("from_raw", newest.get("from", ""))
    parts = [
        f"From: {from_addr}",
        f"Subject: {newest.get('subject', '')}",
        f"Body: [{len(candidates)} messages from this client in the same thread]\n",
    ]

    for c in candidates:
        ts = c["created_at"]
        date_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown date"
        parts.append(f"--- Message from {date_str} ---")
        parts.append(c["msg"].get("body", ""))
        parts.append("")

    return "\n".join(parts)


def format_result(result: dict) -> str:
    """Format the processing result for display."""
    lines = []
    lines.append("=" * 50)
    lines.append("CLASSIFICATION")
    lines.append("=" * 50)
    lines.append(f"Needs Reply: {result['needs_reply']}")
    lines.append(f"Situation: {result['situation']}")
    lines.append(f"Client Email: {result['client_email']}")
    lines.append(f"Client Name: {result['client_name']}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("CLIENT DATA")
    lines.append("=" * 50)
    if result["client_found"]:
        c = result["client_data"]
        lines.append(f"Status: FOUND")
        lines.append(f"Payment Type: {c['payment_type']}")
        if c.get("zelle_address"):
            lines.append(f"Zelle: {c['zelle_address']}")
        d = c.get("discount_percent", 0)
        dl = c.get("discount_orders_left", 0)
        if d > 0 and dl > 0:
            lines.append(f"Discount: {d}% ({dl} orders left)")
        else:
            lines.append("Discount: none")
    else:
        lines.append("Status: NEW CLIENT (not in database)")
    lines.append("")

    # Stock check section (if applicable)
    if result.get("stock_issue"):
        lines.append("=" * 50)
        lines.append("STOCK CHECK")
        lines.append("=" * 50)
        stock_check = result["stock_issue"]["stock_check"]
        for item in stock_check["items"]:
            status = "OK" if item["is_sufficient"] else "INSUFFICIENT"
            shortage = ""
            if not item["is_sufficient"] and item["total_available"] > 0:
                shortage = " [PARTIAL]"
            lines.append(
                f"{item['base_flavor']}: ordered {item['ordered_qty']}, "
                f"available {item['total_available']} [{status}]{shortage}"
            )

        # Show alternative decision for each OOS flavor
        best_alts = result["stock_issue"].get("best_alternatives", {})
        if best_alts:
            lines.append("")
            lines.append("ALTERNATIVE DECISION:")
            for flavor, decision in best_alts.items():
                alts = decision.get("alternatives", [])
                if not alts:
                    lines.append(f"  {flavor} → no alternative available")
                    continue

                rendered = []
                for opt in alts:
                    alt = opt["alternative"]
                    reason = opt.get("reason", "fallback")
                    reason_text = reason
                    if reason == "history" and opt.get("order_count"):
                        reason_text = f"history ({opt['order_count']}x ordered before)"
                    elif reason == "llm":
                        reason_text = "AI pick"
                    rendered.append(
                        f"{alt['category']} / {alt['product_name']} (qty: {alt['quantity']}) [{reason_text}]"
                    )
                lines.append(f"  {flavor} → " + " | ".join(rendered))
        lines.append("")

    # Fulfillment section
    if result.get("fulfillment"):
        lines.append("=" * 50)
        lines.append("FULFILLMENT")
        lines.append("=" * 50)
        ff = result["fulfillment"]
        lines.append(f"Status: {ff['status']}")

        if ff.get("warehouse"):
            lines.append(f"Warehouse: {ff['warehouse']}")

        if ff["status"] == "updated":
            ur = ff.get("update_result", {})
            lines.append(f"Updated rows: {ur.get('updated', 0)}")
            for detail in ur.get("details", []):
                if "old_maks" in detail:
                    lines.append(
                        f"- {detail['product_name']}: "
                        f"{detail['old_maks']} -> {detail['new_maks']}"
                    )
        elif ff["status"] == "skipped_split":
            lines.append("Reason: no single warehouse can fulfill all items")
            lines.append("maks_sales was NOT updated")
            breakdown = ff.get("split_breakdown")
            if breakdown:
                lines.append("")
                lines.append("Split breakdown:")
                for bd_item in breakdown:
                    lines.append(f"  {bd_item['base_flavor']} (need {bd_item['ordered_qty']}):")
                    for wh, available in bd_item["availability"].items():
                        if available >= bd_item["ordered_qty"]:
                            tag = "OK"
                        elif available > 0:
                            tag = "PARTIAL"
                        else:
                            tag = "--"
                        lines.append(f"    {wh}: {available} [{tag}]")
        elif ff["status"] == "blocked_ambiguous_variant":
            reason = ff.get("reason", "ambiguous_variant")
            reason_labels = {
                "ambiguous_variant": "ambiguous variant mapping",
                "unresolved_variant_strict": "unresolved variant (strict mode)",
                "missing_order_id_new_order_postpay": "missing order_id for new_order_postpay",
            }
            lines.append(f"Reason: {reason_labels.get(reason, reason)}")
            lines.append("maks_sales was NOT updated")
            ambiguous = ff.get("ambiguous_flavors", [])
            if ambiguous:
                lines.append(f"Affected: {', '.join(ambiguous)}")
        elif ff["status"] == "skipped_duplicate":
            lines.append("Reason: already processed (duplicate)")
        elif ff["status"] == "skipped_unresolved_order":
            lines.append("Reason: no resolved order items found")
        elif ff["status"] == "error":
            lines.append(f"Error: {ff.get('error', 'unknown')}")
            ur = ff.get("update_result", {})
            for err in ur.get("errors", []):
                lines.append(f"  - {err}")

        if ff.get("tried_warehouses"):
            lines.append(f"Tried: {', '.join(ff['tried_warehouses'])}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("DRAFT REPLY")
    lines.append("=" * 50)
    if result["template_used"]:
        lines.append("[Template - exact copy]")
        lines.append("")
        lines.append(result["draft_reply"])
    elif result.get("needs_routing"):
        lines.append("[Router will generate reply]")
    else:
        lines.append(result["draft_reply"])

    return "\n".join(lines)
