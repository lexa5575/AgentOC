"""Tests for pricing calculation integration.

Covers:
- Price source selection (parser_used=True vs False)
- Guard: template skipped when {PRICE} needed but no price available
- price_alert flags: mismatch and unmatched
"""

import pytest

pytestmark = pytest.mark.smoke


from unittest.mock import patch

import sys

import agents.pipeline  # ensure initial load

from agents.models import EmailClassification, OrderItem
from agents.handlers.template_utils import fill_template_reply


def _process_classified_email(classification):
    """Always call via current sys.modules reference (survives module re-imports)."""
    return sys.modules["agents.pipeline"].process_classified_email(classification)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_classification(**kwargs):
    defaults = {
        "needs_reply": True,
        "situation": "new_order",
        "client_email": "test@example.com",
        "parser_used": False,
    }
    defaults.update(kwargs)
    return EmailClassification(**defaults)


def _make_result(**kwargs):
    defaults = {
        "client_found": True,
        "client_data": {
            "payment_type": "prepay",
            "name": "Test User",
            "discount_percent": 0,
            "discount_orders_left": 0,
            "zelle_address": "pay@zelle.com",
        },
        "template_used": False,
        "draft_reply": None,
        "needs_routing": True,
        "needs_reply": True,
        "situation": "new_order",
        "client_email": "test@example.com",
        "client_name": None,
        "stock_issue": None,
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# fill_template_reply: price source
# ---------------------------------------------------------------------------

def test_parser_used_true_uses_site_price():
    """parser_used=True → template uses classification.price (site price)."""
    classification = _make_classification(
        parser_used=True,
        price="$220.00",
        client_name="Test User",
    )
    result = _make_result(calculated_price=200.0)

    result, found = fill_template_reply(classification, result, "new_order")
    assert found is True
    assert "$220.00" in result["draft_reply"]
    assert "$200.00" not in result["draft_reply"]


def test_parser_used_false_uses_calculated_price():
    """parser_used=False → template uses calculated_price, ignores LLM price."""
    classification = _make_classification(
        parser_used=False,
        price="$999.00",  # LLM garbage — should be ignored
        client_name="Test User",
    )
    result = _make_result(calculated_price=220.0)

    result, found = fill_template_reply(classification, result, "new_order")
    assert found is True
    assert "$220.00" in result["draft_reply"]
    assert "$999.00" not in result["draft_reply"]


# ---------------------------------------------------------------------------
# fill_template_reply: guard
# ---------------------------------------------------------------------------

def test_guard_no_price_skips_template():
    """No calculated_price for parser_used=False → template not used."""
    classification = _make_classification(
        parser_used=False,
        price="$999.00",
        client_name="Test User",
    )
    result = _make_result()  # no calculated_price key

    result, found = fill_template_reply(classification, result, "new_order")
    assert found is False


def test_guard_allows_template_without_price_placeholder():
    """Templates without {PRICE} should work even without price."""
    classification = _make_classification(
        parser_used=False,
        situation="oos_agrees",
        client_name="Test User",
    )
    # oos_agrees/prepay template doesn't have {PRICE}
    result = _make_result()  # no calculated_price

    result, found = fill_template_reply(classification, result, "oos_agrees")
    assert found is True


# ---------------------------------------------------------------------------
# process_classified_email: price alerts
# ---------------------------------------------------------------------------

@patch("agents.pipeline.calculate_order_price", return_value=220.0)
@patch("agents.pipeline.resolve_order_items", side_effect=lambda items, **kw: (items, []))
@patch("agents.pipeline.select_best_alternatives")
@patch("agents.pipeline.check_stock_for_order")
@patch("agents.pipeline.get_stock_summary")
@patch("agents.pipeline.get_client")
def test_mismatch_alert(mock_client, mock_summary, mock_stock, mock_alts, mock_resolve, mock_price):
    """parser_used=True, site price ≠ catalog → price_alert type=mismatch."""
    mock_client.return_value = {"payment_type": "prepay", "name": "Test"}
    mock_summary.return_value = {"total": 10}
    mock_stock.return_value = {
        "all_in_stock": True,
        "items": [{
            "base_flavor": "Green",
            "ordered_qty": 2,
            "stock_entries": [
                {"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 10},
            ],
            "total_available": 10,
            "is_sufficient": True,
        }],
        "insufficient_items": [],
    }

    classification = _make_classification(
        parser_used=True,
        price="$300.00",  # site says $300, catalog says $220
        order_items=[OrderItem(product_name="Green", base_flavor="Green", quantity=2)],
    )

    result = _process_classified_email(classification)
    assert result.get("price_alert") is not None
    assert result["price_alert"]["type"] == "mismatch"
    assert result["price_alert"]["site_price"] == "$300.00"
    assert result["price_alert"]["calculated_price"] == "$220.00"


@patch("agents.pipeline.calculate_order_price", return_value=None)
@patch("agents.pipeline.resolve_order_items", side_effect=lambda items, **kw: (items, []))
@patch("agents.pipeline.check_stock_for_order")
@patch("agents.pipeline.get_stock_summary")
@patch("agents.pipeline.get_client")
def test_unmatched_alert(mock_client, mock_summary, mock_stock, mock_resolve, mock_price):
    """parser_used=False, ambiguous categories → price_alert type=unmatched."""
    mock_client.return_value = {"payment_type": "prepay", "name": "Test"}
    mock_summary.return_value = {"total": 10}
    mock_stock.return_value = {
        "all_in_stock": True,
        "items": [{
            "base_flavor": "WeirdItem",
            "ordered_qty": 1,
            "stock_entries": [
                {"category": "KZ_TEREA", "product_name": "Weird", "quantity": 5},
                {"category": "TEREA_JAPAN", "product_name": "Weird", "quantity": 3},
            ],
            "total_available": 8,
            "is_sufficient": True,
        }],
        "insufficient_items": [],
    }

    classification = _make_classification(
        parser_used=False,
        order_items=[OrderItem(product_name="Weird", base_flavor="WeirdItem", quantity=1)],
    )

    result = _process_classified_email(classification)
    assert result.get("price_alert") is not None
    assert result["price_alert"]["type"] == "unmatched"
    assert "WeirdItem" in result["price_alert"]["items"]


@patch("agents.pipeline.calculate_order_price", return_value=220.0)
@patch("agents.pipeline.resolve_order_items", side_effect=lambda items, **kw: (items, []))
@patch("agents.pipeline.select_best_alternatives")
@patch("agents.pipeline.check_stock_for_order")
@patch("agents.pipeline.get_stock_summary")
@patch("agents.pipeline.get_client")
def test_no_alert_when_prices_match(mock_client, mock_summary, mock_stock, mock_alts, mock_resolve, mock_price):
    """parser_used=True, site price = catalog → no price_alert."""
    mock_client.return_value = {"payment_type": "prepay", "name": "Test"}
    mock_summary.return_value = {"total": 10}
    mock_stock.return_value = {
        "all_in_stock": True,
        "items": [{
            "base_flavor": "Green",
            "ordered_qty": 2,
            "stock_entries": [
                {"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 10},
            ],
            "total_available": 10,
            "is_sufficient": True,
        }],
        "insufficient_items": [],
    }

    classification = _make_classification(
        parser_used=True,
        price="$220.00",  # matches 2 × $110
        order_items=[OrderItem(product_name="Green", base_flavor="Green", quantity=2)],
    )

    result = _process_classified_email(classification)
    assert result.get("price_alert") is None
    assert result["calculated_price"] == 220.0
