"""
Reply Templates & OOS Template Helpers
---------------------------------------

Static reply templates and OOS email builder for the Email Agent.
"""


# ---------------------------------------------------------------------------
# Reply Templates (hardcoded — never change)
# Key format: (situation, payment_type)
# ---------------------------------------------------------------------------
REPLY_TEMPLATES = {
    ("new_order", "prepay"): (
        "Thank you so much for placing an order\n"
        "Your total is {PRICE} - {DISCOUNT}% = {FINAL_PRICE} FREE shipping\n"
        "\n"
        "!!! Zelle ( In memo or comments don't put anything please ! ) use email below\n"
        "\n"
        "{ZELLE_ADDRESS}\n"
        "\n"
        "If paid today, We will ship your order Tonight from USA\n"
        "Your order will be delivered in 2-4 days max.\n"
        "Thank you!"
    ),
    ("payment_received", "prepay"): (
        "Thank you very much!\n"
        "We received your payment.\n"
        "We will ship your order TODAY!\n"
        "Tracking with USPS will be updated on the USPS website "
        "till midnight on the day of the shipping\n"
        "{TRACKING_URL}\n"
        "\n"
        "{CUSTOMER_NAME}\n"
        "{CUSTOMER_STREET}\n"
        "{CUSTOMER_CITY_STATE_ZIP}"
    ),
    ("new_order", "postpay"): (
        "Hello!\n"
        "Thank you very much for placing an order\n"
        "We will ship your package ASAP\n"
        "Total is {PRICE} - {DISCOUNT}% = {FINAL_PRICE} FREE shipping applied\n"
        "Pay when received as always via Zelle or Cash App\n"
        "ZELLE IS OUR PREFERRED METHOD OF PAYMENT\n"
        "When order is received and you are ready to pay "
        "( In memo or comments don't put anything please ! )\n"
        "\n"
        "Here is your confirmation.\n"
        "Tracking With USPS will be updated on the USPS website "
        "till midnight on the day of the shipping\n"
        "{TRACKING_URL}\n"
        "\n"
        "{CUSTOMER_NAME}\n"
        "{CUSTOMER_STREET}\n"
        "{CUSTOMER_CITY_STATE_ZIP}"
    ),
    # OOS Followup — customer agrees to alternative (prepay)
    ("oos_agrees", "prepay"): (
        "Got it!\n"
        "We will update your order with the alternative.\n"
        "\n"
        "!!! Zelle ( In memo or comments don't put anything please ! ) use email below\n"
        "\n"
        "{ZELLE_ADDRESS}\n"
        "\n"
        "Once we receive your payment, we will ship your order the same day.\n"
        "Thank you!"
    ),
    # OOS Followup — customer agrees to alternative (postpay)
    ("oos_agrees", "postpay"): (
        "Got it!\n"
        "We will update your order and ship your package ASAP.\n"
        "Pay when received as always via Zelle or Cash App\n"
        "ZELLE IS OUR PREFERRED METHOD OF PAYMENT\n"
        "When order is received and you are ready to pay "
        "( In memo or comments don't put anything please ! )\n"
        "\n"
        "Tracking With USPS will be updated on the USPS website "
        "till midnight on the day of the shipping\n"
        "Thank you!"
    ),
    # OOS Followup — customer declines alternative
    ("oos_declines", "any"): (
        "No problem at all!\n"
        "If you change your mind or would like us to find something else for you,\n"
        "just let us know!\n"
        "Thank you!"
    ),
    # ------------------------------------------------------------------
    # Tracking (replaces LLM handler — used only when shipment confirmed)
    # ------------------------------------------------------------------
    ("tracking", "any"): (
        "Hi ! How is your day going ?\n"
        "We shipped your order 100% . USPS website takes 2 hours - couple of days "
        "to update package in their system\n"
        "If nothing changed by {RECHECK_DATE} We will ship you exactly new order .\n"
        "We think everything will be alright .\n"
        "Feel free to ask any questions .\n"
        "Thank you!"
    ),
    # ------------------------------------------------------------------
    # Payment question (replaces LLM handler)
    # ------------------------------------------------------------------
    ("payment_question", "prepay"): (
        "Hi!\n"
        "!!! Zelle ( In memo or comments don't put anything please ! ) use email below\n"
        "\n"
        "{ZELLE_ADDRESS}\n"
        "\n"
        "PS. if it asks for name , you can put any name\n"
        "Thank you!"
    ),
    ("payment_question", "postpay"): (
        "Hi!\n"
        "Pay when received as always via Zelle or Cash App\n"
        "ZELLE IS OUR PREFERRED METHOD OF PAYMENT\n"
        "When order is received and you are ready to pay "
        "( In memo or comments don't put anything please ! )\n"
        "\n"
        "{ZELLE_ADDRESS}\n"
        "\n"
        "Thank you!"
    ),
    # ------------------------------------------------------------------
    # Discount request (replaces LLM handler)
    # ------------------------------------------------------------------
    ("discount_request", "has_discount"): (
        "Hi!\n"
        "Great news — you have a {DISCOUNT}% discount applied for your next "
        "{DISCOUNT_ORDERS_LEFT} order(s)!\n"
        "It will be automatically applied to your next order.\n"
        "Thank you!"
    ),
    ("discount_request", "no_discount"): (
        "Hi!\n"
        "Thank you for being our customer!\n"
        "Unfortunately, we don't have any active discounts at the moment.\n"
        "We do occasionally run promotions, so keep an eye out!\n"
        "Thank you!"
    ),
    # ------------------------------------------------------------------
    # Shipping timeline (replaces LLM handler)
    # ------------------------------------------------------------------
    ("shipping_timeline", "prepay"): (
        "Hi!\n"
        "We ship via USPS from USA.\n"
        "Once we receive your payment, we will ship your order "
        "the same day (if before 3 PM EST) or next business day.\n"
        "Delivery takes 2-4 business days max.\n"
        "FREE shipping on all orders!\n"
        "Thank you!"
    ),
    ("shipping_timeline", "postpay"): (
        "Hi!\n"
        "We ship via USPS from USA.\n"
        "Your order will be shipped ASAP!\n"
        "Delivery takes 2-4 business days max.\n"
        "FREE shipping on all orders!\n"
        "Thank you!"
    ),
}

