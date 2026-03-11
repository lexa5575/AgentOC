"""Tests for region preference resolution module (db/region_preference.py)
and OrderItem.region_preference validator (agents/models.py).

Covers:
- apply_region_preference() narrowing logic
- _family_has_warehouse_stock() per-warehouse check
- _update_region_metadata() deterministic name overwrite
- OrderItem validator normalization (string, list, dedup, garbage)
"""

import json
from unittest.mock import patch, MagicMock

from agents.models import OrderItem
from db.region_preference import apply_region_preference


# ---------------------------------------------------------------------------
# Catalog fixtures
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": 18, "category": "ARMENIA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 39, "category": "KZ_TEREA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 71, "category": "TEREA_EUROPE", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 80, "category": "TEREA_JAPAN", "name_norm": "silver", "stock_name": "T Silver"},
]


def _item(base_flavor="Turquoise", quantity=10, product_ids=None, pref=None, strict=False):
    """Helper to build an item dict for apply_region_preference."""
    return {
        "product_name": f"Terea {base_flavor}",
        "base_flavor": base_flavor,
        "quantity": quantity,
        "original_product_name": base_flavor,
        "product_ids": product_ids or [],
        "region_preference": pref,
        "strict_region": strict,
    }


# ---------------------------------------------------------------------------
# Mock stock helper
# ---------------------------------------------------------------------------

def _mock_stock(pid_stock_map):
    """Return a mock for search_stock_by_ids using pid→[(warehouse, qty)] map.

    Example: {71: [("NY", 5)], 18: [("LA", 20), ("NY", 50)]}
    """
    def _search(product_ids):
        results = []
        for pid in product_ids:
            for wh, qty in pid_stock_map.get(pid, []):
                results.append({
                    "product_id": pid,
                    "warehouse": wh,
                    "quantity": qty,
                })
        return results
    return _search


# ===================================================================
# A. Unit tests — apply_region_preference
# ===================================================================

