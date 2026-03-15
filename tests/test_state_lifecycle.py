"""Tests for conversation_state lifecycle — the critical paths that MUST work
before State Updater can be simplified or removed.

Tests cover:
1. pending_oos_resolution is written correctly by _persist_results
2. pending_oos_resolution is protected during _update_inbound_state
3. payment_received handler reads pending_oos_resolution for prepay guard
4. oos_agreement reads and clears pending_oos_resolution
5. oos_qty_utils reads pending_oos_resolution for merge and enrich
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Module stubs (same pattern as test_oos_followup_intents.py)
# ---------------------------------------------------------------------------
def _install_stubs():
    """Install stubs for agno + DB modules not available in test env."""
    # Clear cached handler modules
    for name in list(sys.modules):
        if name.startswith("agents."):
            sys.modules.pop(name, None)

    # agno stubs
    if "agno" not in sys.modules:
        agno = types.ModuleType("agno")
        agno.__path__ = []
        sys.modules["agno"] = agno
    if "agno.agent" not in sys.modules:
        agno_agent = types.ModuleType("agno.agent")

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, prompt):
                resp = MagicMock()
                resp.content = '{"status": "new"}'
                return resp

        agno_agent.Agent = FakeAgent
        sys.modules["agno.agent"] = agno_agent
    if "agno.models" not in sys.modules:
        agno_models = types.ModuleType("agno.models")
        agno_models.__path__ = []
        sys.modules["agno.models"] = agno_models
    if "agno.models.openai" not in sys.modules:
        agno_openai = types.ModuleType("agno.models.openai")

        class FakeOpenAIResponses:
            def __init__(self, *args, **kwargs):
                pass

        agno_openai.OpenAIResponses = FakeOpenAIResponses
        sys.modules["agno.models.openai"] = agno_openai

    # DB stubs
    db_stub_modules = [
        "db", "db.models", "db.memory", "db.conversation_state",
        "db.catalog", "db.region_family", "db.fulfillment",
        "db.region_preference", "db.stock", "db.email_history",
        "db.shipping",
    ]
    for mod_name in db_stub_modules:
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            if mod_name == "db":
                m.__path__ = []
            sys.modules[mod_name] = m

    db_models = sys.modules["db.models"]
    db_models.get_session = MagicMock()
    db_models.ConversationState = MagicMock()
    db_models.Base = MagicMock()

    db_cs = sys.modules["db.conversation_state"]
    db_cs.get_state = lambda *a, **kw: None
    db_cs.save_state = MagicMock()
    db_cs.get_client_states = lambda *a, **kw: []

    db_catalog = sys.modules["db.catalog"]
    db_catalog.get_display_name = lambda name, cat="": name
    db_catalog.get_base_display_name = lambda name: name
    db_catalog._enrich_display_name_with_region = lambda items: items
    db_catalog.get_catalog_products = lambda: []

    db_region = sys.modules["db.region_family"]
    db_region.CATEGORY_REGION_SUFFIX = dict()
    db_region.is_same_family = lambda a, b: False

    db_region_pref = sys.modules["db.region_preference"]
    db_region_pref.apply_region_preference = lambda items, **kw: items
    db_region_pref.apply_thread_hint = lambda items, **kw: items

    db_stock = sys.modules["db.stock"]
    db_stock.extract_variant_id = lambda *a, **kw: None
    db_stock.has_ambiguous_variants = lambda *a, **kw: False

    db_email_history = sys.modules["db.email_history"]
    db_email_history.get_full_thread_history = lambda *a, **kw: []

    db_memory = sys.modules["db.memory"]
    db_memory.save_email = MagicMock()
    db_memory.save_order_items = MagicMock()
    db_memory.get_client = lambda *a, **kw: None
    db_memory.get_stock_summary = lambda *a, **kw: ""
    db_memory.calculate_order_price = lambda items: 100.0
    db_memory.check_stock_for_order = lambda items: {"all_in_stock": True, "items": items, "insufficient_items": []}
    db_memory.resolve_order_items = lambda items, **kw: (items, [])
    db_memory.replace_order_items = MagicMock()

    db_shipping = sys.modules["db.shipping"]
    db_shipping.save_order_shipping_address = MagicMock()

    # utils stubs
    for mod_name in ["utils", "utils.gmail", "utils.telegram"]:
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            if mod_name == "utils":
                m.__path__ = []
            sys.modules[mod_name] = m

    gmail_mod = sys.modules["utils.gmail"]
    gmail_mod.create_draft = MagicMock(return_value="draft-123")
    gmail_mod.get_full_thread_history = MagicMock(return_value=[])

    tg_mod = sys.modules["utils.telegram"]
    tg_mod.send_telegram = MagicMock()
    tg_mod.send_telegram_async = MagicMock()


_install_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_stock_issue():
    """Minimal stock_issue dict as produced by process_classified_email."""
    return {
        "stock_check": {
            "all_in_stock": False,
            "items": [
                {
                    "base_flavor": "Silver",
                    "product_name": "Silver",
                    "ordered_qty": 3,
                    "total_available": 50,
                    "is_sufficient": True,
                },
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 5,
                    "total_available": 0,
                    "is_sufficient": False,
                },
            ],
            "insufficient_items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 5,
                    "total_available": 0,
                },
            ],
        },
        "best_alternatives": {
            "Green": {
                "alternatives": [
                    {
                        "alternative": {
                            "product_name": "Turquoise",
                            "category": "TEREA_EUROPE",
                        }
                    }
                ]
            },
        },
    }


def _make_pending_oos():
    """Expected pending_oos_resolution after _persist_results processes stock_issue."""
    return {
        "items": [
            {
                "base_flavor": "Green",
                "product_name": "Green",
                "requested_qty": 5,
                "available_qty": 0,
            }
        ],
        "alternatives": {
            "Green": {
                "alternatives": [
                    {"product_name": "Turquoise", "category": "TEREA_EUROPE"}
                ]
            },
        },
        "in_stock_items": [
            {
                "base_flavor": "Silver",
                "product_name": "Silver",
                "ordered_qty": 3,
            }
        ],
    }


class _FakeClassification:
    def __init__(self, **kwargs):
        defaults = {
            "situation": "new_order",
            "client_email": "test@example.com",
            "client_name": "Test",
            "needs_reply": True,
            "order_id": "ORD-999",
            "price": "$220",
            "order_items": [],
            "dialog_intent": None,
            "followup_to": None,
            "customer_street": None,
            "customer_city_state_zip": None,
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: _persist_results writes pending_oos_resolution correctly
# ═══════════════════════════════════════════════════════════════════════════

class TestPersistResultsWritesPendingOOS(unittest.TestCase):
    """Verify that _persist_results builds pending_oos_resolution from stock_issue."""

    def _base_result(self, state, **overrides):
        """Build a minimal result dict for _persist_results."""
        r = {
            "needs_reply": True,
            "draft_reply": "Draft reply...",
            "template_used": True,
            "stock_issue": None,
            "conversation_state": state,
            "client_email": "test@example.com",
            "client_name": "Test",
            "client_found": True,
            "client_data": {"name": "Test"},
            "situation": "new_order",
            "needs_routing": False,
            "effective_situation": None,
            "confirmation_source": None,
            "canonical_confirmed_items": None,
            "_stock_check_items": None,
            "gmail_thread_id": "thread-abc",
            "gmail_account": "default",
        }
        r.update(overrides)
        return r

    def test_pending_oos_written_for_new_order_with_stock_issue(self):
        """new_order + stock_issue + template_used → pending_oos_resolution saved."""
        state = {"status": "new", "facts": {}}
        result = self._base_result(
            state,
            draft_reply="We have a stock issue...",
            stock_issue=_make_stock_issue(),
        )
        classification = _FakeClassification(situation="new_order")
        gmail_thread_id = "thread-abc"

        from agents import pipeline

        with patch.object(pipeline, "save_email"), \
             patch.object(pipeline, "save_state") as mock_save, \
             patch.object(pipeline, "save_order_items", MagicMock()):
            pipeline._persist_results(
                classification, result, gmail_thread_id,
                "msg-123", "From: test\nSubject: Order\nBody: text",
            )

        # Verify pending_oos_resolution was built
        pending = state["facts"].get("pending_oos_resolution")
        self.assertIsNotNone(pending, "pending_oos_resolution must be written")
        self.assertEqual(pending["items"][0]["base_flavor"], "Green")
        self.assertEqual(pending["items"][0]["requested_qty"], 5)
        self.assertEqual(pending["items"][0]["available_qty"], 0)
        self.assertEqual(len(pending["alternatives"]["Green"]["alternatives"]), 1)
        self.assertEqual(
            pending["alternatives"]["Green"]["alternatives"][0]["product_name"],
            "Turquoise",
        )
        self.assertEqual(len(pending["in_stock_items"]), 1)
        self.assertEqual(pending["in_stock_items"][0]["base_flavor"], "Silver")
        self.assertIn("order_id", pending)
        self.assertIn("created_at", pending)

        # Verify save_state was called
        mock_save.assert_called_once()

    def test_no_pending_oos_when_no_stock_issue(self):
        """new_order without stock_issue → no pending_oos_resolution."""
        state = {"status": "new", "facts": {}}
        result = self._base_result(state, stock_issue=None)
        classification = _FakeClassification(situation="new_order")

        from agents import pipeline

        with patch.object(pipeline, "save_email"), \
             patch.object(pipeline, "save_state"), \
             patch.object(pipeline, "save_order_items", MagicMock()):
            pipeline._persist_results(
                classification, result, "thread-abc",
                "msg-123", "From: test\nSubject: Order\nBody: text",
            )

        self.assertNotIn(
            "pending_oos_resolution",
            state.get("facts", {}),
        )

    def test_no_pending_oos_when_not_template(self):
        """new_order + stock_issue but LLM reply (not template) → no pending_oos."""
        state = {"status": "new", "facts": {}}
        result = self._base_result(
            state,
            template_used=False,
            stock_issue=_make_stock_issue(),
        )
        classification = _FakeClassification(situation="new_order")

        from agents import pipeline

        with patch.object(pipeline, "save_email"), \
             patch.object(pipeline, "save_state"), \
             patch.object(pipeline, "save_order_items", MagicMock()):
            pipeline._persist_results(
                classification, result, "thread-abc",
                "msg-123", "From: test\nSubject: Order\nBody: text",
            )

        self.assertNotIn(
            "pending_oos_resolution",
            state.get("facts", {}),
        )


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: _update_inbound_state protects pending_oos_resolution
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateInboundStateProtection(unittest.TestCase):
    """Verify pipeline restores pending_oos_resolution if LLM strips it."""

    def test_pending_oos_restored_when_llm_strips_it(self):
        """If LLM state updater removes pending_oos_resolution during oos_followup,
        pipeline must restore it from previous state."""
        from agents import pipeline

        pending = _make_pending_oos()
        pre_state = {
            "state": {
                "status": "awaiting_oos_decision",
                "facts": {"pending_oos_resolution": pending},
            }
        }

        # LLM returns state WITHOUT pending_oos_resolution
        llm_state = {"status": "new", "facts": {"order_id": "ORD-999"}}

        classification = _FakeClassification(situation="oos_followup")

        with patch.object(pipeline, "update_conversation_state", return_value=llm_state), \
             patch.object(pipeline, "save_state") as mock_save:
            result = pipeline._update_inbound_state(
                gmail_thread_id="thread-abc",
                email_text="Yes I'll take the Turquoise",
                classification=classification,
                pre_state_record=pre_state,
            )

        # pending_oos_resolution must be restored
        self.assertIsNotNone(result)
        restored = result.get("facts", {}).get("pending_oos_resolution")
        self.assertIsNotNone(restored, "pending_oos_resolution must be restored")
        self.assertEqual(restored, pending)

    def test_no_restore_for_non_oos_situation(self):
        """For non-oos_followup situations, don't restore pending_oos."""
        from agents import pipeline

        pending = _make_pending_oos()
        pre_state = {
            "state": {
                "status": "awaiting_oos_decision",
                "facts": {"pending_oos_resolution": pending},
            }
        }

        # LLM removes it, but situation is tracking (not oos_followup)
        llm_state = {"status": "shipped", "facts": {"order_id": "ORD-999"}}

        classification = _FakeClassification(situation="tracking")

        with patch.object(pipeline, "update_conversation_state", return_value=llm_state), \
             patch.object(pipeline, "save_state"):
            result = pipeline._update_inbound_state(
                gmail_thread_id="thread-abc",
                email_text="Where is my order?",
                classification=classification,
                pre_state_record=pre_state,
            )

        # Should NOT restore for tracking
        self.assertIsNone(
            result.get("facts", {}).get("pending_oos_resolution"),
            "pending_oos should NOT be restored for non-oos_followup",
        )

    def test_no_thread_id_skips_state_update(self):
        """Without gmail_thread_id, _update_inbound_state returns None."""
        from agents import pipeline

        classification = _FakeClassification(situation="new_order")

        result = pipeline._update_inbound_state(
            gmail_thread_id=None,
            email_text="New order",
            classification=classification,
            pre_state_record=None,
        )

        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: oos_agreement reads pending_oos_resolution
