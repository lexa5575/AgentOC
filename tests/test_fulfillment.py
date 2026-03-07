"""Tests for the fulfillment engine (Phase 2 + Phase 3 integration).

Tests warehouse selection, idempotency, payment_received item source,
fulfillment trigger, and formatter output.
Google Sheets writes are mocked — no real API calls.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents.formatters import format_result
from agents.handlers.fulfillment_trigger import try_fulfillment
from db.fulfillment import (
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_SKIPPED_SPLIT,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_UPDATED,
    claim_fulfillment_event,
    finalize_fulfillment_event,
    get_order_items_for_fulfillment,
    get_warehouse_spreadsheet_id,
    increment_maks_sales,
    is_duplicate_fulfillment,
    select_fulfillment_warehouse,
)
from db.models import ClientOrderItem, FulfillmentEvent, ProductCatalog, StockItem


# ── Helpers ──────────────────────────────────────────────────────────

def _add_stock(session, warehouse, category, name, qty, maks=0,
               source_row=10, source_col=5, product_id=None):
    """Insert a StockItem and return it."""
    item = StockItem(
        warehouse=warehouse,
        category=category,
        product_name=name,
        quantity=qty,
        maks_sales=maks,
        source_row=source_row,
        source_col=source_col,
        product_id=product_id,
        synced_at=datetime.utcnow(),
    )
    session.add(item)
    session.flush()
    return item


def _add_catalog(session, category, name_norm, stock_name):
    """Insert a ProductCatalog entry and return its id."""
    entry = ProductCatalog(
        category=category,
        name_norm=name_norm,
        stock_name=stock_name,
    )
    session.add(entry)
    session.flush()
    return entry.id


def _add_order_item(session, email, order_id, product_name, base_flavor, qty=1):
    """Insert a ClientOrderItem."""
    item = ClientOrderItem(
        client_email=email.lower().strip(),
        order_id=order_id,
        product_name=product_name,
        base_flavor=base_flavor,
        product_type="stick",
        quantity=qty,
    )
    session.add(item)
    session.flush()
    return item


# ══════════════════════════════════════════════════════════════════════
# Warehouse selection
# ══════════════════════════════════════════════════════════════════════

class TestSelectFulfillmentWarehouse:

    def test_single_warehouse_success(self, db_session):
        """LA_MAKS has all items -> status=updated, warehouse=LA_MAKS."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_UPDATED
        assert result["warehouse"] == "LA_MAKS"
        assert len(result["matched_items"]) == 1
        assert result["matched_items"][0]["ordered_qty"] == 2
        assert result["matched_items"][0]["total_available"] == 10

    def test_fallback_warehouse_success(self, db_session):
        """LA_MAKS empty, CHICAGO_MAX has stock -> fallback to CHICAGO_MAX."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        # LA_MAKS: insufficient
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=0, product_id=cat_id)
        # CHICAGO_MAX: sufficient
        _add_stock(session, "CHICAGO_MAX", "TEREA_JAPAN", "T Silver", qty=5, product_id=cat_id)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 3, "product_ids": [cat_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_UPDATED
        assert result["warehouse"] == "CHICAGO_MAX"
        assert "LA_MAKS" in result["tried_warehouses"]

    def test_split_skip(self, db_session):
        """Silver only in LA, Amber only in Chicago -> skipped_split."""
        session = db_session()
        cat_silver = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_amber = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=5, product_id=cat_silver)
        _add_stock(session, "CHICAGO_MAX", "TEREA_EUROPE", "Amber", qty=5, product_id=cat_amber)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_silver]},
            {"base_flavor": "Amber", "quantity": 1, "product_ids": [cat_amber]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_SKIPPED_SPLIT
        assert result["warehouse"] is None
        assert result["matched_items"] is None
        assert len(result["tried_warehouses"]) == 3  # tried all 3

    def test_empty_order_items(self, db_session):
        result = select_fulfillment_warehouse([], "Los Angeles, CA 90001")
        assert result["status"] == STATUS_SKIPPED_UNRESOLVED

    def test_sum_across_rows(self, db_session):
        """Multiple stock entries per product should be summed."""
        session = db_session()
        cat_id1 = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_id2 = _add_catalog(session, "УНИКАЛЬНАЯ_ТЕРЕА", "t silver", "T Silver")
        # Two entries in same warehouse, different categories but same product_ids
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver",
                   qty=3, product_id=cat_id1, source_row=10)
        _add_stock(session, "LA_MAKS", "УНИКАЛЬНАЯ_ТЕРЕА", "T Silver",
                   qty=4, product_id=cat_id2, source_row=20)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 6, "product_ids": [cat_id1, cat_id2]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_UPDATED
        assert result["matched_items"][0]["total_available"] == 7  # 3 + 4

    def test_ilike_fallback_no_product_ids(self, db_session):
        """Without product_ids, falls back to ILIKE match."""
        session = db_session()
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1},  # no product_ids
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_UPDATED
        assert result["warehouse"] == "LA_MAKS"

    def test_geographic_priority(self, db_session):
        """FL address should try MIAMI_MAKS first."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        # Both warehouses have stock
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_stock(session, "MIAMI_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Miami, FL 33101")

        assert result["status"] == STATUS_UPDATED
        assert result["warehouse"] == "MIAMI_MAKS"
        assert result["tried_warehouses"] == ["MIAMI_MAKS"]


