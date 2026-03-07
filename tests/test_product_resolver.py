"""Unit tests for Product Name Resolver.

Tests:
- Exact match (case-insensitive, after normalization)
- High confidence typo auto-correction ("Sillver" → "Silver")
- Medium confidence (ambiguous) → no auto-resolve, alert
- Low confidence (no match) → no auto-resolve, alert
- Device model standalone ("ONE", "STND", "PRIME") → exact
- Brand prefix stripping ("Tera Green" → matches "Green")
- Region suffix stripping ("Silver EU" → matches "Silver")
- LLM fallback for medium confidence cases
- Batch resolver (resolve_order_items) integration
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Direct import — no DB access needed since we pass known_names explicitly
from db.product_resolver import (
    ResolveResult,
    _extract_region_categories,
    _normalize,
    _resolve_via_alias,
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

    # --- Region prefix stripping (Region Safety hotfix) ---

    def test_strips_region_prefix_eu(self):
        self.assertEqual(_normalize("EU Silver"), "Silver")

    def test_strips_region_prefix_european(self):
        self.assertEqual(_normalize("European Bronze"), "Bronze")

    def test_strips_region_prefix_japan(self):
        self.assertEqual(_normalize("Japan Smooth"), "Smooth")

    def test_strips_region_prefix_japanese(self):
        self.assertEqual(_normalize("Japanese Smooth"), "Smooth")

    def test_strips_region_prefix_me(self):
        self.assertEqual(_normalize("ME Amber"), "Amber")

    def test_strips_region_prefix_kz(self):
        self.assertEqual(_normalize("KZ Silver"), "Silver")

    # --- Stabilization: additional prefix stripping ---

    def test_strips_region_prefix_middle_east(self):
        self.assertEqual(_normalize("Middle East Amber"), "Amber")

    def test_strips_region_prefix_armenia(self):
        self.assertEqual(_normalize("Armenia Silver"), "Silver")

    def test_strips_region_prefix_armenian(self):
        self.assertEqual(_normalize("Armenian Bronze"), "Bronze")

    def test_strips_region_prefix_europe(self):
        self.assertEqual(_normalize("Europe Bronze"), "Bronze")


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
        """'SUMMER BREEZE' matches 'Summer' via alias (Tier 1) or word-prefix."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("SUMMER BREEZE", known)
        self.assertIn(r.confidence, ("exact", "high"))  # alias → exact, word-prefix → high
        self.assertEqual(r.resolved, "Summer")

    def test_word_prefix_with_brand(self):
        """'Tera SUMMER BREEZE' → alias or word-prefix → 'Summer'."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Tera SUMMER BREEZE", known)
        self.assertIn(r.confidence, ("exact", "high"))
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


class TestAliasLookup(unittest.TestCase):
    """Test Tier 1: alias-based deterministic matching."""

    def test_alias_abbreviation_pw(self):
        """'pw' → 'Purple Wave' via alias."""
        known = KNOWN_NAMES  # has "Purple Wave"
        r = resolve_product_name("pw", known)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Purple Wave")

    def test_alias_abbreviation_purple_w(self):
        """'purple w' → 'Purple Wave' via alias."""
        r = resolve_product_name("purple w", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Purple Wave")

    def test_alias_tourquoise(self):
        """'tourquoise' → 'Turquoise' via alias (multi-char typo)."""
        r = resolve_product_name("tourquoise", KNOWN_NAMES)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Turquoise")

    def test_alias_summer_breeze(self):
        """'summer breeze' → 'Summer' via alias (site pattern)."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("summer breeze", known)
        self.assertEqual(r.confidence, "exact")
        self.assertEqual(r.resolved, "Summer")

    def test_alias_not_in_known_names_skipped(self):
        """Alias target not in known_names → fall through to other tiers."""
        # "Summer" is not in base KNOWN_NAMES
        r = resolve_product_name("summer breeze", KNOWN_NAMES)
        # Should fall through since "Summer" isn't available
        self.assertNotEqual(r.resolved, "Summer")

    def test_resolve_via_alias_function(self):
        """Direct test of _resolve_via_alias."""
        self.assertEqual(_resolve_via_alias("pw"), "Purple Wave")
        self.assertEqual(_resolve_via_alias("Tera tourquoise"), "Turquoise")
        self.assertIsNone(_resolve_via_alias("Silver"))  # not an alias


