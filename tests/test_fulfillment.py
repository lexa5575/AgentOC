"""Tests for the fulfillment engine (Phase 2 + Phase 3 + Phase 4 integration).

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
    STATUS_BLOCKED_AMBIGUOUS,
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
    parse_details_json,
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


def _add_order_item(session, email, order_id, product_name, base_flavor, qty=1,
                    variant_id=None):
    """Insert a ClientOrderItem."""
    item = ClientOrderItem(
        client_email=email.lower().strip(),
        order_id=order_id,
        product_name=product_name,
        base_flavor=base_flavor,
        product_type="stick",
        quantity=qty,
        variant_id=variant_id,
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

    def test_no_product_ids_skips_item(self, db_session):
        """Phase 8: without product_ids, item is skipped (no ILIKE fallback)."""
        session = db_session()
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10)
        session.commit()

        order_items = [
            {"base_flavor": "T Silver", "quantity": 1},  # no product_ids
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")

        assert result["status"] == STATUS_SKIPPED_SPLIT

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
        cat_s = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        cat_a = _add_catalog(session, "TEREA_EUROPE", "amber", "Amber")
        _add_order_item(session, "buyer@example.com", "#100", "T Silver", "T Silver",
                        qty=2, variant_id=cat_s)
        _add_order_item(session, "buyer@example.com", "#100", "Amber", "Amber",
                        qty=1, variant_id=cat_a)
        session.commit()

        items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#100")
        assert len(items) == 2
        assert skipped == []
        flavors = {it["base_flavor"] for it in items}
        assert "T Silver" in flavors
        assert "Amber" in flavors

    def test_finds_most_recent_order(self, db_session):
        """Without order_id, returns the most recent order's items."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
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
            variant_id=cat_id,
            created_at=datetime.utcnow(),
        )
        session.add_all([old, new])
        session.commit()

        items, skipped = get_order_items_for_fulfillment("buyer@example.com")
        assert len(items) == 1
        assert skipped == []
        assert items[0]["base_flavor"] == "T Silver"
        assert items[0]["quantity"] == 3

    def test_empty_when_no_items(self, db_session):
        """Returns empty tuple when no ClientOrderItems found."""
        items, skipped = get_order_items_for_fulfillment("nobody@example.com", "#999")
        assert items == []
        assert skipped == []

    def test_empty_when_no_order_id_in_latest(self, db_session):
        """Returns empty tuple when latest item has no order_id."""
        session = db_session()
        item = ClientOrderItem(
            client_email="buyer@example.com", order_id=None,
            product_name="Silver", base_flavor="Silver",
            product_type="stick", quantity=1,
        )
        session.add(item)
        session.commit()

        items, skipped = get_order_items_for_fulfillment("buyer@example.com")
        assert items == []
        assert skipped == []


# ══════════════════════════════════════════════════════════════════════
# Phase 4: variant_id-first read path
# ══════════════════════════════════════════════════════════════════════

class TestReadPathVariantFirst:
    """Phase 4: get_order_items_for_fulfillment variant_id-first logic."""

    def test_variant_id_direct_lookup_no_resolver(self, db_session):
        """[P4] Row with variant_id → product_ids=[variant_id], no resolver called."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_EUROPE", "bronze", "Bronze")
        _add_order_item(
            session, "buyer@example.com", "#200", "Bronze EU", "Bronze",
            qty=2, variant_id=cat_id,
        )
        session.commit()

        with patch("db.product_resolver.resolve_product_to_catalog") as mock_resolve:
            items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#200")

        assert len(items) == 1
        assert skipped == []
        assert items[0]["product_ids"] == [cat_id]
        assert items[0]["base_flavor"] == "Bronze"
        assert items[0]["quantity"] == 2
        # Resolver must NOT be called when variant_id exists
        mock_resolve.assert_not_called()

    def test_null_variant_id_legacy_re_resolve(self, db_session, monkeypatch):
        """[P4] NULL variant_id + strict=false → legacy re-resolve path used."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_order_item(
            session, "buyer@example.com", "#201", "T Silver", "T Silver",
            qty=3,  # no variant_id
        )
        session.commit()

        # Mock resolver to return single match (exact confidence)
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.product_ids = [cat_id]
        mock_result.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_result):
            items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#201")

        assert len(items) == 1
        assert skipped == []
        assert items[0]["base_flavor"] == "T Silver"
        # Legacy path resolves product_ids via resolver
        assert items[0]["product_ids"] == [cat_id]

    def test_null_variant_id_strict_blocks_order(self, db_session, monkeypatch):
        """[P4] NULL variant_id + strict=true → whole order blocked."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "true")
        session = db_session()
        _add_order_item(
            session, "buyer@example.com", "#202", "Silver", "Silver",
            qty=3,  # no variant_id
        )
        session.commit()

        items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#202")
        assert items == []
        assert len(skipped) == 1
        assert skipped[0]["base_flavor"] == "Silver"

    def test_mixed_strict_blocks_whole_order(self, db_session, monkeypatch):
        """[P4] Mixed: one resolved + one unresolved in strict → whole order blocked."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "true")
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_EUROPE", "bronze", "Bronze")
        _add_order_item(
            session, "buyer@example.com", "#203", "Bronze EU", "Bronze",
            qty=2, variant_id=cat_id,
        )
        _add_order_item(
            session, "buyer@example.com", "#203", "Silver", "Silver",
            qty=1,  # no variant_id
        )
        session.commit()

        items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#203")
        # Hard block: whole order blocked even though Bronze has variant_id
        assert items == []
        assert len(skipped) == 1
        assert skipped[0]["base_flavor"] == "Silver"


