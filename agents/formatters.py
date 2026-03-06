"""
Email formatting utilities.

Three pure functions — no external dependencies.
"""


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