# ═══════════════════════════════════════════════════════════════════════════

class TestOOSAgreementReadsState(unittest.TestCase):
    """Verify oos_agreement correctly reads and processes pending_oos_resolution."""

    def test_resolve_returns_no_data_without_pending(self):
        """No pending_oos_resolution → (None, 'no_data')."""
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        result = {"conversation_state": {"facts": {}}}
        confirmed, status = _resolve_oos_agreement(result, "Yes please")
        self.assertIsNone(confirmed)
        self.assertEqual(status, "no_data")

    def test_resolve_returns_no_data_with_empty_state(self):
        """Empty conversation_state → (None, 'no_data')."""
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        result = {"conversation_state": None}
        confirmed, status = _resolve_oos_agreement(result, "Yes please")
        self.assertIsNone(confirmed)
        self.assertEqual(status, "no_data")

    def test_resolve_single_alternative_auto_picks(self):
        """Single alternative → auto-picked, returns confirmed items."""
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        pending = _make_pending_oos()
        result = {"conversation_state": {"facts": {"pending_oos_resolution": pending}}}

        confirmed, status = _resolve_oos_agreement(result, "Yes, sounds good")
        self.assertEqual(status, "ok")
        self.assertIsNotNone(confirmed)
        # Should include in_stock Silver + alternative Turquoise
        flavors = [c["base_flavor"] for c in confirmed]
        self.assertIn("Silver", flavors)
        self.assertIn("Turquoise", flavors)

    def test_resolve_multiple_alternatives_clarify(self):
        """Multiple alternatives + ambiguous text → (None, 'clarify')."""
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        pending = _make_pending_oos()
        # Add second alternative
        pending["alternatives"]["Green"]["alternatives"].append(
            {"product_name": "Amber", "category": "TEREA_EUROPE"}
        )
        result = {"conversation_state": {"facts": {"pending_oos_resolution": pending}}}

        confirmed, status = _resolve_oos_agreement(result, "Yes, I'll take it")
        self.assertIsNone(confirmed)
        self.assertEqual(status, "clarify")

    def test_resolve_multiple_alternatives_explicit_match(self):
        """Multiple alternatives + explicit mention → picks matched one."""
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        pending = _make_pending_oos()
        pending["alternatives"]["Green"]["alternatives"].append(
            {"product_name": "Amber", "category": "TEREA_EUROPE"}
        )
        result = {"conversation_state": {"facts": {"pending_oos_resolution": pending}}}

        confirmed, status = _resolve_oos_agreement(
            result, "I'll take the Amber please"
        )
        self.assertEqual(status, "ok")
        flavors = [c["base_flavor"] for c in confirmed]
        self.assertIn("Amber", flavors)
        self.assertNotIn("Turquoise", flavors)

    def test_clear_pending_oos(self):
        """_clear_pending_oos removes pending_oos_resolution from state."""
        from agents.handlers.oos_agreement import _clear_pending_oos

        pending = _make_pending_oos()
        state = {"facts": {"pending_oos_resolution": pending, "order_id": "ORD-999"}}
        result = {"conversation_state": state}

        _clear_pending_oos(result)

        self.assertNotIn("pending_oos_resolution", state["facts"])
        # Other facts preserved
        self.assertEqual(state["facts"]["order_id"], "ORD-999")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: payment_received reads pending_oos for prepay guard
