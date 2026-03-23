"""Tests for auto_mode hold feature.

Tests _predict_hold logic, deferred email DB operations,
format_hold_result formatting, and UPSERT behavior.
"""

import pytest
from unittest.mock import MagicMock

from db.email_history import (
    save_email,
    email_already_processed,
    email_is_deferred,
    finalize_deferred,
)
from db.models import EmailHistory
from agents.formatters import format_hold_result
from agents.pipeline import _predict_hold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_classification(situation="new_order", needs_reply=True,
                         email="test@example.com", name="Test",
                         order_id="ORD-123", items="Terea Amber"):
    c = MagicMock()
    c.situation = situation
    c.needs_reply = needs_reply
    c.client_email = email
    c.client_name = name
    c.order_id = order_id
    c.items = items
    return c


# ═══════════════════════════════════════════════════════════════════════════
# _predict_hold tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPredictHold:
    """Test _predict_hold() side-effect-free predictor."""

    # -- auto_mode=False ------------------------------------------------

    def test_auto_mode_false_never_holds(self):
        c = _make_classification()
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"}}
        hold, reason = _predict_hold(False, c, result, None)
        assert hold is False
        assert reason is None

    # -- unknown client -------------------------------------------------

    def test_unknown_client_holds(self):
        c = _make_classification()
        result = {"client_found": False, "needs_reply": True, "client_data": None}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is True
        assert reason == "unknown_client"

    def test_unknown_client_no_reply_not_hold(self):
        c = _make_classification()
        result = {"client_found": False, "needs_reply": False, "client_data": None}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    # -- new_order / postpay (final confirmation) -----------------------

    def test_new_order_postpay_no_oos_holds(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is True
        assert reason == "final_confirmation"

    def test_new_order_postpay_stock_issue_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"},
                  "stock_issue": {"some": "data"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_new_order_postpay_all_oos_optional_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"},
                  "all_oos_optional": True}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_new_order_postpay_decision_required_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"},
                  "availability_resolution": {"decision_required": True}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_new_order_postpay_unresolved_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"},
                  "unresolved_context": "some products unresolved"}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    # -- payment_received / prepay (final confirmation) -----------------

    def test_payment_received_prepay_no_oos_holds(self):
        c = _make_classification(situation="payment_received")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is True
        assert reason == "final_confirmation"

    def test_payment_received_prepay_pending_oos_not_hold(self):
        c = _make_classification(situation="payment_received")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        pre_state = {"state": {"facts": {"pending_oos_resolution": {"items": []}}}}
        hold, reason = _predict_hold(True, c, result, pre_state)
        assert hold is False

    # -- non-hold situations --------------------------------------------

    def test_new_order_prepay_not_hold(self):
        """new_order + prepay = payment instructions (not confirmation)."""
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_tracking_not_hold(self):
        c = _make_classification(situation="tracking")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_stock_question_not_hold(self):
        c = _make_classification(situation="stock_question")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "postpay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_needs_reply_false_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": False,
                  "client_data": {"payment_type": "postpay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_client_found_but_no_reply_not_hold(self):
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": False,
                  "client_data": {"payment_type": "postpay"}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    # -- edge cases -----------------------------------------------------

    def test_no_payment_type_not_hold(self):
        """Missing payment_type should not match any hold branch."""
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {}}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_client_data_none_but_client_found_not_hold(self):
        """client_found=True but client_data=None (defensive)."""
        c = _make_classification(situation="new_order")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": None}
        hold, reason = _predict_hold(True, c, result, None)
        assert hold is False

    def test_pre_state_none_payment_received_prepay_holds(self):
        """pre_state_record=None should not crash; no pending_oos → hold."""
        c = _make_classification(situation="payment_received")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        hold, reason = _predict_hold(True, c, result, pre_state_record=None)
        assert hold is True
        assert reason == "final_confirmation"

    def test_pre_state_empty_facts_payment_received_prepay_holds(self):
        """pre_state with empty facts → no pending_oos → hold."""
        c = _make_classification(situation="payment_received")
        result = {"client_found": True, "needs_reply": True,
                  "client_data": {"payment_type": "prepay"}}
        pre_state = {"state": {"facts": {}}}
        hold, reason = _predict_hold(True, c, result, pre_state)
        assert hold is True
        assert reason == "final_confirmation"


