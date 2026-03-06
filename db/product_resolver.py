"""
Product Name Resolver
---------------------

Multi-tier product name matching:
1. Alias lookup (deterministic, 0ms)
2. Exact match (case-insensitive, after normalization)
3. Device models (ONE, STND, PRIME)
4. Word-prefix ("SUMMER BREEZE" → "SUMMER" → match)
5. Fuzzy match (SequenceMatcher >= 0.80 + gap)
6. LLM fallback (gpt-4o-mini for medium confidence — ~1-5% of cases)
7. Return medium/low → operator alert

Region-aware: if the original product name contains a region hint
(e.g. "made in Europe", "EU"), product_ids are filtered to the matching
category. "Tera AMBER made in Europe" → only TEREA_EUROPE ids.

Usage:
    resolved_items, alerts = resolve_order_items(items)
    # resolved_items: items with auto-corrected names + product_ids (exact/high)
    # alerts: list of unresolved items (medium/low) for operator attention
"""

import logging
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Feature flag: use ProductCatalog instead of StockItem for resolution.
# Set USE_CATALOG_RESOLVER=false in env to revert to legacy string path.
USE_CATALOG_RESOLVER = os.environ.get("USE_CATALOG_RESOLVER", "true").lower() == "true"

# Feature flag: use LLM (gpt-4o-mini) for medium confidence cases.
# Set USE_LLM_RESOLVER=false in env to disable LLM fallback.
USE_LLM_RESOLVER = os.environ.get("USE_LLM_RESOLVER", "true").lower() == "true"

# Brand prefixes to strip before matching (mirrors email_parser logic)
_BRAND_PREFIXES = ("Tera ", "Terea ", "Heets ", "T ")

# Region and product-line suffixes to strip (case-insensitive)
_REGION_SUFFIXES = (
    " made in middle east",
    " made in armenia",
    " made in europe",
    " eu",
    " japan",
    " kz",
    " unique flavor",  # УНИКАЛЬНАЯ_ТЕРЕА line: "Black Purple Menthol Unique Flavor" → "Black Purple Menthol"
    " unique",         # shorter variant
)

# Origin suffixes that indicate a specific regional variant (Armenia/EU/KZ/Japan).
# Used to detect whether "Tera Purple" means Japan T Purple (no suffix) vs Armenia Purple
# ("Tera Purple made in Middle East"). Does NOT include "unique flavor" since that's
# a product-line marker, not a regional origin indicator.
_ORIGIN_SUFFIXES = (
    " made in middle east",
    " made in armenia",
    " made in europe",
    " eu",
    " japan",
    " kz",
)

# Device model names — valid as standalone (no color required)
_DEVICE_MODELS = {"ONE", "STND", "PRIME"}

# ---------------------------------------------------------------------------
# Tier 1: Aliases (deterministic, 0ms)
# ---------------------------------------------------------------------------
# Maps alternative/misspelled names → canonical stock name.
# Keys are lowercased. Add new entries as new site patterns are discovered.
_ALIASES: dict[str, str] = {
    # Abbreviations (fuzzy can't match short forms)
    "pw": "Purple Wave",
    "purple w": "Purple Wave",
    # Multi-character typos (fuzzy gives medium, not high)
    "tourquoise": "Turquoise",
    "turqoise": "Turquoise",
    # Site-specific naming patterns (word-prefix handles some, alias is faster)
    "summer breeze": "Summer",
}

# ---------------------------------------------------------------------------
# Region detection: suffix → allowed stock categories
# ---------------------------------------------------------------------------
_REGION_TO_CATEGORIES: dict[str, frozenset[str]] = {
    " made in europe": frozenset({"TEREA_EUROPE"}),
    " eu": frozenset({"TEREA_EUROPE"}),
    " made in middle east": frozenset({"ARMENIA", "KZ_TEREA"}),
    " made in armenia": frozenset({"ARMENIA"}),
    " me": frozenset({"ARMENIA", "KZ_TEREA"}),
    " japan": frozenset({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}),
    " kz": frozenset({"KZ_TEREA"}),
}