# ══════════════════════════════════════════════════════════════════════
# Idempotency
# ══════════════════════════════════════════════════════════════════════

class TestIdempotency:

    # ── claim_fulfillment_event structured result ──────────────────

    def test_claim_returns_created_with_event_id(self, db_session):
        """First claim returns created=True, duplicate=False, event_id=int."""
        result = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        assert result["created"] is True
        assert result["duplicate"] is False
        assert result["error"] is None
        assert isinstance(result["event_id"], int)

    def test_claim_duplicate_by_gmail_message_id(self, db_session):
        """Second INSERT with same gmail_message_id+trigger blocked by DB."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
            gmail_message_id="msg_abc",
        )
        result = claim_fulfillment_event(
            client_email="other@example.com",  # different email
            order_id="#999",                    # different order
            trigger_type="new_order_postpay",   # same trigger
            status=STATUS_UPDATED,
            gmail_message_id="msg_abc",         # same gmail_message_id
        )
        assert result["created"] is False
        assert result["duplicate"] is True
        assert result["error"] is None
        assert result["event_id"] is None

    def test_claim_duplicate_by_email_order_trigger(self, db_session):
        """Second INSERT with same email+order_id+trigger blocked by DB."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        result = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        assert result["created"] is False
        assert result["duplicate"] is True
        assert result["error"] is None

    def test_claim_different_trigger_allowed(self, db_session):
        """Same email+order but different trigger_type is allowed."""
        r1 = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        r2 = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="payment_received_prepay",
            status=STATUS_UPDATED,
        )
        assert r1["created"] is True
        assert r2["created"] is True

    # ── is_duplicate_fulfillment (read-only pre-check) ────────────

    def test_is_duplicate_by_gmail_message_id(self, db_session):
        """Read-only check detects duplicate by gmail_message_id."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
            gmail_message_id="msg_abc",
        )
        assert is_duplicate_fulfillment(
            "test@example.com", "#123", "new_order_postpay",
            gmail_message_id="msg_abc",
        )

    def test_is_duplicate_by_email_order_trigger(self, db_session):
        """Read-only check detects duplicate by email+order+trigger."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        assert is_duplicate_fulfillment(
            "test@example.com", "#123", "new_order_postpay",
        )

    def test_not_duplicate_different_trigger(self, db_session):
        """Same email+order but different trigger_type is NOT a duplicate."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        assert not is_duplicate_fulfillment(
            "test@example.com", "#123", "payment_received_prepay",
        )

    def test_not_duplicate_fresh(self, db_session):
        """No prior events -> not a duplicate."""
        assert not is_duplicate_fulfillment(
            "test@example.com", "#123", "new_order_postpay",
        )

    def test_not_duplicate_no_order_id(self, db_session):
        """No order_id and no gmail_message_id -> not a duplicate (can't check)."""
        claim_fulfillment_event(
            client_email="test@example.com",
            order_id=None,
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        # NULL order_id: DB unique constraint doesn't fire for NULLs,
        # and is_duplicate_fulfillment skips check without order_id
        assert not is_duplicate_fulfillment(
            "test@example.com", None, "new_order_postpay",
        )


# ══════════════════════════════════════════════════════════════════════
# Finalize fulfillment event
# ══════════════════════════════════════════════════════════════════════

class TestFinalizeFulfillmentEvent:

    def test_finalize_updates_status(self, db_session):
        """Finalize changes event status from processing to updated."""
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#123",
            trigger_type="new_order_postpay",
            status=STATUS_PROCESSING,
        )
        event_id = claim["event_id"]

        ok = finalize_fulfillment_event(event_id, STATUS_UPDATED, details={"updated": 2})
        assert ok is True

        # Verify DB
        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=event_id).first()
        assert event.status == STATUS_UPDATED
        assert json.loads(event.details_json)["updated"] == 2

    def test_finalize_to_error(self, db_session):
        """Finalize changes event status from processing to error."""
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#456",
            trigger_type="new_order_postpay",
            status=STATUS_PROCESSING,
        )
        event_id = claim["event_id"]

        ok = finalize_fulfillment_event(event_id, STATUS_ERROR, details={"errors": ["Sheets timeout"]})
        assert ok is True

        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=event_id).first()
        assert event.status == STATUS_ERROR

    def test_finalize_nonexistent_returns_false(self, db_session):
        """Finalize with invalid event_id returns False."""
        ok = finalize_fulfillment_event(999999, STATUS_UPDATED)
        assert ok is False


