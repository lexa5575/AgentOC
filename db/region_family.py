"""
Region Family Policy — Single Source of Truth
----------------------------------------------

Groups of stock categories that are interchangeable for customers
(same product, same price, same customer-facing region label).

v1: ME family (ARMENIA ↔ KZ_TEREA).
v2: Japan family (TEREA_JAPAN ↔ УНИКАЛЬНАЯ_ТЕРЕА) — same manufacturer, same price ($115).

All helpers follow FAIL-CLOSED principle:
- Unknown categories → not same family → ambiguous
- Empty sets → not same family → ambiguous
- Unknown product_ids → None (no preferred)
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

REGION_FAMILIES: dict[str, frozenset[str]] = {
    "ME": frozenset({"ARMENIA", "KZ_TEREA"}),
    "EU": frozenset({"TEREA_EUROPE"}),
    "JAPAN": frozenset({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}),
}

# Within a family, which category's product_id to pick as variant_id
PREFERRED_CATEGORY: dict[str, str] = {
    "ME": "ARMENIA",
    "EU": "TEREA_EUROPE",
    "JAPAN": "TEREA_JAPAN",
}

# Customer-facing region suffix (all categories, including Japan — display
# doesn't depend on family policy)
CATEGORY_REGION_SUFFIX: dict[str, str] = {
    "ARMENIA": "ME",
    "KZ_TEREA": "ME",  # FIX: was "KZ" in oos_followup.py
    "TEREA_EUROPE": "EU",
    "TEREA_JAPAN": "Japan",
    "УНИКАЛЬНАЯ_ТЕРЕА": "Japan",
}

# Reverse lookup: category → family name (built from REGION_FAMILIES)
_CATEGORY_TO_FAMILY: dict[str, str] = {}
for _family, _cats in REGION_FAMILIES.items():
    for _cat in _cats:
        _CATEGORY_TO_FAMILY[_cat] = _family


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_family(category: str) -> str | None:
    """Return the family name for a category, or None if not in any family."""
    return _CATEGORY_TO_FAMILY.get(category)


def is_same_family(categories: set[str]) -> bool:
    """Return True if ALL categories belong to the same known region family.

    FAIL-CLOSED:
    - Empty set → False (unknown = ambiguous)
    - Any unknown category → False
    - Single known category → True
    - Multiple categories in same family → True
    - Cross-family → False
    """
    if not categories:
        return False

    families: set[str | None] = set()
    for cat in categories:
        f = _CATEGORY_TO_FAMILY.get(cat)
        if f is None:
            # Unknown category → fail-closed
            return False
        families.add(f)

    return len(families) == 1


def get_preferred_product_id(
    product_ids: list[int],
    catalog_entries: list[dict],
) -> int | None:
    """Pick the preferred product_id from same-family candidates.

    FAIL-CLOSED:
    - Empty product_ids → None
    - Any product_id not found in catalog → None
    - Cross-family product_ids → None
    - Same-family → return id from PREFERRED_CATEGORY

    Args:
        product_ids: List of catalog ids (expected same family).
        catalog_entries: Catalog dicts with 'id' and 'category' keys.
    """
    if not product_ids:
        return None

    if len(product_ids) == 1:
        # Single id — validate it exists in catalog (fail-closed)
        cat_lookup = {e["id"]: e["category"] for e in catalog_entries}
        if product_ids[0] not in cat_lookup:
            logger.warning(
                "get_preferred_product_id: single pid %d not in catalog — returning None",
                product_ids[0],
            )
            return None
        return product_ids[0]

    # Build lookup for relevant ids
    id_to_cat: dict[int, str] = {}
    cat_lookup = {e["id"]: e["category"] for e in catalog_entries}
    for pid in product_ids:
        cat = cat_lookup.get(pid)
        if cat is None:
            # Unknown product_id → fail-closed
            logger.warning(
                "get_preferred_product_id: pid %d not in catalog — returning None",
                pid,
            )
            return None
        id_to_cat[pid] = cat

    categories = set(id_to_cat.values())
    if not is_same_family(categories):
        # Cross-family → fail-closed
        return None

    # Find the family
    family = _CATEGORY_TO_FAMILY.get(next(iter(categories)))
    if family and family in PREFERRED_CATEGORY:
        preferred_cat = PREFERRED_CATEGORY[family]
        for pid, cat in id_to_cat.items():
            if cat == preferred_cat:
                return pid

    # Fail-closed: preferred category not found → None
    logger.warning(
        "get_preferred_product_id: preferred category not found for family %s — returning None",
        family,
    )
    return None


def expand_to_family_ids(
    product_ids: list[int],
    catalog_entries: list[dict],
) -> list[int]:
    """Expand product_ids to include all same-family siblings with same name_norm.

    Given [ARMENIA Silver id=17], returns [17, 24] (adding KZ_TEREA Silver).
    STRICTLY filters by name_norm — Silver will NOT pull in Amber or Bronze.

    Args:
        product_ids: Current product_ids (may be single preferred id).
        catalog_entries: Full catalog for sibling lookup.

    Returns:
        Expanded list of product_ids including family siblings.
    """
    if not product_ids or not catalog_entries:
        return list(product_ids) if product_ids else []

    id_to_entry: dict[int, dict] = {e["id"]: e for e in catalog_entries}
    id_set = set(product_ids)

    for pid in list(product_ids):  # iterate copy since we modify id_set
        entry = id_to_entry.get(pid)
        if not entry:
            continue

        family = _CATEGORY_TO_FAMILY.get(entry["category"])
        if not family:
            continue

        family_cats = REGION_FAMILIES[family]

        for other in catalog_entries:
            if (
                other["id"] not in id_set
                and other["category"] in family_cats
                and other["name_norm"] == entry["name_norm"]
            ):
                id_set.add(other["id"])

    return sorted(id_set)


def get_region_suffix(category: str) -> str | None:
    """Return customer-facing region suffix for a category."""
    return CATEGORY_REGION_SUFFIX.get(category)


# Family → customer-facing suffix
_FAMILY_SUFFIX: dict[str, str] = {"EU": "EU", "ME": "ME", "JAPAN": "Japan"}


def get_family_suffix(family: str) -> str | None:
    """Family name → customer-facing suffix. E.g. "EU" → "EU", "JAPAN" → "Japan"."""
    return _FAMILY_SUFFIX.get(family)


_REGION_KEYWORDS: dict[str, str] = {
    "made in europe": "EU",
    "made in middle east": "ME",
    "made in armenia": "ME",
    "made in japan": "JAPAN",
    "european": "EU",
}


def extract_region_from_text(text: str) -> str | None:
    """Extract a single region family from free-form email text.

    Scans for region keywords anywhere in text (not just suffixes).
    Returns None if 0 or >1 families detected.

    Examples:
        "I'll do Blue made in Europe please" → "EU"
        "Blue" → None
    """
    text_lower = text.lower()
    found: set[str] = set()
    # Check longer phrases first
    for phrase, family in sorted(_REGION_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if phrase in text_lower:
            found.add(family)
    return found.pop() if len(found) == 1 else None