@dataclass
class ResolveResult:
    """Result of resolving a single product name."""

    original: str
    resolved: str | None
    confidence: str  # "exact", "high", "medium", "low"
    score: float
    candidates: list[str] = field(default_factory=list)
    product_ids: list[int] = field(default_factory=list)
    name_norm: str | None = None
    display_name: str | None = None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Normalize product name: strip brand prefixes and region suffixes."""
    name = name.strip()
    name_lower_check = name.lower()
    for prefix in _BRAND_PREFIXES:
        if name_lower_check.startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    name_lower = name.lower()
    for suffix in _REGION_SUFFIXES:
        if name_lower.endswith(suffix):
            name = name[: len(name) - len(suffix)]
            break
    return name.strip()


def _has_origin_suffix(raw_name: str) -> bool:
    """Return True if raw_name contains an explicit regional origin suffix.

    Used to distinguish "Tera Purple" (Japan T Purple) from
    "Tera Purple made in Middle East" (Armenia Purple).
    """
    name = raw_name.strip()
    name_lower_check = name.lower()
    for prefix in _BRAND_PREFIXES:
        if name_lower_check.startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    name_lower = name.lower()
    return any(name_lower.endswith(s) for s in _ORIGIN_SUFFIXES)


def _resolve_via_alias(raw_name: str) -> str | None:
    """Tier 1: Look up normalized name in alias dictionary.

    Returns canonical product name or None if no alias found.
    """
    normalized = _normalize(raw_name).lower()
    return _ALIASES.get(normalized)


def _extract_region_categories(name: str) -> frozenset[str] | None:
    """Extract target stock categories from a region suffix in the product name.

    Strips brand prefix first, then checks for region suffixes.
    Returns matching categories or None if no region detected.

    Examples:
        "Tera AMBER made in Europe" → {"TEREA_EUROPE"}
        "Silver EU" → {"TEREA_EUROPE"}
        "Green made in Middle East" → {"ARMENIA", "KZ_TEREA"}
        "Silver" → None (no region)
    """
    name = name.strip()
    name_lower_check = name.lower()
    for prefix in _BRAND_PREFIXES:
        if name_lower_check.startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    name_lower = name.lower()
    for suffix, cats in _REGION_TO_CATEGORIES.items():
        if name_lower.endswith(suffix):
            return cats
    return None


# ---------------------------------------------------------------------------
# Known names from stock DB (legacy — kept for backward compat / fallback)
# ---------------------------------------------------------------------------

def get_known_product_names() -> list[str]:
    """Get distinct product names from stock table."""
    from db.models import StockItem, get_session

    session = get_session()
    try:
        rows = session.query(StockItem.product_name).distinct().all()
        return sorted(set(r[0] for r in rows))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# LLM fallback (Tier 3 — gpt-4o-mini for medium confidence)
# ---------------------------------------------------------------------------

def _resolve_via_llm(raw_name: str, known_names: list[str]) -> str | None:
    """Ask gpt-4o-mini to match a product name when fuzzy matching is uncertain.

    Called only for medium confidence cases (~1-5% of lookups).
    Returns the matched product name or None if LLM can't determine.

    Validates LLM response against known_names to prevent hallucination.
    On any API error, returns None (graceful fallback to medium/operator alert).
    """
    try:
        import openai

        catalog_list = ", ".join(known_names)
        prompt = (
            f'Customer ordered: "{raw_name}"\n\n'
            f"Our product catalog:\n{catalog_list}\n\n"
            f"NAMING CONVENTIONS (important!):\n"
            f'- "Tera", "Terea", "TERA" brand prefix = "T" prefix in our catalog\n'
            f'  Example: "TERA SMOOTH REGULAR" → "T Smooth", "Terea Purple" → "T Purple"\n'
            f'- Website names often include extra trailing words like "Regular", "Flavor"\n'
            f'  Example: "BALANCED REGULAR" → "T Balanced", "RICH REGULAR" → "T RICH"\n'
            f"- Ignore case differences\n"
            f"- Armenia/EU/KZ products have NO prefix (just the flavor name)\n\n"
            f"Which product from our catalog did the customer mean?\n"
            f'Reply with ONLY the exact product name from the catalog above.\n'
            f'If you truly cannot determine, reply with "NONE".'
        )

        client = openai.OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=50,
        )
        answer = response.choices[0].message.content.strip()

        if answer == "NONE":
            logger.info("LLM resolver: NONE for '%s'", raw_name)
            return None

        # Validate: answer must exactly match a known name
        for name in known_names:
            if name.lower() == answer.lower():
                return name

        logger.warning(
            "LLM resolver: response '%s' not in known names for '%s'",
            answer, raw_name,
        )
        return None

    except Exception as e:
        logger.warning("LLM resolver error for '%s': %s", raw_name, e)
        return None


# ---------------------------------------------------------------------------
# Single name resolver (core matching logic)
# ---------------------------------------------------------------------------

def resolve_product_name(
    raw_name: str,
    known_names: list[str] | None = None,
) -> ResolveResult:
    """Resolve a potentially misspelled product name against known stock names.

    Args:
        raw_name: Customer-provided product name (may be misspelled)
        known_names: Optional pre-fetched list of known product names.
                     If None, queries the database.

    Returns:
        ResolveResult with confidence level and best match.
    """
    if known_names is None:
        known_names = get_known_product_names()

    if not known_names:
        # No stock data — pass through unchanged
        return ResolveResult(raw_name, raw_name, "low", 0.0, [])

    # 1. Alias lookup (Tier 1 — deterministic, 0ms)
    alias_match = _resolve_via_alias(raw_name)
    if alias_match:
        # Verify alias target exists in known_names
        for name in known_names:
            if name.lower() == alias_match.lower():
                logger.info("Alias match: '%s' → '%s'", raw_name, name)
                return ResolveResult(raw_name, name, "exact", 1.0, [name])

    normalized = _normalize(raw_name)
    normalized_lower = normalized.lower()

    # 2. Exact match (case-insensitive, after normalization)
    for name in known_names:
        if _normalize(name).lower() == normalized_lower:
            # Japan T-prefix heuristic: if the input has NO regional origin suffix
            # and a "T <name>" Japan variant exists, prefer it over the plain
            # Armenia/EU/KZ product with the same base name.
            # Example: "Tera PURPLE" (no ME/EU suffix) → T Purple, not Armenia Purple.
            # "Tera Purple made in Middle East" (has ME suffix) → Armenia Purple.
            if not _has_origin_suffix(raw_name):
                t_variant = next(
                    (n for n in known_names if n.lower() == "t " + normalized_lower),
                    None,
                )
                if t_variant:
                    logger.info(
                        "Japan T-prefix heuristic: '%s' → '%s' (no origin suffix, T-variant exists)",
                        raw_name, t_variant,
                    )
                    return ResolveResult(raw_name, t_variant, "high", 0.92, [t_variant, name])
            return ResolveResult(raw_name, name, "exact", 1.0, [name])

    # 3. Device model-only: "ONE", "STND", "PRIME" — valid without color
    if normalized.upper() in _DEVICE_MODELS:
        return ResolveResult(raw_name, normalized.upper(), "exact", 1.0, [normalized.upper()])

    # 4. Word-prefix match: "SUMMER BREEZE" → try "SUMMER" → matches "Summer"
    #    Site/email names often add extra words to the base product name.
    words = normalized.split()
    if len(words) >= 2:
        # Build known names lookup (normalized → original)
        known_lookup: dict[str, str] = {}
        for name in known_names:
            known_lookup[_normalize(name).lower()] = name
        # Try removing trailing words one by one
        for end in range(len(words) - 1, 0, -1):
            prefix_lower = " ".join(words[:end]).lower()
            if prefix_lower in known_lookup:
                matched = known_lookup[prefix_lower]
                # Apply Japan T-prefix heuristic (same as exact match)
                if not _has_origin_suffix(raw_name):
                    t_variant = next(
                        (n for n in known_names if n.lower() == "t " + prefix_lower),
                        None,
                    )
                    if t_variant:
                        logger.info(
                            "Word-prefix + T-heuristic: '%s' → '%s'",
                            raw_name, t_variant,
                        )
                        return ResolveResult(raw_name, t_variant, "high", 0.95, [t_variant, matched])
                logger.info(
                    "Word-prefix match: '%s' → '%s' (dropped trailing words)",
                    raw_name, matched,
                )
                return ResolveResult(raw_name, matched, "high", 0.95, [matched])

    # 5. Fuzzy match via SequenceMatcher
    scores = []
    for name in known_names:
        name_norm = _normalize(name).lower()
        score = SequenceMatcher(None, normalized_lower, name_norm).ratio()
        scores.append((name, score))
    scores.sort(key=lambda x: x[1], reverse=True)

    best_name, best_score = scores[0]
    second_score = scores[1][1] if len(scores) >= 2 else 0.0
    top_candidates = [n for n, _ in scores[:3]]
    gap = best_score - second_score

    if best_score >= 0.80 and gap >= 0.15:
        return ResolveResult(raw_name, best_name, "high", best_score, top_candidates)

    # 6. LLM fallback for medium confidence (gpt-4o-mini)
    if best_score >= 0.55 and USE_LLM_RESOLVER:
        llm_match = _resolve_via_llm(raw_name, known_names)
        if llm_match:
            logger.info(
                "LLM resolved: '%s' → '%s' (fuzzy was medium, score=%.2f)",
                raw_name, llm_match, best_score,
            )
            return ResolveResult(raw_name, llm_match, "high", 0.85, top_candidates)

    if best_score >= 0.55:
        return ResolveResult(raw_name, None, "medium", best_score, top_candidates)
    return ResolveResult(raw_name, None, "low", best_score, top_candidates)


# ---------------------------------------------------------------------------
# Catalog-backed resolver (Phase 2)
# ---------------------------------------------------------------------------

def resolve_product_to_catalog(
    raw_name: str,
    catalog_entries: list[dict] | None = None,
    original_product_name: str | None = None,
) -> ResolveResult:
    """Resolve a product name against the ProductCatalog.

    Like resolve_product_name() but uses catalog as source of truth
    and returns product_ids for downstream lookups.

    Region-aware: if original_product_name (or raw_name) contains a region
    suffix (e.g. "made in Europe", "EU"), product_ids are filtered to the
    matching category only. This prevents "Tera AMBER made in Europe" from
    matching KZ Amber when EU Amber is out of stock.

    Args:
        raw_name: Customer-provided product name (may be misspelled).
        catalog_entries: Optional pre-fetched catalog entries from
            get_catalog_products(). If None, queries the database.
        original_product_name: Full product name from the order (e.g.
            "Tera AMBER made in Europe"). Used for region detection.
            Falls back to raw_name if not provided.

    Returns:
        ResolveResult with product_ids, name_norm, and display_name populated
        for exact/high confidence matches.
    """
    if not USE_CATALOG_RESOLVER:
        return ResolveResult(raw_name, None, "low", 0.0)

    if catalog_entries is None:
        from db.catalog import get_catalog_products
        catalog_entries = get_catalog_products()

    if not catalog_entries:
        return ResolveResult(raw_name, raw_name, "low", 0.0, [])

    # Filter catalog by product type (sticks vs devices)
    from db.stock import STICK_CATEGORIES, DEVICE_CATEGORIES, get_product_type
    product_type = get_product_type(raw_name)
    allowed_cats = DEVICE_CATEGORIES if product_type == "device" else STICK_CATEGORIES
    filtered = [e for e in catalog_entries if e["category"] in allowed_cats]

    if not filtered:
        return ResolveResult(raw_name, raw_name, "low", 0.0, [])

    # Extract unique stock_names (deduplicated by normalized form)
    seen_norms: set[str] = set()
    known_names: list[str] = []
    for entry in filtered:
        norm_key = _normalize(entry["stock_name"]).lower()
        if norm_key not in seen_norms:
            seen_norms.add(norm_key)
            known_names.append(entry["stock_name"])

    # Use existing matching logic
    result = resolve_product_name(raw_name, known_names)

    # Enrich with catalog data if match found
    if result.resolved and result.confidence in ("exact", "high"):
        resolved_norm = _normalize(result.resolved).lower()

        # Expand to spelling equivalents (e.g. "sienna" ↔ "siena")
        from db.catalog import get_equivalent_norms
        equivalent_norms = get_equivalent_norms(resolved_norm)

        matching = [
            e for e in filtered
            if _normalize(e["stock_name"]).lower() in equivalent_norms
        ]

        # Region-aware filtering: if the product name contains a region
        # suffix, narrow product_ids to only the matching category.
        region_source = original_product_name or raw_name
        region_cats = _extract_region_categories(region_source)
        if region_cats and matching:
            region_matching = [e for e in matching if e["category"] in region_cats]
            if region_matching:
                logger.info(
                    "Region filter: '%s' → categories %s (%d/%d catalog entries)",
                    region_source, set(region_cats),
                    len(region_matching), len(matching),
                )
                matching = region_matching

        result.product_ids = [e["id"] for e in matching]
        result.name_norm = matching[0]["name_norm"] if matching else None

        # Display name: region-specific if one category, generic otherwise
        if matching:
            from db.catalog import get_base_display_name, get_display_name
            categories = {e["category"] for e in matching}
            if len(categories) == 1:
                result.display_name = get_display_name(
                    matching[0]["stock_name"], matching[0]["category"]
                )
            else:
                result.display_name = get_base_display_name(matching[0]["stock_name"])

    return result


# ---------------------------------------------------------------------------
# Batch resolver for order items
# ---------------------------------------------------------------------------

def resolve_order_items(
    items: list[dict],
    known_names: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve product names in a list of order items.

    Uses ProductCatalog when USE_CATALOG_RESOLVER is True (default),
    falls back to legacy StockItem names otherwise.

    Args:
        items: List of dicts with keys: base_flavor, product_name, quantity.
        known_names: Optional pre-fetched known names (legacy path only).

    Returns:
        (resolved_items, alerts):
        - resolved_items: items with auto-corrected names + product_ids (exact/high)
        - alerts: list of unresolved items for operator attention
    """
    # If known_names explicitly provided, always use legacy path (tests + manual override)
    if known_names is not None:
        return _resolve_order_items_legacy(items, known_names)
    if USE_CATALOG_RESOLVER:
        return _resolve_order_items_catalog(items)
    return _resolve_order_items_legacy(items, None)


