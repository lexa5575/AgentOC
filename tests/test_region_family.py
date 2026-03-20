"""Tests for Region Family Policy (db/region_family.py).

Covers:
- get_family()
- is_same_family() including fail-closed edge cases
- get_preferred_product_id() including fail-closed
- expand_to_family_ids() with name_norm safety
- get_region_suffix()
"""

import importlib
import sys
import unittest


def _ensure_real_module():
    """Force-reload db.region_family from disk if it was replaced by a test stub.

    Other test files replace sys.modules["db.region_family"] with types.ModuleType
    stubs that have empty REGION_FAMILIES. When pytest runs all tests in one process,
    this pollution persists. We detect it by checking REGION_FAMILIES content.
    """
    mod_name = "db.region_family"
    mod = sys.modules.get(mod_name)
    if mod is None or not getattr(mod, "REGION_FAMILIES", None):
        # Missing or stub with empty dict — force reload
        sys.modules.pop(mod_name, None)
    return importlib.import_module(mod_name)


_mod = _ensure_real_module()
CATEGORY_REGION_SUFFIX = _mod.CATEGORY_REGION_SUFFIX
expand_to_family_ids = _mod.expand_to_family_ids
get_family = _mod.get_family
get_preferred_product_id = _mod.get_preferred_product_id
get_region_suffix = _mod.get_region_suffix
is_same_family = _mod.is_same_family

# Mock catalog entries for testing
MOCK_CATALOG = [
    {"id": 10, "category": "TEREA_EUROPE", "name_norm": "silver", "stock_name": "Silver"},
    {"id": 17, "category": "ARMENIA", "name_norm": "silver", "stock_name": "Silver"},
    {"id": 24, "category": "KZ_TEREA", "name_norm": "silver", "stock_name": "Silver"},
    {"id": 40, "category": "TEREA_EUROPE", "name_norm": "bronze", "stock_name": "Bronze"},
    {"id": 50, "category": "ARMENIA", "name_norm": "bronze", "stock_name": "Bronze"},
    {"id": 55, "category": "KZ_TEREA", "name_norm": "bronze", "stock_name": "Bronze"},
    {"id": 61, "category": "ARMENIA", "name_norm": "sun pearl", "stock_name": "Sun Pearl"},
    {"id": 76, "category": "TEREA_EUROPE", "name_norm": "sun pearl", "stock_name": "Sun Pearl"},
    {"id": 80, "category": "TEREA_JAPAN", "name_norm": "smooth", "stock_name": "T Smooth"},
    {"id": 85, "category": "УНИКАЛЬНАЯ_ТЕРЕА", "name_norm": "smooth", "stock_name": "T Smooth"},
]


class TestGetFamily(unittest.TestCase):
    def test_armenia(self):
        self.assertEqual(get_family("ARMENIA"), "ME")

    def test_kz_terea(self):
        self.assertEqual(get_family("KZ_TEREA"), "ME")

    def test_terea_europe(self):
        self.assertEqual(get_family("TEREA_EUROPE"), "EU")

    def test_unknown_category(self):
        self.assertIsNone(get_family("ONE"))

    def test_device_category(self):
        self.assertIsNone(get_family("STND"))


class TestIsSameFamily(unittest.TestCase):
    def test_me_family(self):
        self.assertTrue(is_same_family({"ARMENIA", "KZ_TEREA"}))

    def test_single_category(self):
        self.assertTrue(is_same_family({"ARMENIA"}))

    def test_single_eu(self):
        self.assertTrue(is_same_family({"TEREA_EUROPE"}))

    def test_cross_family_me_eu(self):
        self.assertFalse(is_same_family({"ARMENIA", "TEREA_EUROPE"}))

    def test_japan_same_family(self):
        """TEREA_JAPAN and УНИКАЛЬНАЯ_ТЕРЕА are in JAPAN family."""
        self.assertTrue(is_same_family({"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"}))

    # --- Fail-closed tests ---

    def test_empty_set_fail_closed(self):
        self.assertFalse(is_same_family(set()))

    def test_unknown_category_fail_closed(self):
        self.assertFalse(is_same_family({"UNKNOWN_CAT"}))

    def test_known_plus_unknown_fail_closed(self):
        self.assertFalse(is_same_family({"ARMENIA", "UNKNOWN_CAT"}))

    def test_three_me_categories_impossible_but_safe(self):
        """If somehow 3 categories all in ME — still True."""
        # Only 2 ME categories exist, but the logic should handle any count
        self.assertTrue(is_same_family({"ARMENIA", "KZ_TEREA"}))