# ═══════════════════════════════════════════════════════════════════════════
# Deferred DB operations
# ═══════════════════════════════════════════════════════════════════════════

class TestDeferredDB:
    """Test deferred email DB operations (UPSERT, email_is_deferred, finalize)."""

    def test_save_email_with_deferred_flag(self, db_session):
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="Order",
            body="I want to order",
            situation="new_order",
            gmail_message_id="msg_001",
            gmail_thread_id="thread_001",
            deferred=True,
        )
        assert email_already_processed("msg_001")
        assert email_is_deferred("msg_001")

    def test_save_email_default_not_deferred(self, db_session):
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="Order",
            body="body",
            situation="new_order",
            gmail_message_id="msg_002",
        )
        assert email_already_processed("msg_002")
        assert not email_is_deferred("msg_002")

    def test_upsert_updates_deferred_and_situation(self, db_session):
        # First save: deferred
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="Order",
            body="original body",
            situation="new_order",
            gmail_message_id="msg_003",
            gmail_thread_id="thread_001",
            deferred=True,
        )
        assert email_is_deferred("msg_003")

        # UPSERT: same gmail_message_id, deferred=False
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="Order (updated)",
            body="updated body",
            situation="payment_received",
            gmail_message_id="msg_003",
            gmail_thread_id="thread_001",
            deferred=False,
        )
        assert not email_is_deferred("msg_003")
        assert email_already_processed("msg_003")

        # Verify body and subject preserved (NOT overwritten by UPSERT)
        session = db_session()
        record = session.query(EmailHistory).filter_by(gmail_message_id="msg_003").first()
        assert record.body == "original body"
        assert record.subject == "Order"
        assert record.situation == "payment_received"  # situation IS updated
        assert record.deferred is False
        session.close()

    def test_upsert_preserves_created_at(self, db_session):
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="S",
            body="B",
            situation="new_order",
            gmail_message_id="msg_004",
            deferred=True,
        )
        session = db_session()
        original = session.query(EmailHistory).filter_by(gmail_message_id="msg_004").first()
        original_ts = original.created_at
        session.close()

        # UPSERT
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="S2",
            body="B2",
            situation="payment_received",
            gmail_message_id="msg_004",
            deferred=False,
        )
        session = db_session()
        record = session.query(EmailHistory).filter_by(gmail_message_id="msg_004").first()
        assert record.created_at == original_ts
        session.close()

    def test_finalize_deferred(self, db_session):
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="S",
            body="B",
            situation="new_order",
            gmail_message_id="msg_005",
            deferred=True,
        )
        assert email_is_deferred("msg_005")

        finalize_deferred("msg_005")

        assert not email_is_deferred("msg_005")
        assert email_already_processed("msg_005")

        # Verify body preserved
        session = db_session()
        record = session.query(EmailHistory).filter_by(gmail_message_id="msg_005").first()
        assert record.body == "B"
        assert record.situation == "new_order"
        session.close()

    def test_finalize_nonexistent_is_noop(self, db_session):
        """finalize_deferred on non-existent message_id should not raise."""
        finalize_deferred("msg_nonexistent")

    def test_email_is_deferred_nonexistent(self, db_session):
        assert not email_is_deferred("msg_nonexistent")

    def test_save_email_no_gmail_message_id_always_inserts(self, db_session):
        """Without gmail_message_id, save_email always inserts (no UPSERT)."""
        save_email(
            client_email="test@example.com",
            direction="outbound",
            subject="Re: Order",
            body="Reply 1",
            situation="new_order",
            gmail_thread_id="thread_001",
        )
        save_email(
            client_email="test@example.com",
            direction="outbound",
            subject="Re: Order",
            body="Reply 2",
            situation="new_order",
            gmail_thread_id="thread_001",
        )
        session = db_session()
        count = session.query(EmailHistory).filter_by(direction="outbound").count()
        assert count == 2
        session.close()

    def test_finalize_already_finalized_is_noop(self, db_session):
        """Finalizing an already non-deferred email does nothing."""
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="S",
            body="B",
            situation="new_order",
            gmail_message_id="msg_006",
            deferred=False,
        )
        # Should not raise or change anything
        finalize_deferred("msg_006")
        assert not email_is_deferred("msg_006")
        assert email_already_processed("msg_006")

    def test_double_finalize_is_idempotent(self, db_session):
        """Calling finalize_deferred twice should be safe."""
        save_email(
            client_email="test@example.com",
            direction="inbound",
            subject="S",
            body="B",
            situation="new_order",
            gmail_message_id="msg_007",
            deferred=True,
        )
        finalize_deferred("msg_007")
        finalize_deferred("msg_007")
        assert not email_is_deferred("msg_007")