# ---------------------------------------------------------------------------
# Out-of-Stock Template (STABLE — Python fills variables, no LLM)
# ---------------------------------------------------------------------------
def _format_alternative(alt_entry: dict) -> str:
    """Format a single alternative for customer-facing display.

    Args:
        alt_entry: Dict with keys: alternative (stock item dict), reason, order_count

    Returns:
        Formatted string like "Terea Amber ME (same product, different region)"
    """
    from db.catalog import get_display_name

    alt = alt_entry["alternative"]
    reason = alt_entry.get("reason", "fallback")
    raw_name = alt["product_name"]
    category = alt.get("category", "")

    formatted = get_display_name(raw_name, category)

    if reason == "same_flavor":
        formatted += " (same product, different region)"
    elif reason == "history":
        formatted += " (you've ordered before)"

    return formatted


def _build_formatter_input(
    insufficient_items: list[dict],
    best_alternatives: dict,
) -> tuple[list[dict], int, str]:
    """Convert pipeline data to LLM formatter input format.

    Returns:
        (formatter_items, total_oos_count, format_mode)
        - formatter_items: only items WITH alternatives
        - total_oos_count: all OOS items (including without alternatives)
        - format_mode: single source of truth for formatting rules
    """
    from db.catalog import get_display_name, get_base_display_name

    total_oos_count = len(insufficient_items)

    # Step 1: filter to items with alternatives
    items_with_alts = []
    for item in insufficient_items:
        flavor = item["base_flavor"]
        decision = best_alternatives.get(flavor, {})
        alts = decision.get("alternatives", [])
        if alts:
            items_with_alts.append((item, alts))

    if not items_with_alts:
        return [], total_oos_count, "single_item"  # mode irrelevant when empty

    # Step 2: determine format_mode
    n = len(items_with_alts)
    same_flavor_count = sum(
        1 for _, alts in items_with_alts
        if alts[0]["reason"] == "same_flavor"
    )
    other_count = n - same_flavor_count

    if n == 1 and total_oos_count == 1:
        format_mode = "single_item"
    elif n == 1 and total_oos_count > 1:
        format_mode = "per_item_mapping"
    elif n > 1 and other_count == 0:
        format_mode = "all_same_flavor_grouped"
    elif n > 1 and same_flavor_count == 0:
        format_mode = "per_item_mapping"
    else:
        format_mode = "hybrid_mixed"

    # Step 3: build formatter_items using format_mode
    max_alts = 3 if format_mode == "single_item" else 1

    formatter_items = []
    for item, alts in items_with_alts:
        display = item.get("display_name") or get_base_display_name(item["base_flavor"])
        ordered_qty = item.get("ordered_qty", 1)
        total_available = item.get("total_available", 0)
        missing_qty = ordered_qty - total_available

        alt_list = []
        for a in alts[:max_alts]:
            alt_item = a["alternative"]
            alt_display = get_display_name(
                alt_item["product_name"], alt_item.get("category", ""),
            )
            alt_list.append({
                "display_name": alt_display,
                "reason": a.get("reason", "fallback"),
            })

        formatter_items.append({
            "display_name": display,
            "ordered_qty": ordered_qty,
            "total_available": total_available,
            "missing_qty": missing_qty,
            "alternatives": alt_list,
        })

    return formatter_items, total_oos_count, format_mode


