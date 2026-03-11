"""Tests for pipeline integration with region_preference.

Verifies that process_classified_email correctly applies region preference
to narrow product_ids and prevent ambiguous blocks when preference is explicit.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import agents.pipeline  # noqa: F401 — ensure initial load

from agents.models import EmailClassification, OrderItem


def _process(classification, gmail_message_id=None):
    return sys.modules["agents.pipeline"].process_classified_email(
        classification, gmail_message_id=gmail_message_id,
    )


def _make_classification(**kwargs):
    defaults = {
        "needs_reply": True,
        "situation": "new_order",
        "client_email": "region@example.com",
        "parser_used": False,
    }
    defaults.update(kwargs)
    return EmailClassification(**defaults)


# ── Mocks ────────────────────────────────────────────────────────────

_CLIENT = {
    "payment_type": "prepay",
    "name": "Test User",
    "discount_percent": 0,
    "discount_orders_left": 0,
    "zelle_address": "pay@zelle.com",
}

_CATALOG = [
    {"id": 18, "category": "ARMENIA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 39, "category": "KZ_TEREA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 71, "category": "TEREA_EUROPE", "name_norm": "turquoise", "stock_name": "T Turquoise"},
]


def _resolve_cross_family(items, **kw):
    """Mock resolve: returns cross-family product_ids."""
    for it in items:
        it["product_ids"] = [18, 39, 71]
    return items, []


def _stock_all_in(items):
    """Mock stock check: all in stock."""
    return {
        "all_in_stock": True,
        "items": [
            {
                "base_flavor": it["base_flavor"],
                "product_name": it["product_name"],
                "ordered_qty": it["quantity"],
                "total_available": 100,
                "is_sufficient": True,
            }
            for it in items
        ],
        "insufficient_items": [],
    }


def _stock_none(items):
    """Mock stock check: nothing in stock."""
    return {
        "all_in_stock": False,
        "items": [
            {
                "base_flavor": it["base_flavor"],
                "product_name": it["product_name"],
                "ordered_qty": it["quantity"],
                "total_available": 0,
                "is_sufficient": False,
                "original_product_name": it.get("original_product_name"),
            }
            for it in items
        ],
        "insufficient_items": [
            {
                "base_flavor": it["base_flavor"],
                "product_name": it["product_name"],
                "ordered_qty": it["quantity"],
                "total_available": 0,
                "original_product_name": it.get("original_product_name"),
            }
            for it in items
        ],
    }


# Mock for region_preference: EU has stock
def _mock_stock_eu_ok(product_ids):
    results = []
    for pid in product_ids:
        if pid == 71:  # EU
            results.append({"product_id": 71, "warehouse": "NY", "quantity": 50})
    return results


# Mock for region_preference: nothing in stock
def _mock_stock_empty(product_ids):
    return []


# ── Tests ────────────────────────────────────────────────────────────

@patch("agents.pipeline.get_client", return_value=_CLIENT)
@patch("agents.pipeline.get_stock_summary", return_value={"total": 100})
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_cross_family)
@patch("agents.pipeline.check_stock_for_order", side_effect=_stock_all_in)
@patch("agents.pipeline.has_ambiguous_variants", return_value=[])
@patch("agents.pipeline.calculate_order_price", return_value=100.0)
@patch("db.region_preference.search_stock_by_ids", side_effect=_mock_stock_eu_ok)
@patch("db.region_preference.get_catalog_products", return_value=_CATALOG)
class TestSoftPreferencePipeline:
    """Soft preference EU with stock → fulfillment proceeds, no ambiguous block."""

    def test_not_blocked(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU", "ME"],
            )],
        )
        result = _process(classification)
        assert "fulfillment_blocked" not in result

    def test_stock_check_items_narrowed(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU", "ME"],
            )],
        )
        result = _process(classification)
        sci = result.get("_stock_check_items", [])
        assert len(sci) == 1
        assert sci[0]["product_ids"] == [71]

    def test_display_name_updated(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU", "ME"],
            )],
        )
        result = _process(classification)
        sci = result.get("_stock_check_items", [])
        assert "EU" in sci[0].get("original_product_name", "")
        assert sci[0].get("display_name") is not None


@patch("agents.pipeline.get_client", return_value=_CLIENT)
@patch("agents.pipeline.get_stock_summary", return_value={"total": 100})
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_cross_family)
@patch("agents.pipeline.check_stock_for_order", side_effect=_stock_none)
@patch("agents.pipeline.select_best_alternatives", return_value={"reason": "oos", "alternatives": []})
@patch("db.region_preference.search_stock_by_ids", side_effect=_mock_stock_empty)
@patch("db.region_preference.get_catalog_products", return_value=_CATALOG)
class TestStrictPreferenceOOS:
    """Strict EU, OOS → stock_issue (OOS flow), NOT fulfillment_blocked."""

    def test_oos_flow_not_ambiguous(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU"],
                strict_region=True,
            )],
        )
        result = _process(classification)
        assert result.get("stock_issue") is not None
        assert "fulfillment_blocked" not in result

    def test_original_product_name_has_eu(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU"],
                strict_region=True,
            )],
        )
        result = _process(classification)
        # The stock_issue insufficient_items should reflect EU context
        insuff = result["stock_issue"]["stock_check"]["insufficient_items"]
        assert any("EU" in i.get("original_product_name", "") for i in insuff)


@patch("agents.pipeline.get_client", return_value=_CLIENT)
@patch("agents.pipeline.get_stock_summary", return_value={"total": 100})
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_cross_family)
@patch("agents.pipeline.check_stock_for_order", side_effect=_stock_all_in)
@patch("agents.pipeline.has_ambiguous_variants", return_value=["Turquoise"])
@patch("agents.pipeline.calculate_order_price", return_value=100.0)
class TestNoPrefCrossFamilyBlocked:
    """No region_preference + cross-family → existing ambiguous block."""

    def test_blocked_as_before(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
            )],
        )
        result = _process(classification)
        assert result.get("fulfillment_blocked") is True


@patch("agents.pipeline.get_client", return_value=_CLIENT)
@patch("agents.pipeline.get_stock_summary", return_value={"total": 100})
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_cross_family)
@patch("agents.pipeline.check_stock_for_order", side_effect=_stock_none)
@patch("agents.pipeline.select_best_alternatives", return_value={"reason": "oos", "alternatives": []})
@patch("db.region_preference.search_stock_by_ids", side_effect=_mock_stock_empty)
@patch("db.region_preference.get_catalog_products", return_value=_CATALOG)
class TestSoftBothOOS:
    """Soft pref EU+ME, both OOS → OOS flow with EU context, not ambiguous."""

    def test_oos_not_ambiguous(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU", "ME"],
            )],
        )
        result = _process(classification)
        assert result.get("stock_issue") is not None
        assert "fulfillment_blocked" not in result

    def test_eu_context_preserved(self, *mocks):
        classification = _make_classification(
            order_items=[OrderItem(
                product_name="Turquoise",
                base_flavor="Turquoise",
                quantity=10,
                region_preference=["EU", "ME"],
            )],
        )
        result = _process(classification)
        insuff = result["stock_issue"]["stock_check"]["insufficient_items"]
        assert any("EU" in i.get("original_product_name", "") for i in insuff)


@patch("agents.pipeline.get_client", return_value=_CLIENT)
@patch("agents.pipeline.get_stock_summary", return_value={"total": 100})
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_cross_family)
@patch("agents.pipeline.check_stock_for_order", side_effect=_stock_all_in)
@patch("agents.pipeline.has_ambiguous_variants", return_value=[])
@patch("agents.pipeline.calculate_order_price", return_value=100.0)
class TestFakeOrderItemCompat:
    """SimpleNamespace (fake order items in tests) don't crash with getattr."""

    def test_no_crash(self, *mocks):
        ns_item = SimpleNamespace(
            product_name="Turquoise",
            base_flavor="Turquoise",
            quantity=10,
        )
        classification = _make_classification()
        classification.order_items = [ns_item]
        # Should not raise — getattr fallback handles missing fields
        result = _process(classification)
        assert result is not None