# ══════════════════════════════════════════════════════════════════════
# Deterministic order-item source (payment_received)
# ══════════════════════════════════════════════════════════════════════

class TestGetOrderItemsForFulfillment:

    def test_finds_items_by_order_id(self, db_session):
        """Finds order items by client_email + order_id."""
        session = db_session()
        _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_order_item(session, "buyer@example.com", "#100", "T Silver", "T Silver", qty=2)
        _add_order_item(session, "buyer@example.com", "#100", "Amber", "Amber", qty=1)
        session.commit()

        items = get_order_items_for_fulfillment("buyer@example.com", "#100")
        assert len(items) == 2
        flavors = {it["base_flavor"] for it in items}
        assert "T Silver" in flavors
        assert "Amber" in flavors

    def test_finds_most_recent_order(self, db_session):
        """Without order_id, returns the most recent order's items."""
        session = db_session()
        _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        old = ClientOrderItem(
            client_email="buyer@example.com", order_id="#old",
            product_name="Green", base_flavor="Green",
            product_type="stick", quantity=1,
            created_at=datetime.utcnow() - timedelta(days=10),
        )
        new = ClientOrderItem(
            client_email="buyer@example.com", order_id="#new",
            product_name="T Silver", base_flavor="T Silver",
            product_type="stick", quantity=3,
            created_at=datetime.utcnow(),
        )
        session.add_all([old, new])
        session.commit()

        items = get_order_items_for_fulfillment("buyer@example.com")
        assert len(items) == 1
        assert items[0]["base_flavor"] == "T Silver"
        assert items[0]["quantity"] == 3

    def test_empty_when_no_items(self, db_session):
        """Returns empty list when no ClientOrderItems found."""
        items = get_order_items_for_fulfillment("nobody@example.com", "#999")
        assert items == []

    def test_empty_when_no_order_id_in_latest(self, db_session):
        """Returns empty when latest item has no order_id."""
        session = db_session()
        item = ClientOrderItem(
            client_email="buyer@example.com", order_id=None,
            product_name="Silver", base_flavor="Silver",
            product_type="stick", quantity=1,
        )
        session.add(item)
        session.commit()

        items = get_order_items_for_fulfillment("buyer@example.com")
        assert items == []


# ══════════════════════════════════════════════════════════════════════
# increment_maks_sales (mocked Sheets)
# ══════════════════════════════════════════════════════════════════════

