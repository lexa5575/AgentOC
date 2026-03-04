"""
Product Name Resolver
---------------------

Deterministic fuzzy matching for misspelled product names.
Uses difflib.SequenceMatcher (stdlib) — no new dependencies.

Confidence levels:
- exact:  case-insensitive exact match after normalization
- high:   score >= 0.80 with clear winner (gap >= 0.15 to second)
- medium: score >= 0.55 but ambiguous or borderline
- low:    score < 0.55 — no reasonable match

Usage:
    resolved_items, alerts = resolve_order_items(items)
    # resolved_items: items with auto-corrected names (exact/high)
    # alerts: list of unresolved items (medium/low) for operator attention
"""

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Brand prefixes to strip before matching (mirrors email_parser logic)
_BRAND_PREFIXES = ("Tera ", "Terea ", "Heets ")

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


@dataclass
class ResolveResult:
    """Result of resolving a single product name."""

    original: str
    resolved: str | None
    confidence: str  # "exact", "high", "medium", "low"
    score: float
    candidates: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Normalize product name: strip brand prefixes and region suffixes."""
    name = name.strip()
    for prefix in _BRAND_PREFIXES:
        if name.startswith(prefix):
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
    for prefix in _BRAND_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name_lower = name.lower()
    return any(name_lower.endswith(s) for s in _ORIGIN_SUFFIXES)


# ---------------------------------------------------------------------------
# Known names from stock DB
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
# Single name resolver
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

    normalized = _normalize(raw_name)
    normalized_lower = normalized.lower()

    # 1. Exact match (case-insensitive, after normalization)
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

    # 2. Device model-only: "ONE", "STND", "PRIME" — valid without color
    if normalized.upper() in _DEVICE_MODELS:
        return ResolveResult(raw_name, normalized.upper(), "exact", 1.0, [normalized.upper()])

    # 3. Fuzzy match via SequenceMatcher
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
    if best_score >= 0.55:
        return ResolveResult(raw_name, None, "medium", best_score, top_candidates)
    return ResolveResult(raw_name, None, "low", best_score, top_candidates)


# ---------------------------------------------------------------------------
# Batch resolver for order items
# ---------------------------------------------------------------------------

def resolve_order_items(
    items: list[dict],
    known_names: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve product names in a list of order items.

    Args:
        items: List of dicts with keys: base_flavor, product_name, quantity.
        known_names: Optional pre-fetched known names (avoids repeated DB calls).

    Returns:
        (resolved_items, alerts):
        - resolved_items: items with auto-corrected names (exact/high confidence)
        - alerts: list of unresolved items for operator attention
    """
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