# ═══════════════════════════════════════════════════════════════════════════

class TestPaymentReceivedPendingOOSGuard(unittest.TestCase):
    """Verify payment_received checks pending_oos_resolution for prepay clients."""

    def setUp(self):
        self._patches = [
            patch("db.email_history.get_full_thread_history", return_value=[]),
            patch("db.conversation_state.get_client_states", return_value=[]),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _make_result(self, payment_type="prepay", has_pending_oos=False):
        client_data = {
            "name": "Test User",
            "payment_type": payment_type,
            "zelle_address": "pay@example.com",
            "discount_percent": 0,
            "discount_orders_left": 0,
            "street": "",
            "city_state_zip": "",
        }
        state = {"facts": {}}
        if has_pending_oos:
            state["facts"]["pending_oos_resolution"] = _make_pending_oos()

        return {
            "needs_reply": True,
            "situation": "payment_received",
            "client_email": "test@example.com",
            "client_name": "Test User",
            "client_found": True,
            "client_data": client_data,
            "template_used": False,
            "draft_reply": None,
            "needs_routing": True,
            "stock_issue": None,
            "conversation_state": state,
        }

    def test_prepay_with_pending_oos_sends_oos_agrees(self):
        """Prepay client + pending OOS → oos_agrees template (not payment_received)."""
        from agents.handlers.payment_received import handle_payment_received
        from agents.handlers.template_utils import fill_template_reply

        cls = _FakeClassification(situation="payment_received")
        result = self._make_result(payment_type="prepay", has_pending_oos=True)

        with patch(
            "agents.handlers.payment_received.fill_template_reply",
            wraps=lambda **kwargs: (
                dict(kwargs["result"], draft_reply="OOS agrees reply", template_used=True),
                True,
            ),
        ) as mock_fill:
            out = handle_payment_received(cls, result, "I sent the payment")

        # Verify oos_agrees situation was used
        call_kwargs = mock_fill.call_args
        self.assertEqual(call_kwargs.kwargs["situation"], "oos_agrees")
        self.assertTrue(out.get("template_used"))

    def test_prepay_without_pending_oos_sends_payment_received(self):
        """Prepay client without pending OOS → normal payment_received template."""
        from agents.handlers.payment_received import handle_payment_received

        cls = _FakeClassification(situation="payment_received")
        result = self._make_result(payment_type="prepay", has_pending_oos=False)

        with patch(
            "agents.handlers.payment_received.fill_template_reply",
            wraps=lambda **kwargs: (
                dict(kwargs["result"], draft_reply="Payment received!", template_used=True),
                True,
            ),
        ) as mock_fill:
            out = handle_payment_received(cls, result, "I sent the payment")

        call_kwargs = mock_fill.call_args
        self.assertEqual(call_kwargs.kwargs["situation"], "payment_received")

    def test_postpay_ignores_pending_oos(self):
        """Postpay client with pending OOS → normal payment_received (ignores OOS)."""
        from agents.handlers.payment_received import handle_payment_received

        cls = _FakeClassification(situation="payment_received")
        result = self._make_result(payment_type="postpay", has_pending_oos=True)

        with patch(
            "agents.handlers.payment_received.fill_template_reply",
            wraps=lambda **kwargs: (
                dict(kwargs["result"], draft_reply="Payment received!", template_used=True),
                True,
            ),
        ) as mock_fill:
            out = handle_payment_received(cls, result, "I sent the payment")

        call_kwargs = mock_fill.call_args
        self.assertEqual(call_kwargs.kwargs["situation"], "payment_received")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: oos_qty_utils reads pending_oos_resolution
# ═══════════════════════════════════════════════════════════════════════════

class TestOOSQtyUtilsReadsState(unittest.TestCase):
    """Verify merge and enrich functions work with pending_oos_resolution."""

    def _make_result_with_pending(self):
        pending = _make_pending_oos()
        return {
            "conversation_state": {
                "facts": {"pending_oos_resolution": pending}
            }
        }

    def test_merge_adds_in_stock_items(self):
        """_merge_in_stock_items adds Silver from pending's in_stock_items."""
        from agents.handlers.oos_qty_utils import _merge_in_stock_items

        extracted = [{"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 5}]
        result = self._make_result_with_pending()

        merged = _merge_in_stock_items(extracted, result)

        flavors = [m["base_flavor"] for m in merged]
        self.assertIn("Turquoise", flavors)
        self.assertIn("Silver", flavors)
        self.assertEqual(len(merged), 2)

    def test_merge_no_duplicates(self):
        """If Silver already in extracted, don't duplicate."""
        from agents.handlers.oos_qty_utils import _merge_in_stock_items

        extracted = [
            {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
            {"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 5},
        ]
        result = self._make_result_with_pending()

        merged = _merge_in_stock_items(extracted, result)
        self.assertEqual(len(merged), 2)  # No duplicate Silver

    def test_merge_without_pending_returns_unchanged(self):
        """No pending_oos_resolution → extracted returned as-is."""
        from agents.handlers.oos_qty_utils import _merge_in_stock_items

        extracted = [{"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 5}]
        result = {"conversation_state": {"facts": {}}}

        merged = _merge_in_stock_items(extracted, result)
        self.assertEqual(merged, extracted)

    def test_enrich_uses_pending_qty(self):
        """_enrich_qty_from_pending replaces default qty=1 with pending qty."""
        from agents.handlers.oos_qty_utils import _enrich_qty_from_pending

        # LLM extracted qty=1 (default), but pending knows requested_qty=5
        extracted = [{"base_flavor": "Green", "product_name": "Green", "quantity": 1}]
        result = self._make_result_with_pending()

        enriched = _enrich_qty_from_pending(
            extracted, result, inbound_text="Yes sounds good",
        )

        self.assertEqual(enriched[0]["quantity"], 5)

    def test_enrich_preserves_explicit_qty(self):
        """If LLM extracted qty > 1, don't override."""
        from agents.handlers.oos_qty_utils import _enrich_qty_from_pending

        extracted = [{"base_flavor": "Green", "product_name": "Green", "quantity": 3}]
        result = self._make_result_with_pending()

        enriched = _enrich_qty_from_pending(
            extracted, result, inbound_text="I'll take 3 boxes of Green",
        )

        self.assertEqual(enriched[0]["quantity"], 3)

    def test_enrich_without_pending_returns_unchanged(self):
        """No pending → extracted returned as-is."""
        from agents.handlers.oos_qty_utils import _enrich_qty_from_pending

        extracted = [{"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 1}]
        result = {"conversation_state": {"facts": {}}}

        enriched = _enrich_qty_from_pending(extracted, result, inbound_text="ok")
        self.assertEqual(enriched[0]["quantity"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6: Fallback paths when conversation_state is completely None
# ═══════════════════════════════════════════════════════════════════════════

class TestGracefulDegradationWithoutState(unittest.TestCase):
    """Verify all state consumers handle None conversation_state gracefully."""

    def test_oos_agreement_none_state(self):
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        result = {"conversation_state": None}
        confirmed, status = _resolve_oos_agreement(result, "ok")
        self.assertIsNone(confirmed)
        self.assertEqual(status, "no_data")

    def test_oos_agreement_missing_key(self):
        from agents.handlers.oos_agreement import _resolve_oos_agreement

        result = {}  # no conversation_state key at all
        confirmed, status = _resolve_oos_agreement(result, "ok")
        self.assertIsNone(confirmed)
        self.assertEqual(status, "no_data")

    def test_merge_none_state(self):
        from agents.handlers.oos_qty_utils import _merge_in_stock_items

        extracted = [{"base_flavor": "X", "product_name": "X", "quantity": 1}]
        result = {"conversation_state": None}
        merged = _merge_in_stock_items(extracted, result)
        self.assertEqual(merged, extracted)

    def test_enrich_none_state(self):
        from agents.handlers.oos_qty_utils import _enrich_qty_from_pending

        extracted = [{"base_flavor": "X", "product_name": "X", "quantity": 1}]
        result = {"conversation_state": None}
        enriched = _enrich_qty_from_pending(extracted, result, "ok")
        self.assertEqual(enriched[0]["quantity"], 1)

    def test_clear_pending_oos_none_state(self):
        from agents.handlers.oos_agreement import _clear_pending_oos

        result = {"conversation_state": None}
        # Should not raise
        _clear_pending_oos(result)

    def test_clear_pending_oos_empty_facts(self):
        from agents.handlers.oos_agreement import _clear_pending_oos

        result = {"conversation_state": {"facts": {}}}
        _clear_pending_oos(result)


if __name__ == "__main__":
    unittest.main()
