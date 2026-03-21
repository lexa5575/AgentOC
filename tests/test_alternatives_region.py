"""Tests for region-aware alternative selection in db.alternatives."""

import pytest

pytestmark = pytest.mark.domain_stock

import unittest
from unittest.mock import patch, MagicMock


def _item(product_name: str, category: str, qty: int = 5, product_id: int = 1) -> dict:
    return {
        "product_name": product_name,
        "category": category,
        "quantity": qty,
        "warehouse": "LA_MAKS",
        "is_fallback": False,
        "synced_at": None,
        "product_id": product_id,
        "flavor_family": "classic",
    }


# Available stock: mix of JAPAN and ME items
_JAPAN_ITEMS = [
    _item("Tropical", "TEREA_JAPAN", qty=8, product_id=10),
    _item("Regular", "TEREA_JAPAN", qty=5, product_id=11),
]
_ME_ITEMS = [
    _item("Silver", "ARMENIA", qty=20, product_id=20),
    _item("Bronze", "KZ_TEREA", qty=15, product_id=21),
]
_EU_ITEMS = [
    _item("Green", "TEREA_EUROPE", qty=12, product_id=30),
]
_ALL_ITEMS = _JAPAN_ITEMS + _ME_ITEMS + _EU_ITEMS


def _base_patches():
    """Return dict of common patches for all tests."""
    return {
        "avail": patch("db.alternatives._get_available_items"),
        "ptype": patch("db.alternatives.get_product_type", return_value="STICKS"),
        "cats": patch("db.alternatives._get_allowed_categories",
                       return_value={"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА", "ARMENIA", "KZ_TEREA", "TEREA_EUROPE"}),
        "resolve": patch("db.product_resolver.resolve_product_to_catalog"),
        "history": patch("db.alternatives.get_client_flavor_history", return_value=[]),
        "session": patch("db.alternatives.get_session"),
        "equiv": patch("db.catalog.get_equivalent_norms", return_value={"amber"}),
        "normalize": patch("db.product_resolver._normalize", side_effect=lambda x: x),
        "extract_rc": patch("db.product_resolver._extract_region_categories", return_value=set()),
        "llm": patch("agents.alternatives.get_llm_alternatives"),
    }


class TestRegionPreferenceStrict(unittest.TestCase):
    """strict_region=True + region_preference → only preferred region categories."""

    def test_japan_strict_returns_only_japan(self):
        """JAPAN + strict=True → only TEREA_JAPAN / УНИКАЛЬНАЯ_ТЕРЕА items returned."""
        patches = _base_patches()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            mocks["avail"].return_value = list(_ALL_ITEMS)
            mocks["resolve"].return_value = MagicMock(product_ids=[99], display_name="Amber")
            mocks["llm"].return_value = list(_JAPAN_ITEMS)

            from db.alternatives import select_best_alternatives
            result = select_best_alternatives(
                client_email="test@example.com",
                base_flavor="Amber",
                region_preference=["JAPAN"],
                strict_region=True,
            )

            alternatives = result["alternatives"]
            self.assertTrue(len(alternatives) > 0, "Should return at least one alternative")
            for alt in alternatives:
                cat = alt["alternative"]["category"]
                self.assertIn(
                    cat,
                    {"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"},
                    f"strict_region=True but got category {cat}",
                )
        finally:
            for p in patches.values():
                p.stop()

    def test_japan_strict_no_stock_returns_empty(self):
        """JAPAN + strict=True + no JAPAN stock → empty result with reason=region_unavailable."""
        patches = _base_patches()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            # Only ME and EU items available, no Japan
            mocks["avail"].return_value = list(_ME_ITEMS + _EU_ITEMS)
            mocks["resolve"].return_value = MagicMock(product_ids=[99], display_name="Amber")

            from db.alternatives import select_best_alternatives
            result = select_best_alternatives(
                client_email="test@example.com",
                base_flavor="Amber",
                region_preference=["JAPAN"],
                strict_region=True,
            )

            self.assertEqual(result["alternatives"], [])
            self.assertEqual(result["reason"], "region_unavailable")
        finally:
            for p in patches.values():
                p.stop()


class TestRegionPreferenceSoft(unittest.TestCase):
    """strict_region=False + region_preference → fallback allowed."""

    def test_japan_soft_no_japan_stock_falls_back(self):
        """JAPAN + strict=False + no JAPAN stock → fallback to other regions (non-empty)."""
        patches = _base_patches()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            # Only ME items available, no Japan
            mocks["avail"].return_value = list(_ME_ITEMS)
            mocks["resolve"].return_value = MagicMock(product_ids=[99], display_name="Amber")
            mocks["llm"].return_value = list(_ME_ITEMS)

            from db.alternatives import select_best_alternatives
            result = select_best_alternatives(
                client_email="test@example.com",
                base_flavor="Amber",
                region_preference=["JAPAN"],
                strict_region=False,
            )

            alternatives = result["alternatives"]
            self.assertTrue(len(alternatives) > 0, "Soft mode should allow fallback to other regions")
            self.assertNotEqual(result["reason"], "region_unavailable")
        finally:
            for p in patches.values():
                p.stop()


class TestStrictPostFilter(unittest.TestCase):
    """strict_region=True → post-filter drops cross-region LLM results."""

    def test_strict_post_filter_drops_cross_region(self):
        """LLM returns ME item for JAPAN strict → post-filter must remove it."""
        patches = _base_patches()
        mocks = {k: p.start() for k, p in patches.items()}
        try:
            # Mix of Japan and ME items available
            japan_item = {"category": "TEREA_JAPAN", "product_name": "T Lemon", "quantity": 5, "flavor_family": "citrus"}
            me_item = {"category": "ARMENIA", "product_name": "Yellow", "quantity": 30, "flavor_family": "citrus"}
            mocks["avail"].return_value = [japan_item, me_item]
            mocks["resolve"].return_value = MagicMock(product_ids=[99], display_name="Tropical")
            # LLM returns both (cross-region mistake)
            mocks["llm"].return_value = [me_item, japan_item]

            from db.alternatives import select_best_alternatives
            result = select_best_alternatives(
                client_email="test@example.com",
                base_flavor="Tropical",
                region_preference=["JAPAN"],
                strict_region=True,
            )

            alternatives = result["alternatives"]
            # ME item must be filtered out by post-filter
            for alt in alternatives:
                cat = alt["alternative"]["category"]
                self.assertIn(
                    cat, {"TEREA_JAPAN", "УНИКАЛЬНАЯ_ТЕРЕА"},
                    f"strict JAPAN mode returned non-JAPAN category: {cat}",
                )
        finally:
            for p in patches.values():
                p.stop()


if __name__ == "__main__":
    unittest.main()