class TestApplyRegionPreferenceNoOp:
    """Items without region_preference pass through unchanged."""

    def test_none_preference(self):
        items = [_item(product_ids=[18, 39, 71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39, 71]

    def test_single_pid_no_preference(self):
        items = [_item(product_ids=[71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    def test_single_pid_with_pref_no_metadata_overwrite(self):
        """Single pid already resolved → don't overwrite metadata even with pref."""
        item = _item(product_ids=[71], pref=["EU"])
        original_name = item["product_name"]
        result = apply_region_preference([item], catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        assert result[0]["product_name"] == original_name  # unchanged

    def test_same_family_no_preference(self):
        items = [_item(product_ids=[18, 39], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39]

    def test_cross_family_no_preference_unchanged(self):
        """Without preference, cross-family stays cross-family (existing ambiguous behavior)."""
        items = [_item(product_ids=[18, 39, 71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39, 71]


class TestSoftPreferenceWithStock:
    """Soft preference: try families in order, pick first with stock."""

    @patch("db.region_preference.search_stock_by_ids")
    def test_eu_has_stock(self, mock_search):
        mock_search.side_effect = _mock_stock({71: [("NY", 20)]})
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    @patch("db.region_preference.search_stock_by_ids")
    def test_eu_oos_me_has_stock(self, mock_search):
        mock_search.side_effect = _mock_stock({
            71: [("NY", 2)],  # EU: not enough
            18: [("LA", 65)],  # ARMENIA: enough
        })
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        # ME family = ARMENIA + KZ_TEREA
        assert set(result[0]["product_ids"]) == {18, 39}

    @patch("db.region_preference.search_stock_by_ids")
    def test_both_oos_falls_to_first_pref(self, mock_search):
        """Both EU and ME OOS → first preferred (EU) → downstream OOS flow."""
        mock_search.side_effect = _mock_stock({})  # nothing in stock
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        # original_product_name should have EU context
        assert "EU" in result[0]["original_product_name"]

    @patch("db.region_preference.search_stock_by_ids")
    def test_preference_overrides_history(self, mock_search):
        """Explicit preference picks family even when another has more stock."""
        mock_search.side_effect = _mock_stock({
            71: [("NY", 15)],  # EU: enough
            18: [("LA", 100)],  # ME: much more but not preferred
        })
        items = [_item(product_ids=[18, 39, 71], pref=["EU"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]


class TestStrictPreference:
    """Strict: only first preferred family, regardless of stock."""

    def test_strict_eu_has_stock(self):
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    def test_strict_eu_oos_no_fallback(self):
        """Strict EU OOS → product_ids=[71] → downstream OOS flow, NOT ambiguous."""
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        assert "EU" in result[0]["original_product_name"]

    def test_strict_japan_no_pids(self):
        """Strict Japan but no Japan Turquoise → empty pids → OOS flow."""
        items = [_item(product_ids=[18, 39, 71], pref=["JAPAN"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == []
        assert "Japan" in result[0]["product_name"]


class TestMetadataUpdate:
    """_update_region_metadata deterministically overwrites names."""

    def test_display_metadata_after_narrowing(self):
        items = [_item(product_ids=[18, 39, 71], pref=["ME"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert "ME" in result[0]["original_product_name"]
        assert result[0].get("display_name") is not None
        assert result[0]["display_name"] != ""

    def test_strict_eu_oos_original_name_has_suffix(self):
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["original_product_name"] == "Turquoise EU"

    def test_japan_zero_pids_synthesized(self):
        items = [_item(base_flavor="Turquoise", product_ids=[18, 39, 71], pref=["JAPAN"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_name"] == "Terea Turquoise Japan"
        assert result[0]["display_name"] == "Terea Turquoise Japan"
        assert result[0]["original_product_name"] == "Turquoise Japan"

    def test_non_terea_brand_preserved(self):
        """Non-Terea brand (e.g. ONE Green) keeps brand prefix in fallback."""
        item = {
            "product_name": "ONE Green",
            "base_flavor": "Green",
            "quantity": 5,
            "original_product_name": "ONE Green",
            "product_ids": [18, 39, 71],  # cross-family
            "region_preference": ["JAPAN"],
            "strict_region": True,
        }
        result = apply_region_preference([item], catalog_entries=CATALOG)
        # JAPAN has no pids → fallback synthesis from "ONE Green" + " Japan"
        assert result[0]["product_ids"] == []
        assert result[0]["product_name"] == "ONE Green Japan"
        assert "Terea" not in result[0]["product_name"]

    def test_stale_display_name_overwritten(self):
        """Old generic display_name gets overwritten after apply."""
        item = _item(product_ids=[18, 39, 71], pref=["ME"], strict=True)
        item["display_name"] = "Terea Turquoise"  # stale generic
        result = apply_region_preference([item], catalog_entries=CATALOG)
        assert result[0]["display_name"] != "Terea Turquoise"
        assert "ME" in result[0]["display_name"] or "Middle East" in result[0]["display_name"]


# ===================================================================
# B. Validator tests — OrderItem.region_preference
# ===================================================================

class TestRegionPreferenceValidator:
    """OrderItem.normalize_region_preference validator."""

    def test_lowercase_normalized(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["eu", "me"])
        assert oi.region_preference == ["EU", "ME"]

    def test_japan_alias(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["japan"])
        assert oi.region_preference == ["JAPAN"]

    def test_invalid_dropped(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["blah", "eu"])
        assert oi.region_preference == ["EU"]

    def test_all_invalid_becomes_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["unknown"])
        assert oi.region_preference is None

    def test_null_stays_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=None)
        assert oi.region_preference is None

    def test_absent_defaults_none(self):
        oi = OrderItem(product_name="X", base_flavor="X")
        assert oi.region_preference is None

    def test_string_input_wrapped(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference="eu")
        assert oi.region_preference == ["EU"]

    def test_duplicates_removed(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["EU", "eu", "EU"])
        assert oi.region_preference == ["EU"]

    def test_int_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=123)
        assert oi.region_preference is None

    def test_bool_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=True)
        assert oi.region_preference is None

    def test_dict_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference={"region": "EU"})
        assert oi.region_preference is None

    def test_europe_alias(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["europe"])
        assert oi.region_preference == ["EU"]

    def test_order_preserved(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["me", "eu", "japan"])
        assert oi.region_preference == ["ME", "EU", "JAPAN"]


# ===================================================================
# C. Classifier parsing test — run_classification → OrderItem with region fields
# ===================================================================

class TestClassifierParsingRegionFields:
    """Verify that run_classification() correctly parses region_preference
    and strict_region from LLM JSON output into OrderItem fields."""

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_region_fields_parsed_from_llm_json(self, _clean, _parse, mock_agent):
        """LLM returns JSON with region_preference → OrderItem has normalized fields."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Turquoise",
                    "base_flavor": "Turquoise",
                    "quantity": 10,
                    "region_preference": ["eu", "me"],
                    "strict_region": False,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        assert result.order_items is not None
        assert len(result.order_items) == 1
        oi = result.order_items[0]
        assert oi.region_preference == ["EU", "ME"]
        assert oi.strict_region is False

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_null_region_fields_parsed(self, _clean, _parse, mock_agent):
        """LLM returns null region_preference → OrderItem has None."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Silver",
                    "base_flavor": "Silver",
                    "quantity": 5,
                    "region_preference": None,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        oi = result.order_items[0]
        assert oi.region_preference is None
        assert oi.strict_region is False  # default

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_missing_region_fields_use_defaults(self, _clean, _parse, mock_agent):
        """LLM omits region fields entirely → defaults (None, False)."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Green",
                    "base_flavor": "Green",
                    "quantity": 2,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        oi = result.order_items[0]
        assert oi.region_preference is None
        assert oi.strict_region is False