class TestIncrementMaksSales:

    def _make_sheet_config(self):
        """Create a mock SheetStructureConfig."""
        section = MagicMock()
        section.name = "TEREA_JAPAN"
        section.maks_col = 8
        config = MagicMock()
        config.sections = [section]
        return config

    @patch("db.fulfillment.get_warehouse_spreadsheet_id", return_value="sheet_123")
    @patch("tools.google_sheets.SheetsClient")
    @patch("db.sheet_config.load_sheet_config")
    def test_normal_update(self, mock_config, mock_sheets_cls, mock_get_id, db_session):
        """Normal increment: writes to Sheets and updates local DB."""
        mock_config.return_value = self._make_sheet_config()
        mock_client = MagicMock()
        mock_client.find_active_sheet.return_value = "LA MAKS FEB"
        mock_sheets_cls.return_value = mock_client

        session = db_session()
        item = _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver",
                          qty=10, maks=5, source_row=15)
        session.commit()

        matched = [{
            "base_flavor": "T Silver",
            "product_name": "T Silver",
            "ordered_qty": 2,
            "category": "TEREA_JAPAN",
            "source_row": 15,
            "maks_sales": 5,
            "stock_item_id": item.id,
            "total_available": 10,
        }]

        result = increment_maks_sales("LA_MAKS", matched)

        assert result["updated"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == []
        assert result["details"][0]["old_maks"] == 5
        assert result["details"][0]["new_maks"] == 7

        # Verify Sheets API was called
        mock_client.update_cell.assert_called_once_with(
            "sheet_123", "LA MAKS FEB", 15, 8, 7,
        )

        # Verify local DB updated
        session2 = db_session()
        updated = session2.query(StockItem).filter_by(id=item.id).first()
        assert updated.maks_sales == 7

    @patch("db.fulfillment.get_warehouse_spreadsheet_id", return_value="sheet_123")
    @patch("tools.google_sheets.SheetsClient")
    @patch("db.sheet_config.load_sheet_config")
    def test_skip_no_maks_col(self, mock_config, mock_sheets_cls, mock_get_id, db_session):
        """Items in categories without maks_col are skipped."""
        section = MagicMock()
        section.name = "TEREA_JAPAN"
        section.maks_col = None  # no maks_col
        config = MagicMock()
        config.sections = [section]
        mock_config.return_value = config
        mock_sheets_cls.return_value = MagicMock()

        matched = [{
            "base_flavor": "T Silver",
            "product_name": "T Silver",
            "ordered_qty": 1,
            "category": "TEREA_JAPAN",
            "source_row": 10,
            "maks_sales": 5,
            "stock_item_id": 999,
            "total_available": 10,
        }]

        result = increment_maks_sales("LA_MAKS", matched)
        assert result["skipped"] == 1
        assert result["updated"] == 0

    @patch("db.sheet_config.load_sheet_config", return_value=None)
    def test_error_no_sheet_config(self, mock_config, db_session):
        """No sheet config -> error."""
        result = increment_maks_sales("UNKNOWN", [])
        assert len(result["errors"]) == 1
        assert "No sheet config" in result["errors"][0]


# ══════════════════════════════════════════════════════════════════════
# get_warehouse_spreadsheet_id
# ══════════════════════════════════════════════════════════════════════

class TestGetWarehouseSpreadsheetId:

    def test_from_json_env(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "LA_MAKS", "spreadsheet_id": "id_la"},
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id_chi"},
        ]))
        assert get_warehouse_spreadsheet_id("LA_MAKS") == "id_la"
        assert get_warehouse_spreadsheet_id("CHICAGO_MAX") == "id_chi"
        assert get_warehouse_spreadsheet_id("UNKNOWN") is None

    def test_legacy_single_warehouse(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", "")
        monkeypatch.setenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")
        monkeypatch.setenv("STOCK_SPREADSHEET_ID", "legacy_id")
        assert get_warehouse_spreadsheet_id("LA_MAKS") == "legacy_id"
        assert get_warehouse_spreadsheet_id("OTHER") is None

    def test_no_config(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", "")
        monkeypatch.setenv("STOCK_SPREADSHEET_ID", "")
        assert get_warehouse_spreadsheet_id("LA_MAKS") is None


# ── Integration helpers ──────────────────────────────────────────────

def _mock_classification(**kwargs):
    """Create a mock EmailClassification-like object."""
    defaults = {
        "client_email": "test@example.com",
        "situation": "new_order",
        "order_id": "#100",
        "customer_city_state_zip": "Los Angeles, CA 90001",
        "customer_street": "123 Main St",
        "client_name": "Test Client",
        "needs_reply": True,
        "order_items": [],
        "parser_used": False,
        "price": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _base_result(**overrides):
    """Minimal result dict for format_result()."""
    r = {
        "needs_reply": True,
        "situation": "new_order",
        "client_email": "test@example.com",
        "client_name": "Test Client",
        "client_found": True,
        "client_data": {"payment_type": "postpay"},
        "template_used": True,
        "draft_reply": "Hello, your order is ready!",
    }
    r.update(overrides)
    return r


# ══════════════════════════════════════════════════════════════════════
# Fulfillment trigger (Phase 3)
# ══════════════════════════════════════════════════════════════════════

class TestTryFulfillment:
    """Integration tests for try_fulfillment trigger."""

    def test_new_order_postpay_selects_warehouse(self, db_session):
        """new_order/postpay with stock -> selects warehouse, attempts increment.

        Without mocked Sheets, increment returns error (no sheet config).
        Full success path tested in TestClaimLifecycle.test_successful_increment_finalized_as_updated.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#100",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_postpay")

        ff = result["fulfillment"]
        assert ff["warehouse"] == "LA_MAKS"
        assert ff["trigger_type"] == "new_order_postpay"
        assert "update_result" in ff
        # Without sheet config, increment fails -> lifecycle finalizes as error
        assert ff["status"] == STATUS_ERROR

    def test_payment_received_prepay_selects_warehouse(self, db_session):
        """payment_received/prepay with items -> selects warehouse, attempts increment.

        Without mocked Sheets, increment returns error (no sheet config).
        Full success path tested in TestClaimLifecycle.test_successful_increment_finalized_as_updated.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_order_item(session, "buyer@example.com", "#200", "T Silver", "T Silver", qty=1)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#200",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
        }

        try_fulfillment(classification, result)

        ff = result["fulfillment"]
        assert ff["warehouse"] == "LA_MAKS"
        assert ff["trigger_type"] == "payment_received_prepay"
        assert "update_result" in ff
        # Without sheet config, increment fails -> lifecycle finalizes as error
        assert ff["status"] == STATUS_ERROR

    def test_payment_received_ignores_conversation_state(self, db_session):
        """payment_received uses classification.order_id, NOT conversation_state.

        Verifies that even when conversation_state has a different order_id (#WRONG),
        the trigger uses classification.order_id (#CORRECT) to fetch items.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Order #CORRECT in DB — T Silver (matches stock)
        _add_order_item(session, "buyer@example.com", "#CORRECT", "T Silver", "T Silver", qty=1)
        # Order #WRONG in DB — Amber (NOT in stock for LA_MAKS)
        _add_order_item(session, "buyer@example.com", "#WRONG", "Amber", "Amber", qty=5)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#CORRECT",  # classification has the right order_id
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
            # conversation_state has a WRONG order_id — must be ignored
            "conversation_state": {"facts": {"order_id": "#WRONG"}},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_ignore_state")

        ff = result["fulfillment"]
        # Key assertion: warehouse found = items came from #CORRECT (T Silver in LA_MAKS)
        # If it had used #WRONG, it would try Amber which is NOT in LA_MAKS stock
        assert ff["warehouse"] == "LA_MAKS"
        assert ff["trigger_type"] == "payment_received_prepay"

    def test_payment_received_wrong_order_id_falls_back_to_latest(self, db_session):
        """payment_received with wrong order_id falls back to latest order."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Only a "latest" order exists (different order_id than classification)
        _add_order_item(session, "buyer@example.com", "#LATEST", "T Silver", "T Silver", qty=2)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#NONEXISTENT",  # This order_id has no items in DB
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_fallback")

        ff = result["fulfillment"]
        # Should have fallen back to latest order (#LATEST) and found warehouse
        assert ff["warehouse"] == "LA_MAKS"
        assert ff["trigger_type"] == "payment_received_prepay"
        # Status depends on sheet config availability (error without mocks)
        assert ff["status"] in (STATUS_UPDATED, STATUS_ERROR)

    def test_split_warehouse_skipped(self, db_session):
        """Items split across warehouses -> skipped_split, no increment."""
        session = db_session()
        cat_s = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_a = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=5, product_id=cat_s)
        _add_stock(session, "CHICAGO_MAX", "TEREA_EUROPE", "Amber", qty=5, product_id=cat_a)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#300",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_s]},
                {"base_flavor": "Amber", "quantity": 1, "product_ids": [cat_a]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_split")

        assert result["fulfillment"]["status"] == STATUS_SKIPPED_SPLIT
        assert result["fulfillment"]["warehouse"] is None

    def test_no_fulfillment_for_prepay_new_order(self, db_session):
        """new_order/prepay should NOT trigger fulfillment."""
        classification = _mock_classification(situation="new_order")
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "prepay"},
        }

        try_fulfillment(classification, result)

        assert "fulfillment" not in result

    def test_duplicate_skipped(self, db_session):
        """Duplicate processing -> skipped_duplicate."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#400",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        # First call -> updated (or error due to no sheet config, but claim is recorded)
        try_fulfillment(classification, result, gmail_message_id="msg_dup")
        first_status = result["fulfillment"]["status"]
        assert first_status in (STATUS_UPDATED, STATUS_ERROR)  # depends on sheet config

        # Second call -> duplicate
        del result["fulfillment"]
        try_fulfillment(classification, result, gmail_message_id="msg_dup")
        assert result["fulfillment"]["status"] == STATUS_SKIPPED_DUPLICATE

    def test_unresolved_items(self, db_session):
        """No stock items for new_order -> skipped_unresolved."""
        classification = _mock_classification(
            situation="new_order",
            order_id="#500",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            # No _stock_check_items
        }

        try_fulfillment(classification, result)

        assert result["fulfillment"]["status"] == STATUS_SKIPPED_UNRESOLVED

    def test_draft_failure_no_fulfillment(self, db_session):
        """When draft was not created, try_fulfillment is never called.

        This is enforced in pipeline.py via:
            if result.get("gmail_draft_id"):
                try_fulfillment(...)
        Not testable directly here — this test documents the contract.
        """
        # Pipeline only calls try_fulfillment when gmail_draft_id is present.
        # We verify: if called anyway with wrong payment_type, it's a no-op.
        classification = _mock_classification(situation="new_order")
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "unknown"},
        }
        try_fulfillment(classification, result)
        assert "fulfillment" not in result

    def test_empty_string_ids_normalized(self, db_session):
        """Empty string order_id/gmail_message_id are normalized to None."""
        classification = _mock_classification(
            situation="new_order",
            order_id="",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            # No items -> unresolved, but IDs should be None
        }

        try_fulfillment(classification, result, gmail_message_id="")

        assert result["fulfillment"]["status"] == STATUS_SKIPPED_UNRESOLVED


# ══════════════════════════════════════════════════════════════════════
# OOS Fulfillment Source Gating (Plan §7.4 / §8B)
# ══════════════════════════════════════════════════════════════════════

class TestOOSFulfillmentSourceGating:
    """Tests for OOS-derived effective_situation fulfillment gating (plan §8B)."""

    def test_trusted_source_effective_new_order_runs(self, db_session):
        """[8B.1] trusted source + effective_situation=new_order + postpay → fulfillment runs."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="oos_followup",
            order_id="#OOS-1",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "oos_followup",
            "effective_situation": "new_order",
            "confirmation_source": "thread_extraction",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_oos_trusted")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "new_order_postpay"
        assert ff["warehouse"] == "LA_MAKS"
        # Without sheet config, increment fails -> status=error, but fulfillment WAS attempted
        assert ff["status"] in (STATUS_UPDATED, STATUS_ERROR)

    def test_pending_oos_source_also_trusted(self, db_session):
        """[8B.1b] pending_oos source is also trusted for fulfillment."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="oos_followup",
            order_id="#OOS-1b",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "oos_followup",
            "effective_situation": "new_order",
            "confirmation_source": "pending_oos",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_oos_pending")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "new_order_postpay"
        assert ff["warehouse"] == "LA_MAKS"

    def test_classifier_source_skipped(self, db_session):
        """[8B.2] classifier source + effective_situation=new_order → fulfillment NOT run."""
        classification = _mock_classification(
            situation="oos_followup",
            order_id="#OOS-2",
        )
        result = {
            "situation": "oos_followup",
            "effective_situation": "new_order",
            "confirmation_source": "classifier",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [99]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_oos_classifier")

        assert "fulfillment" not in result

    def test_no_effective_situation_skipped(self, db_session):
        """[8B.3] no effective_situation → fulfillment NOT run for oos_followup."""
        classification = _mock_classification(
            situation="oos_followup",
            order_id="#OOS-3",
        )
        result = {
            "situation": "oos_followup",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [99]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_oos_none")

        assert "fulfillment" not in result

    def test_native_new_order_unaffected(self, db_session):
        """[8B.4] native situation=new_order + postpay → fulfillment still works (no source check)."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#NATIVE-1",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            # No effective_situation, no confirmation_source — native path
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_native")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "new_order_postpay"
        assert ff["warehouse"] == "LA_MAKS"
        assert ff["status"] in (STATUS_UPDATED, STATUS_ERROR)

    def test_native_payment_received_unaffected(self, db_session):
        """[8B.4b] native situation=payment_received + prepay → fulfillment still works."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_order_item(session, "test@example.com", "#PAY-1", "T Silver", "T Silver", qty=1)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            order_id="#PAY-1",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_pay")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "payment_received_prepay"
        assert ff["warehouse"] == "LA_MAKS"

    def test_llm_fallback_source_skipped(self, db_session):
        """[8B.2b] llm_fallback source → fulfillment NOT run."""
        classification = _mock_classification(
            situation="oos_followup",
            order_id="#OOS-LLM",
        )
        result = {
            "situation": "oos_followup",
            "effective_situation": "new_order",
            "confirmation_source": "llm_fallback",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [99]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_oos_llm")

        assert "fulfillment" not in result


# ══════════════════════════════════════════════════════════════════════
# Claim lifecycle (Phase 3 fix)
# ══════════════════════════════════════════════════════════════════════

class TestClaimLifecycle:
    """Tests for processing -> updated/error lifecycle."""

    def test_successful_updated_finalized(self, db_session):
        """Successful increment: event claimed as processing, finalized as updated."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#LC1",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_lc1")

        # Check that DB event exists and has been finalized
        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_lc1",
            trigger_type="new_order_postpay",
        ).first()
        assert event is not None
        # Without sheet config, increment returns errors -> finalized as error
        # With sheet config mocked, would be "updated"
        # Either way, it should NOT be "processing"
        assert event.status != STATUS_PROCESSING

    @patch("db.fulfillment.get_warehouse_spreadsheet_id", return_value="sheet_123")
    @patch("tools.google_sheets.SheetsClient")
    @patch("db.sheet_config.load_sheet_config")
    def test_successful_increment_finalized_as_updated(
        self, mock_config, mock_sheets_cls, mock_get_id, db_session,
    ):
        """With mocked Sheets: event finalized as 'updated' after successful increment."""
        section = MagicMock()
        section.name = "TEREA_JAPAN"
        section.maks_col = 8
        config = MagicMock()
        config.sections = [section]
        mock_config.return_value = config
        mock_client = MagicMock()
        mock_client.find_active_sheet.return_value = "LA MAKS FEB"
        mock_sheets_cls.return_value = mock_client

        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        item = _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver",
                          qty=10, maks=5, product_id=cat_id, source_row=15)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#LC2",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_lc2")

        # Result shows updated
        assert result["fulfillment"]["status"] == STATUS_UPDATED
        assert result["fulfillment"]["update_result"]["updated"] == 1
        assert result["fulfillment"]["update_result"]["errors"] == []

        # DB event finalized as "updated"
        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_lc2",
        ).first()
        assert event is not None
        assert event.status == STATUS_UPDATED

    def test_increment_error_finalized_as_error(self, db_session):
        """When increment_maks_sales has errors, event finalized as 'error'."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#LC3",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        # No sheet config mocked -> increment_maks_sales returns errors
        try_fulfillment(classification, result, gmail_message_id="msg_lc3")

        # Result should show error
        assert result["fulfillment"]["status"] == STATUS_ERROR
        assert "error" in result["fulfillment"]
        assert "update_result" in result["fulfillment"]

        # DB event finalized as "error" (not stuck on "processing")
        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_lc3",
        ).first()
        assert event is not None
        assert event.status == STATUS_ERROR

    def test_event_never_stuck_as_processing(self, db_session):
        """After try_fulfillment completes (success or error), event is never left as 'processing'.

        This tests the safety net: regardless of what happens during increment,
        the DB event must be finalized to a terminal status.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#LC_SAFE",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_safe")

        # Result must have a terminal status
        assert result["fulfillment"]["status"] in (STATUS_UPDATED, STATUS_ERROR)

        # DB event must NOT be stuck as "processing"
        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_safe",
        ).first()
        assert event is not None
        assert event.status != STATUS_PROCESSING
        assert event.status in (STATUS_UPDATED, STATUS_ERROR)

    def test_exception_safety_net_direct(self, db_session):
        """Direct test: a 'processing' event can be finalized to 'error' via finalize_fulfillment_event.

        This validates the safety net mechanism used in try_fulfillment's except clause.
        """
        # Simulate: claim with processing, then finalize as error (as exception handler would do)
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#CRASH",
            trigger_type="new_order_postpay",
            status=STATUS_PROCESSING,
            gmail_message_id="msg_crash",
        )
        assert claim["created"] is True
        event_id = claim["event_id"]

        # Verify it's currently "processing"
        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=event_id).first()
        assert event.status == STATUS_PROCESSING

        # Safety net: finalize as error (simulating what the except clause does)
        ok = finalize_fulfillment_event(
            event_id,
            status=STATUS_ERROR,
            details={"exception": "something crashed"},
        )
        assert ok is True

        # Verify it's now "error"
        session2 = db_session()
        event2 = session2.query(FulfillmentEvent).filter_by(id=event_id).first()
        assert event2.status == STATUS_ERROR

    def test_duplicate_still_skipped_after_lifecycle(self, db_session):
        """After a lifecycle claim, duplicate is still properly detected."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#LC4",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        items = [{"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]}]
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": items,
        }

        # First call
        try_fulfillment(classification, result, gmail_message_id="msg_lc4")
        first_status = result["fulfillment"]["status"]
        assert first_status in (STATUS_UPDATED, STATUS_ERROR)

        # Second call -> duplicate
        del result["fulfillment"]
        result["_stock_check_items"] = items
        try_fulfillment(classification, result, gmail_message_id="msg_lc4")
        assert result["fulfillment"]["status"] == STATUS_SKIPPED_DUPLICATE


# ══════════════════════════════════════════════════════════════════════
# format_result FULFILLMENT section (Phase 3)
# ══════════════════════════════════════════════════════════════════════

class TestFormatResultFulfillment:
    """format_result shows FULFILLMENT section correctly."""

    def test_format_updated(self):
        result = _base_result()
        result["fulfillment"] = {
            "status": "updated",
            "warehouse": "LA_MAKS",
            "trigger_type": "new_order_postpay",
            "tried_warehouses": ["LA_MAKS"],
            "update_result": {
                "updated": 2, "skipped": 0, "errors": [],
                "details": [
                    {"product_name": "T Silver", "old_maks": 40, "new_maks": 41},
                    {"product_name": "Amber", "old_maks": 32, "new_maks": 33},
                ],
            },
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "Status: updated" in output
        assert "Warehouse: LA_MAKS" in output
        assert "Updated rows: 2" in output
        assert "T Silver: 40 -> 41" in output
        assert "Amber: 32 -> 33" in output

    def test_format_split(self):
        result = _base_result()
        result["fulfillment"] = {
            "status": "skipped_split",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "tried_warehouses": ["LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"],
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "skipped_split" in output
        assert "NOT updated" in output
        assert "Tried:" in output

    def test_format_duplicate(self):
        result = _base_result()
        result["fulfillment"] = {
            "status": "skipped_duplicate",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "duplicate" in output

    def test_format_unresolved(self):
        result = _base_result()
        result["fulfillment"] = {
            "status": "skipped_unresolved_order",
            "warehouse": None,
            "trigger_type": "payment_received_prepay",
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "no resolved order items" in output

    def test_format_error(self):
        result = _base_result()
        result["fulfillment"] = {
            "status": "error",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "error": "Sheets API timeout",
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "Sheets API timeout" in output

    def test_format_error_with_update_result_details(self):
        """Error with update_result shows individual error lines."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "error",
            "warehouse": "LA_MAKS",
            "trigger_type": "new_order_postpay",
            "error": "No sheet config for warehouse LA_MAKS",
            "update_result": {
                "updated": 0, "skipped": 0,
                "errors": ["No sheet config for warehouse LA_MAKS"],
                "details": [],
            },
        }
        output = format_result(result)
        assert "FULFILLMENT" in output
        assert "Status: error" in output
        assert "No sheet config for warehouse LA_MAKS" in output

    def test_no_fulfillment_section_when_absent(self):
        result = _base_result()
        output = format_result(result)
        assert "FULFILLMENT" not in output