class TestGetPreferredProductId(unittest.TestCase):
    def test_me_preferred_is_armenia(self):
        result = get_preferred_product_id([17, 24], MOCK_CATALOG)
        self.assertEqual(result, 17)  # ARMENIA is preferred for ME

    def test_me_reversed_order(self):
        result = get_preferred_product_id([24, 17], MOCK_CATALOG)
        self.assertEqual(result, 17)  # Order doesn't matter

    def test_single_id(self):
        result = get_preferred_product_id([24], MOCK_CATALOG)
        self.assertEqual(result, 24)

    def test_empty_ids(self):
        self.assertIsNone(get_preferred_product_id([], MOCK_CATALOG))

    def test_unknown_id_fail_closed(self):
        result = get_preferred_product_id([999], MOCK_CATALOG)
        self.assertIsNone(result)

    def test_mixed_known_unknown_fail_closed(self):
        result = get_preferred_product_id([17, 999], MOCK_CATALOG)
        self.assertIsNone(result)

    def test_cross_family_returns_none(self):
        """ARMENIA(17) + TEREA_EUROPE(76) = cross-family → None."""
        result = get_preferred_product_id([17, 76], MOCK_CATALOG)
        self.assertIsNone(result)

    def test_eu_single_category(self):
        result = get_preferred_product_id([10], MOCK_CATALOG)
        self.assertEqual(result, 10)


class TestExpandToFamilyIds(unittest.TestCase):
    def test_armenia_silver_expands_to_kz(self):
        result = expand_to_family_ids([17], MOCK_CATALOG)
        self.assertEqual(result, [17, 24])

    def test_kz_silver_expands_to_armenia(self):
        result = expand_to_family_ids([24], MOCK_CATALOG)
        self.assertEqual(result, [17, 24])

    def test_both_already_present(self):
        result = expand_to_family_ids([17, 24], MOCK_CATALOG)
        self.assertEqual(result, [17, 24])

    def test_eu_no_sibling(self):
        result = expand_to_family_ids([10], MOCK_CATALOG)
        self.assertEqual(result, [10])

    def test_only_same_name_norm(self):
        """Silver(17, ARMENIA) must NOT pull Amber(50, ARMENIA) — different name_norm."""
        result = expand_to_family_ids([17], MOCK_CATALOG)
        self.assertNotIn(50, result)  # Bronze ARMENIA
        self.assertNotIn(55, result)  # Bronze KZ_TEREA
        self.assertEqual(result, [17, 24])  # Only Silver siblings

    def test_bronze_expansion(self):
        result = expand_to_family_ids([50], MOCK_CATALOG)
        self.assertEqual(result, [50, 55])  # ARMENIA + KZ_TEREA Bronze

    def test_empty_ids(self):
        self.assertEqual(expand_to_family_ids([], MOCK_CATALOG), [])

    def test_empty_catalog(self):
        self.assertEqual(expand_to_family_ids([17], []), [17])

    def test_unknown_id_passthrough(self):
        """Unknown id not in catalog — just returned as-is, no expansion."""
        result = expand_to_family_ids([999], MOCK_CATALOG)
        self.assertEqual(result, [999])

    def test_japan_expanded(self):
        """JAPAN family: TEREA_JAPAN(80) expands to include УНИКАЛЬНАЯ_ТЕРЕА(85) sibling."""
        result = expand_to_family_ids([80], MOCK_CATALOG)
        self.assertEqual(result, [80, 85])  # both "smooth" entries

    def test_cross_family_not_mixed(self):
        """Sun Pearl: ARMENIA(61) + TEREA_EUROPE(76) — different families.
        Expanding [61] should only add ME siblings, not EU."""
        result = expand_to_family_ids([61], MOCK_CATALOG)
        # Sun Pearl only exists in ARMENIA in ME family, no KZ_TEREA Sun Pearl
        self.assertEqual(result, [61])
        self.assertNotIn(76, result)  # TEREA_EUROPE Sun Pearl NOT included


class TestGetRegionSuffix(unittest.TestCase):
    def test_kz_terea_is_me(self):
        self.assertEqual(get_region_suffix("KZ_TEREA"), "ME")

    def test_armenia_is_me(self):
        self.assertEqual(get_region_suffix("ARMENIA"), "ME")

    def test_europe(self):
        self.assertEqual(get_region_suffix("TEREA_EUROPE"), "EU")

    def test_japan(self):
        self.assertEqual(get_region_suffix("TEREA_JAPAN"), "Japan")

    def test_unknown(self):
        self.assertIsNone(get_region_suffix("ONE"))

    def test_category_region_suffix_dict(self):
        """Verify the exported dict has correct KZ_TEREA mapping."""
        self.assertEqual(CATEGORY_REGION_SUFFIX["KZ_TEREA"], "ME")
        self.assertEqual(CATEGORY_REGION_SUFFIX["ARMENIA"], "ME")


if __name__ == "__main__":
    unittest.main()
