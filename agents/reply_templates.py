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
        "Hello {CUSTOMER_NAME}\n"
        "How are you?\n"
        "Thank you very much for a prompt payment!\n"
        "Nice doing business with you!!!\n"
        "\n"
        "We will ship your order today!\n"
        "Here is the USPS tracking number:\n"
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
}

# ---------------------------------------------------------------------------
# Out-of-Stock Template (STABLE — Python fills variables, no LLM)
# ---------------------------------------------------------------------------
def _format_alternative(alt_entry: dict) -> str:
    """Format a single alternative for customer-facing display.

    Args:
        alt_entry: Dict with keys: alternative (stock item dict), reason, order_count

    Returns:
        Formatted string like "Terea Purple made in Japan", "Terea Green EU", "Terea Amber ME"
    """
    alt = alt_entry["alternative"]
    reason = alt_entry.get("reason", "fallback")
    raw_name = alt["product_name"]
    category = alt.get("category", "")

    if category in ("TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"):
        # "T Purple" → "Terea Purple made in Japan"
        # "Fusion Menthol" → "Terea Fusion Menthol made in Japan"
        core = raw_name[2:] if raw_name.startswith("T ") else raw_name
        formatted = f"Terea {core} made in Japan"
    elif category == "TEREA_EUROPE":
        formatted = f"Terea {raw_name} EU"
    elif category in ("ARMENIA", "KZ_TEREA"):
        formatted = f"Terea {raw_name} ME"
    else:
        # devices and unknown — no region label
        formatted = raw_name

    if reason == "same_flavor":
        formatted += " (same product, different region)"
    elif reason == "history":
        formatted += " (you've ordered before)"

    return formatted


def fill_out_of_stock_template(
    insufficient_items: list[dict],
    best_alternatives: dict,
) -> str:
    """Fill the Out-of-Stock template with actual data. Zero LLM tokens.
    
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
    # Step 1: Classify items into full OOS vs partial OOS
    full_oos = []       # total_available == 0
    partial_oos = []    # total_available > 0 but < ordered
    
    for item in insufficient_items:
        if item["total_available"] == 0:
            full_oos.append(item)
        else:
            partial_oos.append(item)
    
    # Step 2: Build the problem description
    problem_parts = []
    
    if full_oos:
        if len(full_oos) == 1:
            problem_parts.append(f"we just ran out of {full_oos[0]['base_flavor']}")
        else:
            flavors = ", ".join([i["base_flavor"] for i in full_oos[:-1]])
            flavors += f" and {full_oos[-1]['base_flavor']}"
            problem_parts.append(f"we just ran out of {flavors}")
    
    if partial_oos:
        for p in partial_oos:
            problem_parts.append(
                f"we only have {p['total_available']} {p['base_flavor']} available "
                f"(you ordered {p['ordered_qty']})"
            )
    
    # Combine problem parts
    if len(problem_parts) == 1:
        problem_text = problem_parts[0]
    elif len(problem_parts) == 2:
        problem_text = f"{problem_parts[0]}, and {problem_parts[1]}"
    else:
        problem_text = ", ".join(problem_parts[:-1]) + f", and {problem_parts[-1]}"
    
    # Step 3: Build alternatives section
    has_alternatives = False
    alt_lines = []
    
    for item in insufficient_items:
        flavor = item["base_flavor"]
        decision = best_alternatives.get(flavor, {})
        alts = decision.get("alternatives", [])
        
        if alts:
            has_alternatives = True
            # Format up to 3 alternatives
            formatted_alts = [_format_alternative(a) for a in alts[:3]]
            
            if len(insufficient_items) == 1:
                # Single flavor — no need to specify "For X:"
                alt_lines.append(", ".join(formatted_alts))
            else:
                # Multiple flavors — specify which flavor
                alt_lines.append(f"For {flavor}: {', '.join(formatted_alts)}")
    
    # Step 4: Build the final email
    lines = [
        "Hi!",
        "How are you?",
        f"Unfortunately, {problem_text}",
        "",
        "What can we offer? Please choose one of the options below.",
    ]
    
    if has_alternatives:
        if len(alt_lines) == 1:
            lines.append(f"1. We have {alt_lines[0]}")
        else:
            lines.append("1. We have alternatives:")
            for alt_line in alt_lines:
                lines.append(f"   {alt_line}")
        lines.append("2. Check our website for substitutions and ready to ship sticks.")
    else:
        # No alternatives — only website option
        lines.append("1. Check our website for substitutions and ready to ship sticks.")
    
    lines.extend([
        "",
        "Link for the sticks substitution",
        "https://shipmecarton.com",
        "",
        "Please let us know what you think",
    ])
    
    return "\n".join(lines)

