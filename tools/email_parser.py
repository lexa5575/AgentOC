"""
Email Parser — deterministic parsing for website orders + body cleaning.

Two public functions:
- try_parse_order(email_text) → EmailClassification | None
  Detects website order notifications and parses fields with regex.
  Returns None if email is not a parseable order → falls through to LLM.

- clean_email_body(email_text) → str
  Strips quoted blocks, signatures, and excessive whitespace
  from the email body before sending to LLM classifier.
"""

import logging
import re

from agents.models import EmailClassification, OrderItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

# Product table row: row# ProductName $UnitPrice Qty $Total
_ITEM_RE = re.compile(
    r"^\d+\s+"  # row number
    r"(.+?)\s+"  # product name (non-greedy)
    r"\$?[\d,.]+\s+"  # unit price (not captured)
    r"(\d+)\s+"  # quantity
    r"\$?[\d,.]+",  # total (not captured)
    re.MULTILINE,
)

# Brand prefixes to strip from product names for base_flavor
BRAND_PREFIXES = ("Tera ", "Terea ", "Heets ")

# Region suffixes to strip (case-insensitive matching)
REGION_SUFFIXES = (
    " made in Middle East",
    " made in Armenia",
    " made in Europe",
    " EU",
    " Japan",
    " KZ",
)


# ---------------------------------------------------------------------------
# Quoted text removal (shared by parser + body cleaner)
# ---------------------------------------------------------------------------


def _strip_quoted_text(body: str) -> str:
    """Remove quoted reply blocks and signatures from email body."""
    # 1. "On ... wrote:" + everything after.
    #    Allow optional \r?\n before "wrote:" — email clients wrap long lines,
    #    e.g. "On Feb 27 James <email>\r\nwrote:"
    body = re.split(r"\n?\s*On .+?(?:\r?\n\s*)?wrote:\s*\n?", body)[0]

    # 2. "> " quoted lines
    body = re.sub(r"^>.*$", "", body, flags=re.MULTILINE)

    # 3. Common signatures — "Sent from my ..." matches inline too (no newline required),
    #    others require line start for safety.
    body = re.split(
        r"(?:(?:^|\n)\s*(?:Get Outlook for"
        r"|-{3,}\s*(?:Forwarded|Original) [Mm]essage)"
        r"|Sent from my (?:iPhone|iPad|Galaxy|Samsung)"
        r"|Sent (?:from|with) (?:\[?Proton Mail|Yahoo Mail)"
        r"|Отправлено с (?:iPhone|iPad))",
        body,
        flags=re.IGNORECASE,
    )[0]

    return body.strip()


# ---------------------------------------------------------------------------
# Base flavor extraction
# ---------------------------------------------------------------------------


def _extract_base_flavor(product_name: str) -> str:
    """Extract base flavor/color from product name.

    Strips brand prefixes (Tera, Terea, Heets) and region suffixes
    (EU, Japan, made in Middle East, etc.). Case-insensitive for suffixes.

    Examples:
        "Tera Green made in Middle East" → "Green"
        "Tera Turquoise EU" → "Turquoise"
        "ONE Green" → "ONE Green"
        "PRIME Black" → "PRIME Black"
    """
    name = product_name.strip()

    name_lower_check = name.lower()
    for prefix in BRAND_PREFIXES:
        if name_lower_check.startswith(prefix.lower()):
            name = name[len(prefix) :]
            break

    name_lower = name.lower()
    for suffix in REGION_SUFFIXES:
        if name_lower.endswith(suffix.lower()):
            name = name[: -len(suffix)]
            break

    return name.strip()


# ---------------------------------------------------------------------------
# Order item parsing
# ---------------------------------------------------------------------------