def _fallback_format_alternatives(
    insufficient_items: list[dict],
    best_alternatives: dict,
) -> str:
    """Format alternatives using the old verbose template (fallback).

    Returns the "1. We have ..." section text in the current format.
    Used when LLM formatter or validator fails.
    """
    from db.catalog import get_base_display_name

    def _display(item: dict) -> str:
        return item.get("display_name") or get_base_display_name(item["base_flavor"])

    alt_lines = []
    has_alternatives = False

    for item in insufficient_items:
        flavor = item["base_flavor"]
        decision = best_alternatives.get(flavor, {})
        alts = decision.get("alternatives", [])

        if alts:
            has_alternatives = True
            formatted_alts = [_format_alternative(a) for a in alts[:3]]
            alt_text = ", ".join(formatted_alts)

            if len(insufficient_items) == 1:
                missing = item.get("ordered_qty", 1) - item.get("total_available", 0)
                if item.get("total_available", 0) > 0 and missing > 0:
                    alt_lines.append(f"For the missing {missing}: {alt_text}")
                else:
                    alt_lines.append(alt_text)
            else:
                alt_lines.append(f"For {_display(item)}: {alt_text}")

    if not has_alternatives:
        return "1. Check our website for substitutions and ready to ship sticks."

    if len(alt_lines) == 1:
        if alt_lines[0].startswith("For the missing"):
            result = f"1. {alt_lines[0]}"
        else:
            result = f"1. We have {alt_lines[0]}"
    else:
        result = "1. We have alternatives:"
        for alt_line in alt_lines:
            result += f"\n   {alt_line}"

    result += "\n2. Check our website for substitutions and ready to ship sticks."
    return result