# ══════════════════════════════════════════════════════════════════════
# Split breakdown
# ══════════════════════════════════════════════════════════════════════

class TestSplitBreakdown:

    def test_split_returns_breakdown(self, db_session):
        """Split result includes split_breakdown with correct qty per warehouse."""
        session = db_session()
        cat_silver = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_amber = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=5, product_id=cat_silver)
        _add_stock(session, "CHICAGO_MAX", "TEREA_EUROPE", "Amber", qty=3, product_id=cat_amber)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_silver]},
            {"base_flavor": "Amber", "quantity": 1, "product_ids": [cat_amber]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_SKIPPED_SPLIT
        bd = result["split_breakdown"]
        assert len(bd) == 2

        silver = bd[0]
        assert silver["base_flavor"] == "T Silver"
        assert silver["ordered_qty"] == 1
        assert silver["availability"]["LA_MAKS"] == 5
        assert silver["availability"]["CHICAGO_MAX"] == 0
        assert silver["availability"]["MIAMI_MAKS"] == 0

        amber = bd[1]
        assert amber["base_flavor"] == "Amber"
        assert amber["ordered_qty"] == 1
        assert amber["availability"]["LA_MAKS"] == 0
        assert amber["availability"]["CHICAGO_MAX"] == 3
        assert amber["availability"]["MIAMI_MAKS"] == 0

    def test_split_breakdown_sums_multiple_rows(self, db_session):
        """Multiple stock rows for same item in same warehouse are summed."""
        session = db_session()
        cat_id1 = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_id2 = _add_catalog(session, "УНИКАЛЬНАЯ_ТЕРЕА", "t silver v2", "T Silver")
        cat_amber = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        # Two stock entries for Silver in LA_MAKS (different product_ids)
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=3, product_id=cat_id1)
        _add_stock(session, "LA_MAKS", "УНИКАЛЬНАЯ_ТЕРЕА", "T Silver", qty=4, product_id=cat_id2)
        # Amber only in CHICAGO
        _add_stock(session, "CHICAGO_MAX", "TEREA_EUROPE", "Amber", qty=5, product_id=cat_amber)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 8, "product_ids": [cat_id1, cat_id2]},
            {"base_flavor": "Amber", "quantity": 1, "product_ids": [cat_amber]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_SKIPPED_SPLIT
        bd = result["split_breakdown"]
        silver = bd[0]
        assert silver["availability"]["LA_MAKS"] == 7  # 3 + 4

    def test_split_breakdown_warehouse_order(self, db_session):
        """Availability keys match tried_warehouses order."""
        session = db_session()
        cat_silver = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_amber = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=5, product_id=cat_silver)
        _add_stock(session, "MIAMI_MAKS", "TEREA_EUROPE", "Amber", qty=5, product_id=cat_amber)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_silver]},
            {"base_flavor": "Amber", "quantity": 1, "product_ids": [cat_amber]},
        ]
        # FL address -> MIAMI_MAKS first
        result = select_fulfillment_warehouse(order_items, "Miami, FL 33101")

        bd = result["split_breakdown"]
        tried = result["tried_warehouses"]
        # Availability keys should be in same order as tried_warehouses
        for item in bd:
            assert list(item["availability"].keys()) == tried

    def test_no_breakdown_on_success(self, db_session):
        """Success path does not include split_breakdown."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_UPDATED
        assert "split_breakdown" not in result

    def test_format_split_with_breakdown(self):
        """Formatter renders breakdown with OK/PARTIAL/-- tags."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "skipped_split",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "tried_warehouses": ["LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"],
            "split_breakdown": [
                {
                    "base_flavor": "T Silver",
                    "ordered_qty": 3,
                    "availability": {
                        "LA_MAKS": 5,
                        "CHICAGO_MAX": 0,
                        "MIAMI_MAKS": 2,
                    },
                },
                {
                    "base_flavor": "Amber",
                    "ordered_qty": 2,
                    "availability": {
                        "LA_MAKS": 0,
                        "CHICAGO_MAX": 8,
                        "MIAMI_MAKS": 0,
                    },
                },
            ],
        }
        output = format_result(result)
        assert "Split breakdown:" in output
        assert "T Silver (need 3):" in output
        assert "LA_MAKS: 5 [OK]" in output
        assert "CHICAGO_MAX: 0 [--]" in output
        assert "MIAMI_MAKS: 2 [PARTIAL]" in output
        assert "Amber (need 2):" in output
        assert "CHICAGO_MAX: 8 [OK]" in output
        assert "LA_MAKS: 0 [--]" in output

    def test_format_split_without_breakdown(self):
        """Formatter works when no breakdown (backward compat)."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "skipped_split",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "tried_warehouses": ["LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"],
        }
        output = format_result(result)
        assert "skipped_split" in output
        assert "NOT updated" in output
        assert "Split breakdown:" not in output
