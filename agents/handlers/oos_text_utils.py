"""OOS Text Utilities
--------------------

Pure regex/text utilities for OOS handling.
No project-level imports — stdlib only.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Region detection
# ---------------------------------------------------------------------------

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
# Quantity extraction
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


# ---------------------------------------------------------------------------
# Label parsers (ordered_items label format: "Tera PURPLE WAVE made in Middle East x2")
# ---------------------------------------------------------------------------

_LABEL_REGION_MAP = {
    "middle east": "ME", "europe": "EU", "european": "EU",
    "japan": "Japan", "japanese": "Japan",
    "kazakhstan": "KZ", "armenia": "Armenia",
}


def _extract_base_flavor_from_label(label: str) -> str:
    """Extract base flavor from ordered_items label like 'Tera PURPLE WAVE made in Middle East x2'."""
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
    m = re.search(r"\bmade\s+in\s+(.+?)(?:\s+x\d+)?$", label, flags=re.IGNORECASE)
    if m:
        region_raw = m.group(1).strip().lower()
        return _LABEL_REGION_MAP.get(region_raw, "")
    return ""


def _extract_qty_from_label(label: str) -> int:
    """Extract quantity from label like 'Tera PURPLE WAVE made in Middle East x2'."""
    m = re.search(r"\bx(\d+)\s*$", label, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1
