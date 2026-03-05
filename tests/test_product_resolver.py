"""Unit tests for Product Name Resolver.

Tests:
- Exact match (case-insensitive, after normalization)
- High confidence typo auto-correction ("Sillver" → "Silver")
- Medium confidence (ambiguous) → no auto-resolve, alert
- Low confidence (no match) → no auto-resolve, alert
- Device model standalone ("ONE", "STND", "PRIME") → exact
- Brand prefix stripping ("Tera Green" → matches "Green")
- Region suffix stripping ("Silver EU" → matches "Silver")
- Batch resolver (resolve_order_items) integration
"""

import unittest

# Direct import — no DB access needed since we pass known_names explicitly
from db.product_resolver import (
    ResolveResult,
    _normalize,
    resolve_order_items,
    resolve_product_name,
)


# Known product names (simulating stock DB)
KNOWN_NAMES = [
    "Amber",
    "Bronze",
    "Green",
    "Purple Wave",
    "Silver",
    "Turquoise",
    "Yellow",
]


class TestNormalize(unittest.TestCase):
    """Test the _normalize helper."""

    def test_strips_brand_prefix_tera(self):
        self.assertEqual(_normalize("Tera Green"), "Green")

    def test_strips_brand_prefix_terea(self):
        self.assertEqual(_normalize("Terea Silver"), "Silver")

    def test_strips_brand_prefix_heets(self):
        self.assertEqual(_normalize("Heets Amber"), "Amber")

    def test_strips_region_suffix_eu(self):
        self.assertEqual(_normalize("Silver EU"), "Silver")

    def test_strips_region_suffix_japan(self):
        self.assertEqual(_normalize("Green Japan"), "Green")

    def test_strips_region_suffix_kz(self):
        self.assertEqual(_normalize("Amber KZ"), "Amber")

    def test_strips_region_suffix_made_in(self):
        self.assertEqual(_normalize("Green made in Middle East"), "Green")

    def test_strips_region_suffix_made_in_armenia(self):
        self.assertEqual(_normalize("Turquoise made in Armenia"), "Turquoise")

    def test_strips_both_prefix_and_suffix(self):
        # Only brand prefix is stripped (suffix applied to result)
        self.assertEqual(_normalize("Terea Green made in Armenia"), "Green")

    def test_no_change_when_no_prefix_or_suffix(self):
        self.assertEqual(_normalize("Silver"), "Silver")

    def test_strips_whitespace(self):
        self.assertEqual(_normalize("  Green  "), "Green")