# ═══════════════════════════════════════════════════════════════════════════
# format_hold_result tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatHoldResult:
    """Test format_hold_result formatting."""

    def test_unknown_client_format(self):
        c = _make_classification(email="john@example.com", name="John")
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert text.startswith("✋ HOLD:")
        assert "Клиент не в базе" in text
        assert "john@example.com" in text
        assert "process_email" in text

    def test_unknown_client_includes_situation(self):
        c = _make_classification(situation="tracking", email="a@example.com")
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert "tracking" in text

    def test_unknown_client_includes_order_id(self):
        c = _make_classification(order_id="ORD-999")
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert "ORD-999" in text

    def test_unknown_client_includes_items(self):
        c = _make_classification(items="Terea Amber x2")
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert "Terea Amber x2" in text

    def test_unknown_client_no_name(self):
        c = _make_classification(name=None)
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert "не указано" in text

    def test_final_confirmation_format(self):
        c = _make_classification(situation="new_order", email="john@example.com")
        result = {
            "client_data": {"payment_type": "postpay"},
            "calculated_price": 150.0,
            "order_summary": "2x Terea Amber",
        }
        text = format_hold_result(c, result, "final_confirmation")
        assert text.startswith("✋ HOLD:")
        assert "Финальное подтверждение" in text
        assert "new_order" in text
        assert "postpay" in text
        assert "$150.00" in text
        assert "process_email" in text

    def test_final_confirmation_includes_order_summary(self):
        c = _make_classification()
        result = {
            "client_data": {"payment_type": "prepay"},
            "calculated_price": 200.0,
            "order_summary": "3x Terea Silver",
        }
        text = format_hold_result(c, result, "final_confirmation")
        assert "3x Terea Silver" in text

    def test_final_confirmation_no_price(self):
        """Missing calculated_price should not crash."""
        c = _make_classification()
        result = {
            "client_data": {"payment_type": "postpay"},
        }
        text = format_hold_result(c, result, "final_confirmation")
        assert "Финальное подтверждение" in text
        assert "$" not in text  # no price line

    def test_final_confirmation_no_order_summary(self):
        """Missing order_summary should not crash."""
        c = _make_classification()
        result = {
            "client_data": {"payment_type": "postpay"},
            "calculated_price": 100.0,
        }
        text = format_hold_result(c, result, "final_confirmation")
        assert "$100.00" in text

    def test_hold_prefix_for_poller_detection(self):
        """All hold results must start with the HOLD prefix for poller parsing."""
        c = _make_classification()
        result = {"client_data": None}
        text = format_hold_result(c, result, "unknown_client")
        assert text.startswith("✋ HOLD:")

    def test_unknown_reason_fallback(self):
        """Unknown hold_reason should still produce a valid HOLD message."""
        c = _make_classification()
        result = {"client_data": None}
        text = format_hold_result(c, result, "some_future_reason")
        assert text.startswith("✋ HOLD:")
        assert "some_future_reason" in text