def fill_out_of_stock_template(
    insufficient_items: list[dict],
    best_alternatives: dict,
) -> str:
    """Fill the Out-of-Stock template with actual data.

    Uses a hybrid approach: Python template frame + LLM formatter for
    the alternatives line. Falls back to verbose format on LLM failure.

    Handles all situations:
    - Full OOS (qty = 0)
    - Partial OOS (qty > 0 but < ordered)
    - Mixed (some full, some partial)
    - No alternatives available

    Args:
        insufficient_items: List of items with insufficient stock, each has:
            - base_flavor, ordered_qty, total_available, product_name
        best_alternatives: Dict mapping base_flavor -> {alternatives: [...], reason, ...}

    Returns:
        Complete email reply text, ready to send.
    """
    import logging
    _logger = logging.getLogger(__name__)

    # Step 1: Build problem_text (unchanged from original)
    full_oos = []
    partial_oos = []

    for item in insufficient_items:
        if item["total_available"] == 0:
            full_oos.append(item)
        else:
            partial_oos.append(item)

    from db.catalog import get_base_display_name

    def _display(item: dict) -> str:
        return item.get("display_name") or get_base_display_name(item["base_flavor"])

    problem_parts = []

    if full_oos:
        if len(full_oos) == 1:
            problem_parts.append(f"we just ran out of {_display(full_oos[0])}")
        else:
            flavors = ", ".join([_display(i) for i in full_oos[:-1]])
            flavors += f" and {_display(full_oos[-1])}"
            problem_parts.append(f"we just ran out of {flavors}")

    if partial_oos:
        for p in partial_oos:
            problem_parts.append(
                f"we only have {p['total_available']} {_display(p)} available "
                f"(you ordered {p['ordered_qty']})"
            )

    if len(problem_parts) == 1:
        problem_text = problem_parts[0]
    elif len(problem_parts) == 2:
        problem_text = f"{problem_parts[0]}, and {problem_parts[1]}"
    else:
        problem_text = ", ".join(problem_parts[:-1]) + f", and {problem_parts[-1]}"

    # Step 2: Build formatter input and determine format_mode
    formatter_items, total_oos_count, format_mode = _build_formatter_input(
        insufficient_items, best_alternatives,
    )

    # Step 3: Get alternatives line (LLM → validate → fallback)
    if not formatter_items:
        # No alternatives for any item → website-only
        alternatives_section = (
            "1. Check our website for substitutions and ready to ship sticks."
        )
    else:
        from agents.oos_formatter import format_alternatives_line

        raw = format_alternatives_line(formatter_items, format_mode, total_oos_count)
        if raw is None:
            _logger.info("OOS formatter returned None, using fallback")
            alternatives_section = _fallback_format_alternatives(
                insufficient_items, best_alternatives,
            )
        else:
            alternatives_section = f"1. {raw}"
            alternatives_section += (
                "\n2. Check our website for substitutions and ready to ship sticks."
            )

    # Step 4: Assemble final email
    lines = [
        "Hi!",
        "How are you?",
        f"Unfortunately, {problem_text}",
        "",
        "What can we offer? Please choose one of the options below.",
        alternatives_section,
        "",
        "Link for the sticks substitution",
        "https://shipmecarton.com",
        "",
        "Please let us know what you think",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mixed Availability Template (Phase B — decision_required, no fulfillment)
# ---------------------------------------------------------------------------
def fill_mixed_availability_template(
    reservable_items: list[dict],
    unresolved_items: list[dict],
    alternatives_by_flavor: dict,
    reservable_price: float | None = None,
    client_data: dict | None = None,
) -> str:
    """Build decision-required email when some items are in stock and some OOS.

    NOT a fulfillment confirmation. Asks the customer to choose:
    A) ship reservable items only, or B) add a substitute.
    Zero LLM tokens.
    """
    from db.catalog import get_base_display_name

    def _display(item: dict) -> str:
        return item.get("display_name") or get_base_display_name(
            item["base_flavor"],
        )

    # Reserved items list
    reserved_lines = []
    for item in reservable_items:
        reserved_lines.append(
            f"\u2022 {item['ordered_qty']} x {_display(item)}"
        )

    # OOS items + alternatives
    oos_parts = []
    for item in unresolved_items:
        flavor = item["base_flavor"]
        decision = alternatives_by_flavor.get(flavor, {})
        alts = decision.get("alternatives", [])
        display = _display(item)

        if alts:
            alt_names = [_format_alternative(a) for a in alts[:2]]
            oos_parts.append(
                f"{display} is out of stock.\n"
                f"We have: {', '.join(alt_names)}"
            )
        else:
            oos_parts.append(f"{display} is out of stock.")

    # Build A/B choice
    price_a = f"${reservable_price:.2f}" if reservable_price else "TBD"

    reserved_names = ", ".join(
        _display(i) for i in reservable_items
    )

    lines = [
        "Hi!",
        "We have reserved for you:",
    ]
    lines.extend(reserved_lines)
    lines.append("")

    for part in oos_parts:
        lines.append(f"Unfortunately, {part}")
    lines.append("")

    lines.append("Would you like us to:")
    lines.append(f"A) Ship {reserved_names} only ({price_a})")
    lines.append("B) Add substitute and ship both")
    lines.append("")
    lines.append("Please let us know!")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase C: Optional OOS helpers
# ---------------------------------------------------------------------------

def _build_optional_oos_ps(optional_with_alts: list[dict]) -> str:
    """Build P.S. note for optional items that are OOS.

    Args:
        optional_with_alts: List of {"item": stock_item_dict, "best_alternative": alt_entry|None}
    """
    from db.catalog import get_base_display_name

    parts = []
    for entry in optional_with_alts:
        item = entry["item"]
        display = item.get("display_name") or get_base_display_name(
            item["base_flavor"],
        )
        alt = entry.get("best_alternative")
        if alt:
            alt_name = _format_alternative(alt)
            parts.append(
                f"P.S. {display} is out of stock right now.\n"
                f"We have {alt_name}\n"
                f"\u2014 let us know if you'd like to add it!"
            )
        else:
            parts.append(
                f"P.S. {display} is out of stock right now "
                f"\u2014 let us know if you'd like a substitute!"
            )
    return "\n".join(parts)


def fill_optional_oos_only_template(
    optional_items: list[dict],
    alternatives_by_flavor: dict,
) -> str:
    """Build soft reply when ALL items were optional and ALL are OOS.

    No order to confirm — just inform + suggest substitute.
    """
    from db.catalog import get_base_display_name

    def _display(item: dict) -> str:
        return item.get("display_name") or get_base_display_name(
            item["base_flavor"],
        )

    lines = ["Hi!"]

    for item in optional_items:
        flavor = item["base_flavor"]
        display = _display(item)
        decision = alternatives_by_flavor.get(flavor, {})
        alts = decision.get("alternatives", [])

        lines.append(f"Unfortunately, {display} is out of stock right now.")
        if alts:
            alt_names = [_format_alternative(a) for a in alts[:1]]
            lines.append(f"We have {', '.join(alt_names)} available.")

    lines.extend([
        "",
        "Would you like to place an order with a substitute?",
        "",
        "Check our website for more options:",
        "https://shipmecarton.com",
        "",
        "Please let us know!",
    ])

    return "\n".join(lines)