class TestResolveProductName(unittest.TestCase):
    """Test single product name resolution."""

    # --- Exact matches ---

    def test_exact_match_same_case(self):
        r = resolve_product_name("Silver", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Silver")
        self.assertAlmostEqual(r.score, 1.0)

    def test_exact_match_case_insensitive(self):
        r = resolve_product_name("silver", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Silver")

    def test_exact_match_with_brand_prefix(self):
        r = resolve_product_name("Terea Silver", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Silver")

    def test_exact_match_with_region_suffix(self):
        r = resolve_product_name("Green EU", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Green")

    def test_exact_match_full_product_name(self):
        r = resolve_product_name("Terea Green made in Middle East", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Green")

    # --- Device models ---

    def test_device_model_one(self):
        r = resolve_product_name("ONE", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "ONE")

    def test_device_model_stnd(self):
        r = resolve_product_name("stnd", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "STND")

    def test_device_model_prime(self):
        r = resolve_product_name("Prime", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "PRIME")

    # --- High confidence (typo auto-correction) ---

    def test_high_confidence_typo_sillver(self):
        """'Sillver' is close to 'Silver' → high confidence auto-resolve."""
        r = resolve_product_name("Sillver", KNOWN_NAMES)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Silver")
        self.assertGreaterEqual(r.score, 0.80)

    def test_high_confidence_typo_greem(self):
        """'Greem' is close to 'Green' → high confidence."""
        r = resolve_product_name("Greem", KNOWN_NAMES)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Green")

    def test_high_confidence_typo_amberr(self):
        """'Amberr' is close to 'Amber' → high confidence."""
        r = resolve_product_name("Amberr", KNOWN_NAMES)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Amber")

    def test_high_confidence_typo_turquise(self):
        """'Turquise' is close to 'Turquoise' → high confidence."""
        r = resolve_product_name("Turquise", KNOWN_NAMES)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Turquoise")

    # --- Word-prefix match (site names with extra words) ---

    def test_word_prefix_summer_breeze(self):
        """'SUMMER BREEZE' matches 'Summer' via word-prefix (site adds 'Breeze')."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("SUMMER BREEZE", known)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Summer")

    def test_word_prefix_with_brand(self):
        """'Tera SUMMER BREEZE' → strip 'Tera', then word-prefix 'SUMMER' → 'Summer'."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Tera SUMMER BREEZE", known)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Summer")

    def test_word_prefix_purple_wave_no_false_positive(self):
        """'Purple Wave' is an exact match — should NOT trigger word-prefix for 'Purple'."""
        r = resolve_product_name("Purple Wave", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Purple Wave")

    def test_word_prefix_single_word_no_trigger(self):
        """Single-word name should NOT trigger word-prefix (goes to fuzzy)."""
        r = resolve_product_name("Sillver", KNOWN_NAMES)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Silver")

    # --- Low confidence (no reasonable match) ---

    def test_low_confidence_gibberish(self):
        """Completely unrelated name → low confidence, no resolve."""
        r = resolve_product_name("XyzFooBar123", KNOWN_NAMES)
        self.assertEqual(r.confidence, "low")
        self.assertIsNone(r.resolved)
        self.assertLess(r.score, 0.55)

    # --- Empty known names ---

    def test_empty_known_names(self):
        """No known names → pass through unchanged."""
        r = resolve_product_name("Silver", [])
        self.assertEqual(r.confidence, "low")
        self.assertEqual(r.resolved, "Silver")
        self.assertAlmostEqual(r.score, 0.0)


class TestResolveOrderItems(unittest.TestCase):
    """Test batch resolver for order items."""

    def test_exact_match_passthrough(self):
        """Exact matches pass through unchanged."""
        items = [
            {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
            {"base_flavor": "Green", "product_name": "Green", "quantity": 5},
        ]
        resolved, alerts = resolve_order_items(items, KNOWN_NAMES)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(len(alerts), 0)
        self.assertEqual(resolved[0]["base_flavor"], "Silver")
        self.assertEqual(resolved[1]["base_flavor"], "Green")

    def test_typo_auto_corrected(self):
        """High confidence typo → auto-corrected in resolved items."""
        items = [
            {"base_flavor": "Sillver", "product_name": "Sillver", "quantity": 3},
        ]
        resolved, alerts = resolve_order_items(items, KNOWN_NAMES)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(alerts), 0)
        self.assertEqual(resolved[0]["base_flavor"], "Silver")
        self.assertEqual(resolved[0]["product_name"], "Silver")

    def test_unresolved_item_in_alerts(self):
        """Low confidence item → unchanged in resolved, added to alerts."""
        items = [
            {"base_flavor": "XyzFoo", "product_name": "XyzFoo", "quantity": 1},
        ]
        resolved, alerts = resolve_order_items(items, KNOWN_NAMES)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["base_flavor"], "XyzFoo")  # unchanged
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["original"], "XyzFoo")
        self.assertIn(alerts[0]["confidence"], ("medium", "low"))

    def test_mixed_items(self):
        """Mix of exact, typo, and unresolved items."""
        items = [
            {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
            {"base_flavor": "Greem", "product_name": "Greem", "quantity": 5},
            {"base_flavor": "XyzFoo", "product_name": "XyzFoo", "quantity": 1},
        ]
        resolved, alerts = resolve_order_items(items, KNOWN_NAMES)
        self.assertEqual(len(resolved), 3)
        # Silver — exact, unchanged
        self.assertEqual(resolved[0]["base_flavor"], "Silver")
        # Greem → Green (auto-corrected)
        self.assertEqual(resolved[1]["base_flavor"], "Green")
        # XyzFoo — unchanged (unresolved)
        self.assertEqual(resolved[2]["base_flavor"], "XyzFoo")
        # Only XyzFoo in alerts
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["original"], "XyzFoo")

    def test_brand_prefix_stripped_for_matching(self):
        """'Terea Silver' matches 'Silver' after normalization."""
        items = [
            {"base_flavor": "Terea Silver", "product_name": "Terea Silver", "quantity": 2},
        ]
        resolved, alerts = resolve_order_items(items, KNOWN_NAMES)
        self.assertEqual(len(alerts), 0)
        self.assertEqual(resolved[0]["base_flavor"], "Silver")

    def test_empty_items(self):
        """Empty items list → empty results."""
        resolved, alerts = resolve_order_items([], KNOWN_NAMES)
        self.assertEqual(resolved, [])
        self.assertEqual(alerts, [])

    def test_empty_known_names(self):
        """No known names → items pass through unchanged."""
        items = [
            {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
        ]
        resolved, alerts = resolve_order_items(items, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["base_flavor"], "Silver")
        self.assertEqual(len(alerts), 0)


if __name__ == "__main__":
    unittest.main()