class TestExtractRegionCategories(unittest.TestCase):
    """Test region detection from product names."""

    def test_made_in_europe(self):
        cats = _extract_region_categories("Tera AMBER made in Europe")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))

    def test_eu_suffix(self):
        cats = _extract_region_categories("Tera Silver EU")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))

    def test_made_in_middle_east(self):
        cats = _extract_region_categories("Green made in Middle East")
        self.assertEqual(cats, frozenset({"ARMENIA", "KZ_TEREA"}))

    def test_made_in_armenia(self):
        cats = _extract_region_categories("Terea Purple made in Armenia")
        self.assertEqual(cats, frozenset({"ARMENIA"}))

    def test_japan(self):
        cats = _extract_region_categories("Purple Japan")
        self.assertEqual(cats, frozenset({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}))

    def test_kz(self):
        cats = _extract_region_categories("Amber KZ")
        self.assertEqual(cats, frozenset({"KZ_TEREA"}))

    def test_me_suffix(self):
        cats = _extract_region_categories("Tera Turquoise ME")
        self.assertEqual(cats, frozenset({"ARMENIA", "KZ_TEREA"}))

    def test_no_region(self):
        cats = _extract_region_categories("Silver")
        self.assertIsNone(cats)

    def test_no_region_with_brand(self):
        cats = _extract_region_categories("Tera Silver")
        self.assertIsNone(cats)

    # --- Prefix formats (Region Safety hotfix) ---

    def test_eu_prefix(self):
        cats = _extract_region_categories("EU Silver")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))

    def test_european_prefix(self):
        cats = _extract_region_categories("European Bronze")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))

    def test_japan_prefix(self):
        cats = _extract_region_categories("Japan Smooth")
        self.assertEqual(cats, frozenset({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}))

    def test_japanese_prefix(self):
        cats = _extract_region_categories("Japanese Smooth")
        self.assertEqual(cats, frozenset({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}))

    def test_me_prefix(self):
        cats = _extract_region_categories("ME Amber")
        self.assertEqual(cats, frozenset({"ARMENIA", "KZ_TEREA"}))

    def test_kz_prefix(self):
        cats = _extract_region_categories("KZ Silver")
        self.assertEqual(cats, frozenset({"KZ_TEREA"}))

    def test_eu_prefix_with_brand(self):
        cats = _extract_region_categories("Tera EU Bronze")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))

    # --- Stabilization: additional prefix formats ---

    def test_middle_east_prefix(self):
        cats = _extract_region_categories("Middle East Amber")
        self.assertEqual(cats, frozenset({"ARMENIA", "KZ_TEREA"}))

    def test_armenian_prefix(self):
        cats = _extract_region_categories("Armenian Bronze")
        self.assertEqual(cats, frozenset({"ARMENIA"}))

    def test_armenia_prefix(self):
        cats = _extract_region_categories("Armenia Silver")
        self.assertEqual(cats, frozenset({"ARMENIA"}))

    def test_europe_prefix(self):
        cats = _extract_region_categories("Europe Bronze")
        self.assertEqual(cats, frozenset({"TEREA_EUROPE"}))