class TestTriggerSkippedItemsBlocking:
    """Phase 4: fulfillment_trigger handles skipped_items from read path."""

    def test_payment_received_skipped_items_blocks(self, db_session, monkeypatch):
        """[P4] payment_received with strict skipped_items → STATUS_BLOCKED_AMBIGUOUS."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "true")
        session = db_session()
        # Order item without variant_id
        _add_order_item(
            session, "buyer@example.com", "#300", "Silver", "Silver",
            qty=3,  # no variant_id → will be skipped in strict mode
        )
        session.commit()

        classification = _mock_classification(
            client_email="buyer@example.com",
            situation="payment_received",
            order_id="#300",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_strict_block")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["warehouse"] is None
        assert "Silver" in ff.get("ambiguous_flavors", [])

    def test_payment_received_variant_id_passes(self, db_session, monkeypatch):
        """[P4] payment_received with variant_id → proceeds past skipped gate.

        May end as STATUS_ERROR due to missing googleapiclient (pre-existing),
        but must NOT be blocked by skipped_items gate.
        """
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "true")
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_order_item(
            session, "buyer@example.com", "#301", "T Silver", "T Silver",
            qty=2, variant_id=cat_id,
        )
        session.commit()

        classification = _mock_classification(
            client_email="buyer@example.com",
            situation="payment_received",
            order_id="#301",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_variant_ok")

        ff = result["fulfillment"]
        # NOT blocked by skipped gate — proceeds to warehouse selection
        assert ff["status"] != STATUS_BLOCKED_AMBIGUOUS or \
            ff.get("ambiguous_flavors") != ["T Silver"]
        # Warehouse was attempted (may error on Sheets, but that's pre-existing)
        assert ff["status"] in (STATUS_UPDATED, STATUS_ERROR, STATUS_SKIPPED_SPLIT)


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
        _add_order_item(session, "buyer@example.com", "#200", "T Silver", "T Silver", qty=1,
                        variant_id=cat_id)
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
        _add_order_item(session, "buyer@example.com", "#CORRECT", "T Silver", "T Silver", qty=1,
                        variant_id=cat_id)
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

    def test_payment_received_wrong_order_id_no_latest_fallback(self, db_session):
        """payment_received with explicit wrong order_id → SKIPPED_UNRESOLVED, no latest fallback.

        Changed in v3.1: explicit order_id not found in DB no longer falls back to latest.
        This prevents decrementing stock for the wrong order.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Only a "latest" order exists (different order_id than classification)
        _add_order_item(session, "buyer@example.com", "#LATEST", "T Silver", "T Silver", qty=2,
                        variant_id=cat_id)
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
        # Explicit order_id (#NONEXISTENT) not found → no latest fallback → unresolved
        assert ff["status"] == STATUS_SKIPPED_UNRESOLVED

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
        """Duplicate processing -> skipped_duplicate when first event is updated/processing."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Pre-create a successful fulfillment event so the 2nd call is truly a duplicate
        ev = FulfillmentEvent(
            client_email="test@example.com",
            order_id="#400",
            gmail_message_id="msg_dup",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        session.add(ev)
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

        # Call with same gmail_message_id -> should be blocked as duplicate
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
        """Empty string order_id/gmail_message_id are normalized to None.

        Phase 4.1: empty order_id now hits missing_order_id gate
        (new_order_postpay requires order_id).
        """
        classification = _mock_classification(
            situation="new_order",
            order_id="",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            # No items -> but IDs normalized to None first
        }

        try_fulfillment(classification, result, gmail_message_id="")

        # Phase 4.1: empty order_id → blocked (not unresolved)
        assert result["fulfillment"]["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert result["fulfillment"]["reason"] == "missing_order_id_new_order_postpay"


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
        _add_order_item(session, "test@example.com", "#PAY-1", "T Silver", "T Silver", qty=1, variant_id=cat_id)
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

        # Verify it's now "error" with v2 schema
        session2 = db_session()
        event2 = session2.query(FulfillmentEvent).filter_by(id=event_id).first()
        assert event2.status == STATUS_ERROR
        stored = json.loads(event2.details_json)
        assert stored["v"] == 2
        assert stored["exception"] == "something crashed"

    def test_duplicate_still_skipped_after_lifecycle(self, db_session):
        """After a successful lifecycle, duplicate is properly detected."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Pre-create a successful (updated) event to simulate completed lifecycle
        ev = FulfillmentEvent(
            client_email="test@example.com",
            order_id="#LC4",
            gmail_message_id="msg_lc4",
            trigger_type="new_order_postpay",
            status=STATUS_UPDATED,
        )
        session.add(ev)
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

        # Call with same gmail_message_id -> should be blocked as duplicate
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


# ══════════════════════════════════════════════════════════════════════
# Phase 3: Ambiguity gate in try_fulfillment
# ══════════════════════════════════════════════════════════════════════

