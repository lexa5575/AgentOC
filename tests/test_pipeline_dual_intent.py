"""Tests for pipeline dual-intent payment_received resolve block.

Verifies that process_classified_email correctly sets:
- _stock_check_items (when resolved)
- payment_items_unresolved (when resolve fails / no msg_id)
- has_explicit_order_id (for trigger latest-fallback control)
- PAY-* auto order_id (only after successful resolve)
"""

import sys
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
        "situation": "payment_received",
        "client_email": "dual@example.com",
        "parser_used": False,
    }
    defaults.update(kwargs)
    return EmailClassification(**defaults)


# ── Mocks ────────────────────────────────────────────────────────────

_CLIENT_PREPAY = {
    "payment_type": "prepay",
    "name": "Test User",
    "discount_percent": 0,
    "discount_orders_left": 0,
    "zelle_address": "pay@zelle.com",
}

_RESOLVED_ITEM = {
    "product_name": "T Silver",
    "base_flavor": "Silver",
    "quantity": 1,
    "original_product_name": "T Silver",
    "product_ids": [42],
}


def _resolve_ok(items, **kw):
    """Mock resolve_order_items: success, adds product_ids."""
    for it in items:
        it["product_ids"] = [42]
    return items, []


def _resolve_fail(items, **kw):
    """Mock resolve_order_items: failure with alerts."""
    return items, ["Unknown product: Foo"]


def _resolve_no_product_ids(items, **kw):
    """Mock resolve_order_items: success but no product_ids."""
    return items, []


# ── Tests ────────────────────────────────────────────────────────────

@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_ok)
@patch("agents.pipeline.has_ambiguous_variants", return_value=[])
class TestDualIntentResolveSuccess:
    """Dual-intent: resolved items → _stock_check_items + PAY-* order_id."""

    def test_stock_check_items_set(self, _mock_ambig, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert "_stock_check_items" in result
        assert len(result["_stock_check_items"]) == 1
        assert result["_stock_check_items"][0]["product_ids"] == [42]

    def test_pay_order_id_generated(self, _mock_ambig, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        _process(classification, gmail_message_id="msg_abc123456789")

        assert classification.order_id == "PAY-abc123456789"

    def test_has_explicit_order_id_false(self, _mock_ambig, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        # order_id was empty → has_explicit_order_id=False
        # (but after processing, it's PAY-*, still initially False)
        assert result.get("has_explicit_order_id") is False

    def test_no_payment_items_unresolved(self, _mock_ambig, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert "payment_items_unresolved" not in result


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_fail)
class TestDualIntentResolveFail:
    """Dual-intent: resolve failure → payment_items_unresolved, no _stock_check_items."""

    def test_payment_items_unresolved_set(self, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="Foo", base_flavor="Foo", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert result["payment_items_unresolved"] is True
        assert "_stock_check_items" not in result

    def test_order_id_not_generated(self, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="Foo", base_flavor="Foo", quantity=1)],
        )

        _process(classification, gmail_message_id="msg_abc123456789")

        assert classification.order_id is None


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
class TestDualIntentNoMessageId:
    """Dual-intent without gmail_message_id → payment_items_unresolved."""

    def test_no_msg_id_blocks(self, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id=None)

        assert result["payment_items_unresolved"] is True
        assert "_stock_check_items" not in result


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
class TestDualIntentExplicitOrderId:
    """Explicit order_id → dual-intent skipped, has_explicit_order_id=True."""

    def test_explicit_order_id_skips_resolve(self, _mock_client):
        classification = _make_classification(
            order_id="#EXISTING",
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        # Dual-intent not activated → no _stock_check_items
        assert "_stock_check_items" not in result
        assert "payment_items_unresolved" not in result
        assert result["has_explicit_order_id"] is True
        # order_id preserved
        assert classification.order_id == "#EXISTING"


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_no_product_ids)
class TestDualIntentValidationFail:
    """Resolved but missing product_ids → payment_items_unresolved."""

    def test_no_product_ids_blocks(self, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert result["payment_items_unresolved"] is True
        assert "_stock_check_items" not in result


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
@patch("agents.pipeline.resolve_order_items", side_effect=_resolve_ok)
@patch("agents.pipeline.has_ambiguous_variants", return_value=["Silver"])
class TestDualIntentAmbiguous:
    """Ambiguous variants → fulfillment_blocked, but _stock_check_items still set."""

    def test_ambiguous_blocks_fulfillment(self, _mock_ambig, _mock_resolve, _mock_client):
        classification = _make_classification(
            order_items=[OrderItem(product_name="T Silver", base_flavor="Silver", quantity=1)],
        )

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert result["fulfillment_blocked"] is True
        assert result["ambiguous_flavors"] == ["Silver"]
        # _stock_check_items still set (trigger's ambiguous gate will handle)
        assert "_stock_check_items" in result


@patch("agents.pipeline.get_client", return_value=_CLIENT_PREPAY)
class TestNormalPaymentReceivedUnchanged:
    """payment_received without order_items → no dual-intent flags."""

    def test_no_order_items_no_flags(self, _mock_client):
        classification = _make_classification()

        result = _process(classification, gmail_message_id="msg_abc123456789")

        assert "_stock_check_items" not in result
        assert "payment_items_unresolved" not in result
        assert result.get("has_explicit_order_id") is False