class TestCatalogRegionFiltering(unittest.TestCase):
    """Test region-aware product_id filtering in resolve_product_to_catalog."""

    def _make_catalog(self):
        """Build a catalog with same flavor in multiple categories."""
        return [
            {"id": 10, "category": "TEREA_EUROPE", "name_norm": "silver", "stock_name": "Silver"},
            {"id": 20, "category": "ARMENIA", "name_norm": "silver", "stock_name": "Silver"},
            {"id": 30, "category": "KZ_TEREA", "name_norm": "silver", "stock_name": "Silver"},
            {"id": 40, "category": "TEREA_EUROPE", "name_norm": "bronze", "stock_name": "Bronze"},
            {"id": 50, "category": "ARMENIA", "name_norm": "bronze", "stock_name": "Bronze"},
            {"id": 60, "category": "TEREA_JAPAN", "name_norm": "smooth", "stock_name": "T Smooth"},
        ]

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_silver_eu_filters_to_europe_only(self):
        """'Silver EU' should only return TEREA_EUROPE product_ids."""
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog("Silver EU", catalog)
        self.assertIn(result.confidence, ("exact", "high"))
        self.assertEqual(result.product_ids, [10])  # Only TEREA_EUROPE

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_raw_name_has_region_original_without(self):
        """raw_name='Bronze EU', original_product_name='Bronze' → region from raw_name."""
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog(
            "Bronze EU", catalog, original_product_name="Bronze",
        )
        self.assertIn(result.confidence, ("exact", "high"))
        self.assertEqual(result.product_ids, [40])  # Only TEREA_EUROPE

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_prefix_eu_silver_filters_correctly(self):
        """'EU Silver' (prefix format) should filter to TEREA_EUROPE."""
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog("EU Silver", catalog)
        self.assertIn(result.confidence, ("exact", "high"))
        self.assertEqual(result.product_ids, [10])  # Only TEREA_EUROPE

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_no_region_returns_all_categories(self):
        """'Silver' (no region) returns product_ids from all categories."""
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog("Silver", catalog)
        self.assertIn(result.confidence, ("exact", "high"))
        self.assertEqual(sorted(result.product_ids), [10, 20, 30])

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_silver_me_display_name_shows_region(self):
        """'Silver ME' matches ARMENIA+KZ_TEREA — both map to 'ME' display suffix.

        display_name should be 'Terea Silver ME', not generic 'Terea Silver'.
        """
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog("Silver ME", catalog)
        self.assertIn(result.confidence, ("exact", "high"))
        self.assertEqual(sorted(result.product_ids), [20, 30])
        self.assertEqual(result.display_name, "Terea Silver ME")

    @patch("db.product_resolver.USE_CATALOG_RESOLVER", True)
    def test_no_region_display_name_generic(self):
        """'Silver' (no region) matches 3 categories with different suffixes.

        display_name should be generic 'Terea Silver' (no region).
        """
        from db.product_resolver import resolve_product_to_catalog
        catalog = self._make_catalog()
        result = resolve_product_to_catalog("Silver", catalog)
        self.assertEqual(result.display_name, "Terea Silver")


class TestLLMFallback(unittest.TestCase):
    """Test LLM fallback for medium confidence cases."""

    def _mock_openai_response(self, content: str):
        """Create a mock OpenAI client that returns given content."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=content))
        ]
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    @patch("db.product_resolver.USE_LLM_RESOLVER", True)
    @patch("openai.OpenAI")
    def test_llm_resolves_medium_confidence(self, mock_openai_cls):
        """Medium confidence item → LLM returns correct match → high confidence."""
        mock_openai_cls.return_value = self._mock_openai_response("Summer")
        # "Breeze Summer" (reversed) — fuzzy gives medium, word-prefix won't catch
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Breeze Summer", known)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.resolved, "Summer")
        self.assertAlmostEqual(r.score, 0.85)

    @patch("db.product_resolver.USE_LLM_RESOLVER", True)
    @patch("openai.OpenAI")
    def test_llm_returns_garbage_falls_to_medium(self, mock_openai_cls):
        """LLM returns something not in known_names → medium confidence (operator alert)."""
        mock_openai_cls.return_value = self._mock_openai_response("NotARealProduct")
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Breeze Summer", known)
        self.assertEqual(r.confidence, "medium")
        self.assertIsNone(r.resolved)

    @patch("db.product_resolver.USE_LLM_RESOLVER", True)
    @patch("openai.OpenAI")
    def test_llm_api_error_falls_to_medium(self, mock_openai_cls):
        """OpenAI API error → graceful fallback to medium confidence."""
        mock_openai_cls.side_effect = Exception("API timeout")
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Breeze Summer", known)
        self.assertEqual(r.confidence, "medium")
        self.assertIsNone(r.resolved)

    @patch("db.product_resolver.USE_LLM_RESOLVER", False)
    def test_llm_disabled_skips_to_medium(self):
        """USE_LLM_RESOLVER=false → no LLM call, medium confidence."""
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Breeze Summer", known)
        self.assertEqual(r.confidence, "medium")
        self.assertIsNone(r.resolved)

    @patch("db.product_resolver.USE_LLM_RESOLVER", True)
    @patch("openai.OpenAI")
    def test_llm_returns_none_falls_to_medium(self, mock_openai_cls):
        """LLM returns 'NONE' → medium confidence (operator alert)."""
        mock_openai_cls.return_value = self._mock_openai_response("NONE")
        known = KNOWN_NAMES + ["Summer"]
        r = resolve_product_name("Breeze Summer", known)
        self.assertEqual(r.confidence, "medium")
        self.assertIsNone(r.resolved)


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