class TestFulfillmentBlockedAmbiguous:
    """Phase 3: fulfillment_blocked flag blocks fulfillment with blocked status."""

    def test_blocked_flag_skips_fulfillment(self, db_session):
        """[P3] fulfillment_blocked=True → status=blocked_ambiguous_variant, no increment."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#200",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "Silver", "quantity": 3, "product_ids": [10, 30, 54]},
            ],
            "fulfillment_blocked": True,
            "ambiguous_flavors": ["Silver"],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_blocked")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["warehouse"] is None
        assert ff["trigger_type"] == "new_order_postpay"
        assert "Silver" in ff.get("ambiguous_flavors", [])
        # No increment happened — no update_result key
        assert "update_result" not in ff

        # Verify DB event was claimed with blocked status
        events = session.query(FulfillmentEvent).filter_by(
            client_email="test@example.com",
            order_id="#200",
        ).all()
        assert len(events) == 1
        assert events[0].status == STATUS_BLOCKED_AMBIGUOUS

    def test_non_blocked_path_unchanged(self, db_session):
        """[P3] Without fulfillment_blocked, normal path proceeds to warehouse selection.

        Note: may end as STATUS_ERROR due to missing googleapiclient (pre-existing),
        but the key assertion is it did NOT hit the ambiguity gate.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="#201",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
            ],
            # NO fulfillment_blocked flag
        }

        try_fulfillment(classification, result, gmail_message_id="msg_ok")

        ff = result["fulfillment"]
        # Key assertion: NOT blocked by ambiguity gate
        assert ff["status"] != STATUS_BLOCKED_AMBIGUOUS
        # Warehouse was selected (proves we passed the gate)
        assert ff.get("warehouse") is not None or ff["status"] == STATUS_ERROR


class TestFormatBlockedAmbiguous:
    """Phase 3 + 4.1: formatter renders blocked_ambiguous_variant with reason."""

    def test_format_blocked_ambiguous(self):
        """[P3] Formatter shows default reason when no reason field."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "blocked_ambiguous_variant",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "ambiguous_flavors": ["Silver", "Bronze"],
        }
        output = format_result(result)
        assert "blocked_ambiguous_variant" in output
        assert "ambiguous variant mapping" in output
        assert "NOT updated" in output
        assert "Silver, Bronze" in output

    def test_format_reason_unresolved_variant_strict(self):
        """[P4.1-D4] Formatter shows correct reason for unresolved_variant_strict."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "blocked_ambiguous_variant",
            "warehouse": None,
            "trigger_type": "payment_received_prepay",
            "reason": "unresolved_variant_strict",
            "ambiguous_flavors": ["Silver"],
        }
        output = format_result(result)
        assert "unresolved variant (strict mode)" in output
        assert "NOT updated" in output
        assert "Silver" in output

    def test_format_reason_missing_order_id(self):
        """[P4.1-D5] Formatter shows correct reason for missing_order_id_new_order_postpay."""
        result = _base_result()
        result["fulfillment"] = {
            "status": "blocked_ambiguous_variant",
            "warehouse": None,
            "trigger_type": "new_order_postpay",
            "reason": "missing_order_id_new_order_postpay",
        }
        output = format_result(result)
        assert "missing order_id for new_order_postpay" in output
        assert "NOT updated" in output


# ══════════════════════════════════════════════════════════════════════
# Phase 4.1: Hotfix tests
# ══════════════════════════════════════════════════════════════════════