def _resolve_order_items_catalog(
    items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Resolve order items using ProductCatalog as source of truth."""
    from db.catalog import get_catalog_products

    catalog_entries = get_catalog_products()
    if not catalog_entries:
        return items, []  # No catalog data — pass through

    resolved = []
    alerts = []

    for item in items:
        result = resolve_product_to_catalog(
            item["base_flavor"],
            catalog_entries,
            original_product_name=item.get("product_name"),
        )

        if result.confidence in ("exact", "high"):
            resolved_item = {**item}
            if result.resolved and result.resolved != item["base_flavor"]:
                resolved_item["base_flavor"] = result.resolved
                resolved_item["product_name"] = result.resolved
                logger.info(
                    "Product resolved (catalog): '%s' → '%s' (confidence=%s, score=%.2f, ids=%s)",
                    item["base_flavor"], result.resolved,
                    result.confidence, result.score, result.product_ids,
                )
            # Enrich with catalog data
            if result.product_ids:
                resolved_item["product_ids"] = result.product_ids
            if result.display_name:
                resolved_item["display_name"] = result.display_name
            resolved.append(resolved_item)
        else:
            # medium/low — pass through unchanged, add to alerts
            resolved.append(item)
            alerts.append({
                "original": item["base_flavor"],
                "confidence": result.confidence,
                "score": round(result.score, 2),
                "candidates": result.candidates,
            })
            logger.warning(
                "Product unresolved: '%s' (confidence=%s, score=%.2f, candidates=%s)",
                item["base_flavor"], result.confidence,
                result.score, result.candidates[:3],
            )

    return resolved, alerts


def _resolve_order_items_legacy(
    items: list[dict],
    known_names: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve order items using legacy StockItem names (fallback path)."""
    if known_names is None:
        known_names = get_known_product_names()

    if not known_names:
        return items, []  # No stock data — pass through

    resolved = []
    alerts = []

    for item in items:
        result = resolve_product_name(item["base_flavor"], known_names)

        if result.confidence in ("exact", "high"):
            resolved_item = {**item}
            if result.resolved and result.resolved != item["base_flavor"]:
                resolved_item["base_flavor"] = result.resolved
                resolved_item["product_name"] = result.resolved
                logger.info(
                    "Product resolved: '%s' → '%s' (confidence=%s, score=%.2f)",
                    item["base_flavor"], result.resolved,
                    result.confidence, result.score,
                )
            resolved.append(resolved_item)
        else:
            # medium/low — pass through unchanged, add to alerts
            resolved.append(item)
            alerts.append({
                "original": item["base_flavor"],
                "confidence": result.confidence,
                "score": round(result.score, 2),
                "candidates": result.candidates,
            })
            logger.warning(
                "Product unresolved: '%s' (confidence=%s, score=%.2f, candidates=%s)",
                item["base_flavor"], result.confidence,
                result.score, result.candidates[:3],
            )

    return resolved, alerts