# ---------------------------------------------------------------------------
# Integration: pipeline hold early-return
# ---------------------------------------------------------------------------

class TestPipelineHoldIntegration:
    """Integration tests: classify_and_process with auto_mode=True.

    Uses patches to stub external dependencies (LLM, Gmail, Telegram)
    while running the real pipeline logic including _predict_hold and
    _persist_results.
    """

    def _run_pipeline(self, email_text, client_data, situation="new_order",
                      auto_mode=True, gmail_message_id="msg_int_001",
                      gmail_thread_id="thread_int_001",
                      pre_state=None, order_items=None):
        """Run classify_and_process with minimal stubs."""
        from unittest.mock import patch, MagicMock
        import types as _types
        from agents.pipeline import classify_and_process

        # Build fake classification
        fake_classification = MagicMock()
        fake_classification.situation = situation
        fake_classification.needs_reply = True
        fake_classification.client_email = client_data.get("email", "test@example.com") if client_data else "unknown@example.com"
        fake_classification.client_name = client_data.get("name", "Test") if client_data else "Unknown"
        fake_classification.order_id = "ORD-INT-1"
        fake_classification.price = "$100"
        fake_classification.items = "Terea Amber x2"
        fake_classification.order_items = order_items or []
        fake_classification.customer_street = ""
        fake_classification.customer_city_state_zip = ""
        fake_classification.dialog_intent = None
        fake_classification.followup_to = None
        fake_classification.parser_used = False

        saved_emails = []
        original_save = save_email

        def tracking_save(*args, **kwargs):
            saved_emails.append(kwargs)
            original_save(*args, **kwargs)

        patches = [
            patch("agents.pipeline.build_classifier_context",
                  return_value=("", pre_state, None)),
            patch("agents.pipeline.run_classification",
                  return_value=fake_classification),
            patch("agents.pipeline.get_client",
                  return_value=client_data),
            patch("agents.pipeline.get_stock_summary",
                  return_value={"total": 100}),
            patch("agents.pipeline.save_email", side_effect=tracking_save),
            patch("agents.pipeline.save_order_items", return_value=None),
            patch("agents.pipeline.replace_order_items", return_value=0),
            patch("agents.pipeline.update_conversation_state", return_value={}),
            patch("agents.pipeline.save_state", return_value=None),
            patch("agents.pipeline.send_telegram"),
            patch("agents.pipeline.check_reply",
                  return_value=MagicMock(is_ok=True, warnings=[], suggestions=[],
                                         rule_violations=[], llm_issues=[])),
            patch("agents.pipeline.resolve_order_items",
                  side_effect=lambda items, **kw: (items, [])),
            patch("agents.pipeline.check_stock_for_order",
                  return_value={"all_in_stock": True, "items": [], "insufficient_items": []}),
            patch("agents.pipeline.calculate_order_price", return_value=None),
            patch("agents.pipeline.select_best_alternatives",
                  return_value={"alternatives": []}),
            patch("agents.pipeline.update_client", return_value=None),
        ]

        for p in patches:
            p.start()
        try:
            result_str = classify_and_process(
                email_text,
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                auto_mode=auto_mode,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        return result_str, saved_emails

    def test_hold_unknown_client_returns_hold_string(self, db_session):
        """auto_mode=True + unknown client → HOLD result, no outbound saved."""
        result_str, saved = self._run_pipeline(
            email_text="From: unknown@example.com\nSubject: Order\nBody: I want to order",
            client_data=None,
            situation="new_order",
            auto_mode=True,
        )
        assert result_str.startswith("✋ HOLD:")
        assert "Клиент не в базе" in result_str
        # Only inbound saved, no outbound
        inbound_saves = [s for s in saved if s.get("direction") == "inbound"]
        outbound_saves = [s for s in saved if s.get("direction") == "outbound"]
        assert len(inbound_saves) == 1
        assert inbound_saves[0]["deferred"] is True
        assert len(outbound_saves) == 0

    def test_hold_final_confirmation_postpay(self, db_session):
        """auto_mode=True + new_order/postpay → HOLD, no outbound/state."""
        client = {
            "email": "client@example.com", "name": "Client",
            "payment_type": "postpay", "zelle_address": "",
            "discount_percent": 0, "discount_orders_left": 0,
        }
        result_str, saved = self._run_pipeline(
            email_text="From: client@example.com\nSubject: Order\nBody: 2x Terea Amber",
            client_data=client,
            situation="new_order",
            auto_mode=True,
        )
        assert result_str.startswith("✋ HOLD:")
        assert "Финальное подтверждение" in result_str
        inbound = [s for s in saved if s.get("direction") == "inbound"]
        outbound = [s for s in saved if s.get("direction") == "outbound"]
        assert len(inbound) == 1
        assert inbound[0]["deferred"] is True
        assert len(outbound) == 0

    def test_hold_payment_received_prepay(self, db_session):
        """auto_mode=True + payment_received/prepay → HOLD."""
        client = {
            "email": "client@example.com", "name": "Client",
            "payment_type": "prepay", "zelle_address": "pay@z.com",
            "discount_percent": 0, "discount_orders_left": 0,
        }
        result_str, saved = self._run_pipeline(
            email_text="From: client@example.com\nSubject: Payment\nBody: I sent payment",
            client_data=client,
            situation="payment_received",
            auto_mode=True,
        )
        assert result_str.startswith("✋ HOLD:")
        assert "Финальное подтверждение" in result_str

    def test_no_hold_prepay_new_order(self, db_session):
        """auto_mode=True + new_order/prepay → NOT hold (Zelle template)."""
        client = {
            "email": "client@example.com", "name": "Client",
            "payment_type": "prepay", "zelle_address": "pay@z.com",
            "discount_percent": 0, "discount_orders_left": 0,
        }
        result_str, saved = self._run_pipeline(
            email_text="From: client@example.com\nSubject: Order\nBody: 2x Terea Amber",
            client_data=client,
            situation="new_order",
            auto_mode=True,
        )
        # Not held — should NOT start with HOLD prefix
        assert not result_str.startswith("✋ HOLD:")

    def test_manual_mode_postpay_creates_full_result(self, db_session):
        """auto_mode=False + new_order/postpay → full processing, not held."""
        client = {
            "email": "client@example.com", "name": "Client",
            "payment_type": "postpay", "zelle_address": "",
            "discount_percent": 0, "discount_orders_left": 0,
        }
        result_str, saved = self._run_pipeline(
            email_text="From: client@example.com\nSubject: Order\nBody: 2x Terea Amber",
            client_data=client,
            situation="new_order",
            auto_mode=False,
        )
        assert not result_str.startswith("✋ HOLD:")

    def test_hold_deferred_flag_in_db(self, db_session):
        """Verify deferred=True is actually written to DB via _persist_results."""
        self._run_pipeline(
            email_text="From: unknown@example.com\nSubject: Hi\nBody: Hello",
            client_data=None,
            situation="new_order",
            auto_mode=True,
            gmail_message_id="msg_db_check",
        )
        assert email_already_processed("msg_db_check")
        assert email_is_deferred("msg_db_check")


# ---------------------------------------------------------------------------
# Integration: poller pre-reconcile
# ---------------------------------------------------------------------------

def _try_import_poller():
    """Try to import tools.gmail_poller; return module or None."""
    try:
        import tools.gmail_poller
        return tools.gmail_poller
    except Exception:
        return None


_poller_mod = _try_import_poller()


@pytest.mark.skipif(_poller_mod is None, reason="tools.gmail_poller requires full agno stack")
class TestPollerPreReconcile:
    """Test pre-reconcile logic in process_client_email.

    Skipped when tools.gmail_poller can't be imported (requires agno + full deps).
    These tests run in environments where the full stack is available (e.g., Docker).
    """

    def test_deferred_with_manual_reply_finalized(self, db_session):
        """Deferred message + newer outbound in thread → finalized."""
        from datetime import datetime, timezone
        from unittest.mock import patch, MagicMock

        ts_inbound = datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc)
        ts_outbound = datetime(2026, 3, 23, 11, 0, tzinfo=timezone.utc)
        save_email(
            client_email="c@example.com",
            direction="inbound",
            subject="Order",
            body="I want to order",
            situation="new_order",
            gmail_message_id="msg_def_001",
            gmail_thread_id="thread_def_001",
            deferred=True,
        )

        mock_client = MagicMock()
        mock_client.search_unread_from.return_value = [{"msg_id": "msg_def_001"}]
        mock_client.search_unread_order_notifications.return_value = []
        mock_client.get_message.return_value = {
            "from": "c@example.com",
            "from_raw": "c@example.com",
            "reply_to": "",
            "subject": "Order",
            "body": "I want to order",
            "gmail_message_id": "msg_def_001",
            "gmail_thread_id": "thread_def_001",
            "created_at": ts_inbound,
        }
        mock_client.fetch_thread.return_value = [
            {"direction": "inbound", "created_at": ts_inbound,
             "gmail_message_id": "msg_def_001", "client_email": "c@example.com"},
            {"direction": "outbound", "created_at": ts_outbound,
             "gmail_message_id": "msg_out_001", "client_email": "c@example.com"},
        ]

        with patch.object(_poller_mod, "_get_client", return_value=mock_client), \
             patch.object(_poller_mod, "getenv", return_value="fake_token"):
            result = _poller_mod.process_client_email("c@example.com")

        assert "закрыты" in result or "ответил вручную" in result
        assert not email_is_deferred("msg_def_001")

    def test_deferred_no_outbound_processes(self, db_session):
        """Deferred message + no outbound → stays unprocessed, goes to pipeline."""
        from datetime import datetime, timezone
        from unittest.mock import patch, MagicMock

        ts_inbound = datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc)
        save_email(
            client_email="c@example.com",
            direction="inbound",
            subject="Order",
            body="I want to order",
            situation="new_order",
            gmail_message_id="msg_def_002",
            gmail_thread_id="thread_def_002",
            deferred=True,
        )

        mock_client = MagicMock()
        mock_client.search_unread_from.return_value = [{"msg_id": "msg_def_002"}]
        mock_client.search_unread_order_notifications.return_value = []
        mock_client.get_message.return_value = {
            "from": "c@example.com",
            "from_raw": "c@example.com",
            "reply_to": "",
            "subject": "Order",
            "body": "I want to order",
            "gmail_message_id": "msg_def_002",
            "gmail_thread_id": "thread_def_002",
            "created_at": ts_inbound,
        }
        mock_client.fetch_thread.return_value = [
            {"direction": "inbound", "created_at": ts_inbound,
             "gmail_message_id": "msg_def_002", "client_email": "c@example.com"},
        ]

        mock_classify = MagicMock(return_value="Processed result")

        with patch.object(_poller_mod, "_get_client", return_value=mock_client), \
             patch.object(_poller_mod, "getenv", return_value="fake_token"), \
             patch.object(_poller_mod, "classify_and_process", mock_classify), \
             patch.object(_poller_mod, "_send_telegram_result"):
            result = _poller_mod.process_client_email("c@example.com")

        mock_classify.assert_called_once()
        # auto_mode not passed (defaults to False for manual trigger)
        _, kwargs = mock_classify.call_args
        assert kwargs.get("auto_mode") is not True