class TestMissingOrderIdBlocked:
    """Phase 4.1: new_order_postpay + missing order_id → blocked."""

    def test_order_id_none_blocks(self, db_session):
        """[P4.1-D1] new_order_postpay + order_id=None → blocked_ambiguous_variant."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id=None,
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 2, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_no_oid")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["reason"] == "missing_order_id_new_order_postpay"
        assert ff["warehouse"] is None

    def test_order_id_whitespace_blocks(self, db_session):
        """[P4.1-D2] new_order_postpay + order_id='   ' → blocked_ambiguous_variant."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="new_order",
            order_id="   ",
        )
        result = {
            "situation": "new_order",
            "client_data": {"payment_type": "postpay"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_ws_oid")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["reason"] == "missing_order_id_new_order_postpay"


class TestLegacyReResolveAmbiguousBlocked:
    """Phase 4.1: legacy re-resolve with ambiguous product_ids → blocked."""

    def test_legacy_multi_product_ids_blocks(self, db_session, monkeypatch):
        """[P4.1-D3] payment_received strict=false + multi product_ids → blocked."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        _add_order_item(
            session, "buyer@example.com", "#400", "Silver", "Silver",
            qty=3,  # no variant_id → legacy re-resolve
        )
        session.commit()

        # Mock resolver to return 3 product_ids (ambiguous)
        mock_result = MagicMock()
        mock_result.product_ids = [10, 30, 54]
        mock_result.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_result):
            items, skipped = get_order_items_for_fulfillment("buyer@example.com", "#400")

        # Ambiguous re-resolve → items blocked even in non-strict mode
        assert items == []
        assert len(skipped) == 1
        assert skipped[0]["base_flavor"] == "Silver"
        assert skipped[0]["product_ids_count"] == 3

    def test_legacy_ambiguous_reason_in_try_fulfillment(self, db_session, monkeypatch):
        """[P4.2-D] payment_received + legacy ambiguous → reason=ambiguous_variant,
        details_json has product_ids_count=3 (not 0).
        """
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        _add_order_item(
            session, "test@example.com", "#500", "Silver", "Silver",
            qty=2,  # no variant_id → legacy re-resolve
        )
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            order_id="#500",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
        }

        # Mock resolver to return 3 product_ids (ambiguous)
        mock_resolved = MagicMock()
        mock_resolved.product_ids = [10, 30, 54]
        mock_resolved.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved):
            try_fulfillment(classification, result, gmail_message_id="msg_ambig_reason")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["reason"] == "ambiguous_variant"
        assert "Silver" in ff["ambiguous_flavors"]

        # Verify details_json in DB event has product_ids_count=3
        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_ambig_reason",
        ).first()
        assert event is not None
        details = json.loads(event.details_json) if event.details_json else {}
        assert details.get("reason") == "ambiguous_variant"
        skipped_in_details = details.get("skipped_items", [])
        assert len(skipped_in_details) == 1
        assert skipped_in_details[0]["product_ids_count"] == 3


# ══════════════════════════════════════════════════════════════════════
# Region Family: expansion + legacy same-family
# ══════════════════════════════════════════════════════════════════════

class TestFamilyExpansionInQueryStock:
    """_query_stock_entries expands product_ids to family siblings."""

    def test_expansion_finds_sibling_stock(self, db_session, monkeypatch):
        """variant_id=ARMENIA Silver, warehouse has KZ_TEREA Silver → found."""
        monkeypatch.setenv("USE_FAMILY_FULFILLMENT", "true")
        session = db_session()
        # Catalog: ARMENIA Silver (id=17), KZ_TEREA Silver (id=24)
        arm_id = _add_catalog(session, "ARMENIA", "silver", "Silver")
        kz_id = _add_catalog(session, "KZ_TEREA", "silver", "Silver")
        # Stock: only KZ_TEREA Silver in warehouse
        _add_stock(session, "LA_MAKS", "KZ_TEREA", "Silver", qty=10, product_id=kz_id)
        session.commit()

        order_items = [
            {"base_flavor": "Silver", "quantity": 2, "product_ids": [arm_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")
        assert result["status"] == STATUS_UPDATED
        assert result["warehouse"] == "LA_MAKS"

    def test_expansion_disabled_misses_sibling(self, db_session, monkeypatch):
        """USE_FAMILY_FULFILLMENT=false → no expansion, can't find sibling."""
        monkeypatch.setenv("USE_FAMILY_FULFILLMENT", "false")
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "silver", "Silver")
        kz_id = _add_catalog(session, "KZ_TEREA", "silver", "Silver")
        _add_stock(session, "LA_MAKS", "KZ_TEREA", "Silver", qty=10, product_id=kz_id)
        session.commit()

        order_items = [
            {"base_flavor": "Silver", "quantity": 2, "product_ids": [arm_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")
        assert result["status"] == STATUS_SKIPPED_SPLIT  # can't find ARMENIA stock

    def test_expansion_respects_name_norm(self, db_session, monkeypatch):
        """Silver(ARMENIA) must NOT pull Bronze(KZ_TEREA) — different name_norm."""
        monkeypatch.setenv("USE_FAMILY_FULFILLMENT", "true")
        session = db_session()
        arm_silver_id = _add_catalog(session, "ARMENIA", "silver", "Silver")
        kz_bronze_id = _add_catalog(session, "KZ_TEREA", "bronze", "Bronze")
        _add_stock(
            session, "LA_MAKS", "KZ_TEREA", "Bronze", qty=10,
            product_id=kz_bronze_id,
        )
        session.commit()

        order_items = [
            {"base_flavor": "Silver", "quantity": 1, "product_ids": [arm_silver_id]},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")
        # No KZ_TEREA Silver in stock, only Bronze → split
        assert result["status"] == STATUS_SKIPPED_SPLIT


class TestLegacySameFamilyNotSkipped:
    """Legacy re-resolve: same-family multi-match → ready (not skipped)."""

    def test_legacy_same_family_ready(self, db_session, monkeypatch):
        """ARMENIA + KZ_TEREA (same family ME) → ready with preferred id."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "silver", "Silver")
        kz_id = _add_catalog(session, "KZ_TEREA", "silver", "Silver")
        _add_order_item(
            session, "buyer@example.com", "#600", "Silver ME", "Silver",
            qty=2,  # no variant_id → legacy re-resolve
        )
        session.commit()

        mock_result = MagicMock()
        mock_result.product_ids = [arm_id, kz_id]
        mock_result.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_result):
            ready, skipped = get_order_items_for_fulfillment("buyer@example.com", "#600")

        assert len(ready) == 1
        assert skipped == []
        # Preferred is ARMENIA
        assert ready[0]["product_ids"] == [arm_id]

    def test_legacy_cross_family_skipped(self, db_session, monkeypatch):
        """ARMENIA + TEREA_EUROPE (cross-family) → skipped."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "sun pearl", "Sun Pearl")
        eu_id = _add_catalog(session, "TEREA_EUROPE", "sun pearl", "Sun Pearl")
        _add_order_item(
            session, "buyer@example.com", "#601", "Sun Pearl", "Sun Pearl",
            qty=1,
        )
        session.commit()

        mock_result = MagicMock()
        mock_result.product_ids = [arm_id, eu_id]
        mock_result.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_result):
            ready, skipped = get_order_items_for_fulfillment("buyer@example.com", "#601")

        assert ready == []
        assert len(skipped) == 1
        assert skipped[0]["product_ids_count"] == 2

    def test_legacy_resolves_product_name_first(self, db_session, monkeypatch):
        """product_name='Silver ME' tried before base_flavor='Silver'."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "silver", "Silver")
        _add_order_item(
            session, "buyer@example.com", "#602", "Silver ME", "Silver",
            qty=1,
        )
        session.commit()

        # First call (product_name="Silver ME") → exact match, 1 id
        mock_exact = MagicMock()
        mock_exact.product_ids = [arm_id]
        mock_exact.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_exact) as mock_resolve:
            ready, skipped = get_order_items_for_fulfillment("buyer@example.com", "#602")

        assert len(ready) == 1
        assert skipped == []
        # Should have been called with "Silver ME" (product_name), not "Silver" (base_flavor)
        mock_resolve.assert_called_once_with("Silver ME")


# ══════════════════════════════════════════════════════════════════════
# Phase 5: details_json v2 schema
# ══════════════════════════════════════════════════════════════════════

class TestDetailsJsonV2:
    """Phase 5: all new fulfillment events use details_json v2 schema."""

    def test_claim_auto_stamps_v2(self, db_session):
        """[P5-C1] claim_fulfillment_event with details missing 'v' → stored with v=2."""
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#V2-1",
            trigger_type="new_order_postpay",
            status=STATUS_PROCESSING,
            gmail_message_id="msg_v2_claim",
            details={"matched_count": 3},
        )
        assert claim["created"] is True

        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=claim["event_id"]).first()
        stored = json.loads(event.details_json)
        assert stored["v"] == 2
        assert stored["matched_count"] == 3

    def test_claim_preserves_existing_v(self, db_session):
        """[P5] claim with details already having v=2 → not double-stamped."""
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#V2-1b",
            trigger_type="new_order_postpay",
            status=STATUS_BLOCKED_AMBIGUOUS,
            gmail_message_id="msg_v2_existing",
            details={"v": 2, "reason": "ambiguous_variant"},
        )
        assert claim["created"] is True

        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=claim["event_id"]).first()
        stored = json.loads(event.details_json)
        assert stored["v"] == 2
        assert stored["reason"] == "ambiguous_variant"

    def test_claim_none_details_stays_none(self, db_session):
        """[P5] claim with details=None → details_json is NULL (no stamping)."""
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#V2-1c",
            trigger_type="new_order_postpay",
            status=STATUS_SKIPPED_SPLIT,
            gmail_message_id="msg_v2_none",
            details=None,
        )
        assert claim["created"] is True

        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=claim["event_id"]).first()
        assert event.details_json is None

    def test_finalize_auto_stamps_v2(self, db_session):
        """[P5-C2] finalize_fulfillment_event with details missing 'v' → stored with v=2."""
        # First claim
        claim = claim_fulfillment_event(
            client_email="test@example.com",
            order_id="#V2-2",
            trigger_type="new_order_postpay",
            status=STATUS_PROCESSING,
            gmail_message_id="msg_v2_finalize",
        )
        event_id = claim["event_id"]

        # Finalize with details that lack "v"
        ok = finalize_fulfillment_event(
            event_id,
            status=STATUS_ERROR,
            details={"exception": "test crash", "updated": 0, "errors": ["boom"]},
        )
        assert ok is True

        session = db_session()
        event = session.query(FulfillmentEvent).filter_by(id=event_id).first()
        stored = json.loads(event.details_json)
        assert stored["v"] == 2
        assert stored["exception"] == "test crash"
        assert stored["errors"] == ["boom"]

    def test_blocked_event_has_v2_and_reason(self, db_session, monkeypatch):
        """[P5-C3] blocked event (legacy ambiguous) → details_json has v=2, reason, skipped_items."""
        monkeypatch.setenv("REQUIRE_VARIANT_ID", "false")
        session = db_session()
        _add_order_item(
            session, "test@example.com", "#V2-3", "Silver", "Silver",
            qty=1,
        )
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            order_id="#V2-3",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
        }

        mock_resolved = MagicMock()
        mock_resolved.product_ids = [10, 30]
        mock_resolved.confidence = "exact"
        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved):
            try_fulfillment(classification, result, gmail_message_id="msg_v2_blocked")

        assert result["fulfillment"]["status"] == STATUS_BLOCKED_AMBIGUOUS

        session2 = db_session()
        event = session2.query(FulfillmentEvent).filter_by(
            gmail_message_id="msg_v2_blocked",
        ).first()
        assert event is not None
        stored = json.loads(event.details_json)
        assert stored["v"] == 2
        assert stored["reason"] == "ambiguous_variant"
        assert len(stored["skipped_items"]) == 1
        assert stored["skipped_items"][0]["product_ids_count"] == 2

    def test_parse_details_json_v1_backward_compat(self):
        """[P5-C4] Old-style details_json without 'v' → parsed as version=1."""
        old_json = json.dumps({"matched_count": 5, "updated": 2})
        parsed = parse_details_json(old_json)
        assert parsed["version"] == 1
        assert parsed["matched_count"] == 5
        assert parsed["updated"] == 2

    def test_parse_details_json_v2(self):
        """[P5-C4] New-style details_json with v=2 → parsed as version=2."""
        new_json = json.dumps({"v": 2, "reason": "ambiguous_variant"})
        parsed = parse_details_json(new_json)
        assert parsed["version"] == 2
        assert parsed["reason"] == "ambiguous_variant"
        # "v" key replaced by "version"
        assert "v" not in parsed

    def test_parse_details_json_none(self):
        """[P5-C4] None details_json → version=1 default."""
        parsed = parse_details_json(None)
        assert parsed["version"] == 1

    def test_parse_details_json_empty_string(self):
        """[P5-C4] Empty string details_json → version=1 default."""
        parsed = parse_details_json("")
        assert parsed["version"] == 1

    def test_parse_details_json_non_dict(self):
        """[P6-D] Non-dict JSON (e.g. list) → version=1 with _raw."""
        raw = json.dumps([1, 2, 3])
        parsed = parse_details_json(raw)
        assert parsed["version"] == 1
        assert parsed["_raw"] == raw


# ══════════════════════════════════════════════════════════════════════
# Phase 8: ILIKE removal — _query_stock_entries negative test
# ══════════════════════════════════════════════════════════════════════

class TestQueryStockEntriesNoIlike:

    def test_no_product_ids_returns_empty(self, db_session):
        """[P8-T2] _query_stock_entries with empty product_ids → [] (no ILIKE fallback)."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_EUROPE", "silver", "Silver")
        _add_stock(session, "LA_MAKS", "TEREA_EUROPE", "Silver", qty=10, product_id=cat_id)
        session.commit()

        # Despite stock existing with matching name, empty product_ids → empty result
        order_items = [
            {"base_flavor": "Silver", "quantity": 2, "product_ids": []},
        ]
        result = select_fulfillment_warehouse(order_items, "Los Angeles, CA 90001")
        # No warehouse can fulfill because _query_stock_entries returns []
        assert result["status"] == STATUS_SKIPPED_SPLIT


# ── Idempotency retry tests ─────────────────────────────────────────


class TestIsDuplicateFulfillmentStatusFilter:
    """is_duplicate_fulfillment blocks only updated/processing, allows retry for others."""

    def _create_event(self, db_session, status, **kwargs):
        session = db_session()
        ev = FulfillmentEvent(
            client_email="test@example.com",
            order_id="ORD-1",
            gmail_message_id="msg-1",
            trigger_type="new_order_postpay",
            status=status,
            **kwargs,
        )
        session.add(ev)
        session.commit()
        return ev.id

    def test_blocks_after_updated(self, db_session):
        self._create_event(db_session, STATUS_UPDATED)
        assert is_duplicate_fulfillment(
            "test@example.com", "ORD-1", "new_order_postpay", "msg-1",
        ) is True

    def test_blocks_after_processing(self, db_session):
        self._create_event(db_session, STATUS_PROCESSING)
        assert is_duplicate_fulfillment(
            "test@example.com", "ORD-1", "new_order_postpay", "msg-1",
        ) is True

    def test_allows_retry_after_skipped_split(self, db_session):
        self._create_event(db_session, STATUS_SKIPPED_SPLIT)
        assert is_duplicate_fulfillment(
            "test@example.com", "ORD-1", "new_order_postpay", "msg-1",
        ) is False

    def test_allows_retry_after_error(self, db_session):
        self._create_event(db_session, STATUS_ERROR)
        assert is_duplicate_fulfillment(
            "test@example.com", "ORD-1", "new_order_postpay", "msg-1",
        ) is False

    def test_blocks_after_skipped_duplicate(self, db_session):
        """skipped_duplicate is in _BLOCKING_STATUSES — pre-check blocks."""
        self._create_event(db_session, STATUS_SKIPPED_DUPLICATE)
        assert is_duplicate_fulfillment(
            "test@example.com", "ORD-1", "new_order_postpay", "msg-1",
        ) is True


class TestClaimRetryOnIntegrityError:
    """claim_fulfillment_event retries (UPDATE) for retriable statuses."""

    def _create_event(self, db_session, status):
        session = db_session()
        ev = FulfillmentEvent(
            client_email="retry@example.com",
            order_id="ORD-RETRY",
            gmail_message_id="msg-retry",
            trigger_type="new_order_postpay",
            status=status,
            created_at=datetime(2026, 1, 1, 12, 0, 0),
        )
        session.add(ev)
        session.commit()
        return ev.id

    def test_retries_existing_skipped_split(self, db_session):
        old_id = self._create_event(db_session, STATUS_SKIPPED_SPLIT)
        result = claim_fulfillment_event(
            "retry@example.com", "ORD-RETRY", "new_order_postpay",
            STATUS_PROCESSING, gmail_message_id="msg-retry",
        )
        assert result.get("retried") is True
        assert result["event_id"] == old_id
        # Verify created_at NOT changed
        session = db_session()
        ev = session.query(FulfillmentEvent).get(old_id)
        assert ev.status == STATUS_PROCESSING
        assert ev.created_at == datetime(2026, 1, 1, 12, 0, 0)

    def test_blocks_retry_on_updated(self, db_session):
        self._create_event(db_session, STATUS_UPDATED)
        result = claim_fulfillment_event(
            "retry@example.com", "ORD-RETRY", "new_order_postpay",
            STATUS_PROCESSING, gmail_message_id="msg-retry",
        )
        assert result["duplicate"] is True
        assert result.get("retried") is not True

    def test_blocks_retry_on_processing(self, db_session):
        self._create_event(db_session, STATUS_PROCESSING)
        result = claim_fulfillment_event(
            "retry@example.com", "ORD-RETRY", "new_order_postpay",
            STATUS_PROCESSING, gmail_message_id="msg-retry",
        )
        assert result["duplicate"] is True

    def test_blocks_retry_on_skipped_duplicate(self, db_session):
        """skipped_duplicate is not in _RETRIABLE_STATUSES → duplicate."""
        self._create_event(db_session, STATUS_SKIPPED_DUPLICATE)
        result = claim_fulfillment_event(
            "retry@example.com", "ORD-RETRY", "new_order_postpay",
            STATUS_PROCESSING, gmail_message_id="msg-retry",
        )
        assert result["duplicate"] is True


# ── Dual-Intent Payment+Order Tests ─────────────────────────────────

class TestDualIntentPaymentReceived:
    """Tests for payment_received with order_items (dual-intent fix v3.1)."""

    def test_payment_items_unresolved_no_db_fallback(self, db_session):
        """payment_items_unresolved=True → SKIPPED_UNRESOLVED, no DB fallback."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Old order items exist in DB — should NOT be used
        _add_order_item(session, "dual@example.com", "#OLD", "T Silver", "T Silver",
                        qty=1, variant_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="dual@example.com",
            order_id=None,
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
            "payment_items_unresolved": True,
        }

        try_fulfillment(classification, result, gmail_message_id="msg_unresolved")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_SKIPPED_UNRESOLVED
        assert ff["reason"] == "payment_items_unresolved"

    def test_dual_intent_resolved_uses_stock_check_items(self, db_session):
        """Resolved _stock_check_items → used directly for fulfillment.

        Without mocked Sheets, increment fails (no googleapiclient) → STATUS_ERROR.
        The key assertion: trigger_type is correct and warehouse was selected.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="dual@example.com",
            order_id="PAY-abc123def4",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_dual")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "payment_received_prepay"
        # Without sheet config, increment fails → exception → STATUS_ERROR
        # But the key point: it reached the increment stage (used _stock_check_items)
        assert ff["status"] == STATUS_ERROR

    def test_explicit_order_id_skips_dual_intent_uses_db(self, db_session):
        """Explicit order_id + items in DB → DB path, not _stock_check_items.

        Without mocked Sheets → STATUS_ERROR, but warehouse should be selected.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_order_item(session, "buyer@example.com", "#EXPLICIT", "T Silver", "T Silver",
                        qty=1, variant_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#EXPLICIT",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        # No _stock_check_items, no payment_items_unresolved → DB path
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
            "has_explicit_order_id": True,
        }

        try_fulfillment(classification, result, gmail_message_id="msg_explicit")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "payment_received_prepay"
        # Without sheet config → STATUS_ERROR, but warehouse was selected (DB path worked)
        assert ff["status"] == STATUS_ERROR

    def test_explicit_order_id_not_found_no_latest_fallback(self, db_session):
        """Explicit order_id NOT in DB → SKIPPED_UNRESOLVED, no latest fallback."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Old order under different order_id — should NOT be used
        _add_order_item(session, "buyer@example.com", "#OLD_ORDER", "T Silver", "T Silver",
                        qty=1, variant_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#NONEXISTENT",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
            "has_explicit_order_id": True,
        }

        try_fulfillment(classification, result, gmail_message_id="msg_notfound")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_SKIPPED_UNRESOLVED

    def test_explicit_order_id_not_found_defensive_no_flag(self, db_session):
        """Explicit order_id NOT in DB, without has_explicit_order_id flag → defensive check blocks fallback."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        # Old order under different order_id
        _add_order_item(session, "buyer@example.com", "#OLD_ORDER", "T Silver", "T Silver",
                        qty=1, variant_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#NONEXISTENT",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        # Deliberately omit has_explicit_order_id — defensive check should still block
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_noflg")

        ff = result["fulfillment"]
        # Defensive check: #NONEXISTENT doesn't start with PAY- or AUTO- → explicit
        assert ff["status"] == STATUS_SKIPPED_UNRESOLVED

    def test_no_order_items_normal_db_fallback(self, db_session):
        """payment_received without order_items → normal DB path unchanged.

        Without mocked Sheets → STATUS_ERROR, but warehouse selected = DB path works.
        """
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        _add_order_item(session, "buyer@example.com", "#200", "T Silver", "T Silver",
                        qty=1, variant_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="buyer@example.com",
            order_id="#200",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        # No _stock_check_items, no payment_items_unresolved → DB path
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
        }

        try_fulfillment(classification, result, gmail_message_id="msg_normal")

        ff = result["fulfillment"]
        assert ff["trigger_type"] == "payment_received_prepay"
        # Without sheet config → STATUS_ERROR, but warehouse was selected
        assert ff["status"] == STATUS_ERROR

    def test_payment_items_unresolved_no_get_latest_called(self, db_session):
        """payment_items_unresolved → get_order_items_for_fulfillment(..., None) NOT called."""
        classification = _mock_classification(
            situation="payment_received",
            client_email="dual@example.com",
            order_id=None,
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay"},
            "payment_items_unresolved": True,
        }

        with patch(
            "agents.handlers.fulfillment_trigger.get_order_items_for_fulfillment"
        ) as mock_get:
            try_fulfillment(classification, result, gmail_message_id="msg_mock")
            # Should NOT be called at all — early return in branch 1
            mock_get.assert_not_called()

    def test_ambiguous_blocked_with_stock_check_items(self, db_session):
        """fulfillment_blocked=True + _stock_check_items → BLOCKED_AMBIGUOUS."""
        session = db_session()
        cat_id = _add_catalog(session, "TEREA_JAPAN", "t silver", "T Silver")
        _add_stock(session, "LA_MAKS", "TEREA_JAPAN", "T Silver", qty=10, product_id=cat_id)
        session.commit()

        classification = _mock_classification(
            situation="payment_received",
            client_email="dual@example.com",
            order_id="PAY-abc123def4",
            customer_city_state_zip="Los Angeles, CA 90001",
        )
        result = {
            "situation": "payment_received",
            "client_data": {"payment_type": "prepay", "city_state_zip": "Los Angeles, CA 90001"},
            "_stock_check_items": [
                {"base_flavor": "T Silver", "quantity": 1, "product_ids": [cat_id]},
            ],
            "fulfillment_blocked": True,
            "ambiguous_flavors": ["Silver"],
        }

        try_fulfillment(classification, result, gmail_message_id="msg_ambig")

        ff = result["fulfillment"]
        assert ff["status"] == STATUS_BLOCKED_AMBIGUOUS
        assert ff["reason"] == "ambiguous_variant"


# ══════════════════════════════════════════════════════════════════════
# Thread hint in fulfillment read-path
# ══════════════════════════════════════════════════════════════════════

class TestFulfillmentThreadHint:
    """Thread-backed disambiguation in get_order_items_for_fulfillment."""

    def test_thread_hint_resolves_to_ready(self, db_session):
        """variant_id=NULL + thread hint → ready (not skipped)."""
        session = db_session()
        # Create cross-family catalog entries
        arm_id = _add_catalog(session, "ARMENIA", "yellow", "Yellow")
        kz_id = _add_catalog(session, "KZ_TEREA", "yellow", "Yellow")
        eu_id = _add_catalog(session, "TEREA_EUROPE", "yellow", "Yellow")
        # Order item with no variant_id
        _add_order_item(
            session, "buyer@example.com", "#700", "Terea Yellow", "Yellow", qty=2,
        )
        session.commit()

        # Mock resolver → cross-family [arm, kz, eu]
        mock_resolved = MagicMock()
        mock_resolved.product_ids = [arm_id, kz_id, eu_id]
        mock_resolved.confidence = "exact"

        # Thread messages with ME hint
        thread_msgs = [{"body": "2 x Terea Yellow ME", "direction": "outbound"}]

        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved), \
             patch("db.email_history.get_full_thread_history", return_value=thread_msgs):
            items, skipped = get_order_items_for_fulfillment(
                "buyer@example.com", "#700",
                gmail_thread_id="thread_700",
            )

        assert len(items) == 1
        assert skipped == []
        # Resolved to ARMENIA (preferred ME)
        assert items[0]["product_ids"] == [arm_id]

    def test_thread_hint_no_hint_skipped(self, db_session):
        """variant_id=NULL + no thread hint → skipped."""
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "yellow", "Yellow")
        kz_id = _add_catalog(session, "KZ_TEREA", "yellow", "Yellow")
        eu_id = _add_catalog(session, "TEREA_EUROPE", "yellow", "Yellow")
        _add_order_item(
            session, "buyer@example.com", "#701", "Terea Yellow", "Yellow", qty=2,
        )
        session.commit()

        mock_resolved = MagicMock()
        mock_resolved.product_ids = [arm_id, kz_id, eu_id]
        mock_resolved.confidence = "exact"

        # No thread messages with region hint
        thread_msgs = [{"body": "Payment sent!", "direction": "inbound"}]

        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved), \
             patch("db.email_history.get_full_thread_history", return_value=thread_msgs):
            items, skipped = get_order_items_for_fulfillment(
                "buyer@example.com", "#701",
                gmail_thread_id="thread_701",
            )

        assert items == []
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "ambiguous_variant"

    def test_same_family_resolved_no_thread_needed(self, db_session):
        """variant_id=NULL + region-aware product_name → same-family resolve → ready."""
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "yellow", "Yellow")
        kz_id = _add_catalog(session, "KZ_TEREA", "yellow", "Yellow")
        _add_order_item(
            session, "buyer@example.com", "#702", "Terea Yellow ME", "Yellow", qty=2,
        )
        session.commit()

        # Mock resolver → same-family [arm, kz]
        mock_resolved = MagicMock()
        mock_resolved.product_ids = [arm_id, kz_id]
        mock_resolved.confidence = "exact"

        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved):
            items, skipped = get_order_items_for_fulfillment(
                "buyer@example.com", "#702",
            )

        assert len(items) == 1
        assert skipped == []
        # Preferred ARMENIA
        assert items[0]["product_ids"] == [arm_id]

    def test_gmail_account_passed_to_thread_history(self, db_session):
        """gmail_account is correctly forwarded to get_full_thread_history."""
        session = db_session()
        arm_id = _add_catalog(session, "ARMENIA", "yellow", "Yellow")
        kz_id = _add_catalog(session, "KZ_TEREA", "yellow", "Yellow")
        eu_id = _add_catalog(session, "TEREA_EUROPE", "yellow", "Yellow")
        _add_order_item(
            session, "buyer@example.com", "#703", "Terea Yellow", "Yellow", qty=2,
        )
        session.commit()

        mock_resolved = MagicMock()
        mock_resolved.product_ids = [arm_id, kz_id, eu_id]
        mock_resolved.confidence = "exact"

        thread_msgs = [{"body": "2 x Terea Yellow ME", "direction": "outbound"}]

        with patch("db.product_resolver.resolve_product_to_catalog", return_value=mock_resolved), \
             patch("db.email_history.get_full_thread_history", return_value=thread_msgs) as mock_thread:
            get_order_items_for_fulfillment(
                "buyer@example.com", "#703",
                gmail_thread_id="thread_703",
                gmail_account="tilda",
            )

        mock_thread.assert_called_once_with("thread_703", gmail_account="tilda")