def _parse_order_items(body: str) -> list[OrderItem]:
    """Parse product table from order notification body.

    Normalizes whitespace (tabs, multiple spaces) before matching
    to handle HTML-to-text conversion artifacts.
    """
    # Normalize: tabs → spaces, collapse multiple spaces
    normalized_lines = []
    for line in body.splitlines():
        line = line.replace("\t", " ")
        line = re.sub(r" {2,}", " ", line).strip()
        if line:
            normalized_lines.append(line)
    text = "\n".join(normalized_lines)

    items = []
    for m in _ITEM_RE.finditer(text):
        product_name = m.group(1).strip()
        quantity = int(m.group(2))
        base_flavor = _extract_base_flavor(product_name)
        items.append(
            OrderItem(
                product_name=product_name,
                base_flavor=base_flavor,
                quantity=quantity,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Order notification detection
# ---------------------------------------------------------------------------


def _has_order_header(email_text: str) -> bool:
    """Check if email is FROM Shipmecarton (order notification from website).

    Only checks the From: header — NOT Subject. Customer replies with
    'Re: Shipmecarton - Order ...' in Subject must NOT trigger the parser.
    """
    # Extract From: line from headers
    for line in email_text.split("\n"):
        if line.startswith("From:"):
            return "shipmecarton" in line.lower()
        if line.startswith("Body:"):
            break  # Past headers, stop
    return False


def _is_order_notification(body: str) -> bool:
    """Check if body contains website order signature markers.

    Both Order ID and Payment amount must be present in unquoted text.
    """
    return bool(
        re.search(r"Order ID:\s*\d+", body)
        and re.search(r"Payment amount:", body)
    )


# ---------------------------------------------------------------------------
# Main: try_parse_order
# ---------------------------------------------------------------------------


def try_parse_order(email_text: str) -> EmailClassification | None:
    """Try to parse a website order notification deterministically.

    Detects order emails by content markers (Order ID + Payment amount)
    in the UNQUOTED part of the body — prevents false positives on
    customer replies that contain a quoted order notification.

    Returns EmailClassification if successfully parsed, None otherwise
    (caller should fall through to LLM classifier).
    """
    # Header gate: must come from Shipmecarton (From or Subject)
    if not _has_order_header(email_text):
        return None

    # Split headers from body
    if "\nBody:" in email_text:
        _, body = email_text.split("\nBody:", 1)
    else:
        body = email_text

    # Strip quoted text BEFORE checking markers
    unquoted_body = _strip_quoted_text(body)

    if not _is_order_notification(unquoted_body):
        return None

    # --- Parse fields from the full unquoted body ---

    # Order ID
    m = re.search(r"Order ID:\s*(\d+)", unquoted_body)
    if not m:
        return None
    order_id = m.group(1)

    # Payment amount
    m = re.search(r"Payment amount:\s*\$?([\d,.]+)", unquoted_body)
    price = f"${m.group(1)}" if m else None

    # Customer email (from "Email:" field in body)
    client_email = None
    m = re.search(r"(?:^|\n)\s*Email:\s*(.+)", unquoted_body)
    if m:
        email_match = _EMAIL_RE.search(m.group(1))
        if email_match:
            client_email = email_match.group(0).lower()

    # Fallback: try Reply-To header
    if not client_email:
        header = email_text.split("\nBody:", 1)[0] if "\nBody:" in email_text else ""
        for line in header.splitlines():
            if line.lower().startswith("reply-to:"):
                email_match = _EMAIL_RE.search(line)
                if email_match:
                    client_email = email_match.group(0).lower()
                    break

    # Conservative: no email → can't route, fall to LLM
    if not client_email:
        logger.warning("Order parser: Order ID %s found but no client email — fallback to LLM", order_id)
        return None

    # Customer name
    m = re.search(r"Firstname:\s*(.+?)(?:\n|$)", unquoted_body)
    client_name = m.group(1).strip() if m else None

    # Street address
    m = re.search(r"Street address1?:\s*(.+?)(?:\n|$)", unquoted_body)
    customer_street = m.group(1).strip() if m else None

    # City, State, Zip (3 separate fields)
    town_m = re.search(r"Town/City:\s*(.+?)(?:\n|$)", unquoted_body)
    state_m = re.search(r"State:\s*(.+?)(?:\n|$)", unquoted_body)
    zip_m = re.search(r"Postcode/Zip:\s*(.+?)(?:\n|$)", unquoted_body)

    city_state_zip = None
    if town_m:
        parts = [town_m.group(1).strip()]
        if state_m:
            parts.append(state_m.group(1).strip())
        if zip_m:
            parts[-1] = parts[-1] + " " + zip_m.group(1).strip() if parts else zip_m.group(1).strip()
        city_state_zip = ", ".join(parts) if len(parts) > 1 else parts[0]

    # Order items (product table)
    order_items = _parse_order_items(unquoted_body)

    # Conservative: no items → can't do stock check, fall to LLM
    if not order_items:
        logger.warning("Order parser: Order ID %s found but no items parsed — fallback to LLM", order_id)
        return None

    # Build items text (free-text summary)
    items_text = ", ".join(f"{oi.product_name} x {oi.quantity}" for oi in order_items)

    logger.info(
        "Order parsed: id=%s, email=%s, items=%d, price=%s",
        order_id, client_email, len(order_items), price,
    )

    return EmailClassification(
        needs_reply=True,
        situation="new_order",
        client_email=client_email,
        client_name=client_name,
        order_id=order_id,
        price=price,
        customer_street=customer_street,
        customer_city_state_zip=city_state_zip,
        items=items_text,
        order_items=order_items,
        is_followup=False,
        followup_to=None,
        dialog_intent=None,
        parser_used=True,
    )


# ---------------------------------------------------------------------------
# Body cleaner for non-order emails
# ---------------------------------------------------------------------------


def clean_email_body(email_text: str) -> str:
    """Clean email text: strip quoted blocks, signatures, whitespace.

    Used for non-order emails before sending to LLM classifier.
    Only modifies the Body section; headers are preserved unchanged.
    """
    if "\nBody:" not in email_text:
        return email_text

    header, body = email_text.split("\nBody:", 1)
    body = _strip_quoted_text(body)

    # Collapse excessive whitespace
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.strip()

    return header + "\nBody: " + body
