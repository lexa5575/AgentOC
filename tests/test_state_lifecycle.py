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

import pytest

pytestmark = pytest.mark.smoke

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
    # Attach sub-modules on parent package so patch("db.conversation_state...") resolves
    db_pkg = sys.modules.get("db")
    if db_pkg is not None:
        for sub in db_stub_modules:
            if sub != "db" and sub in sys.modules:
                attr = sub.split(".")[-1]
                setattr(db_pkg, attr, sys.modules[sub])

    db_catalog = sys.modules["db.catalog"]
    db_catalog.get_display_name = lambda name, cat="": name
    db_catalog.get_base_display_name = lambda name: name
    db_catalog.get_equivalent_norms = lambda name, *a, **kw: {name}
    db_catalog._enrich_display_name_with_region = lambda *args: args[-1] if args else ""
    db_catalog.get_catalog_products = lambda: []

    # Always create a fresh stub — never mutate the real module object
    db_region = types.ModuleType("db.region_family")
    db_region.CATEGORY_REGION_SUFFIX = dict()
    db_region.REGION_FAMILIES = {}
    db_region.PREFERRED_CATEGORY = {}
    db_region.is_same_family = lambda a, b=None: False
    db_region.get_family = lambda cat: None
    db_region.get_region_suffix = lambda cat: None
    db_region.get_family_suffix = lambda fam: None
    db_region.get_preferred_product_id = lambda *a, **kw: None
    db_region.expand_to_family_ids = lambda ids, catalog: list(ids) if ids else []
    db_region.extract_region_from_text = lambda text: None
    sys.modules["db.region_family"] = db_region

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

    import db.shipping as _db_shipping
    _db_shipping.save_order_shipping_address = MagicMock()

    # tools stubs
    for mod_name in ["tools", "tools.email_parser"]:
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            if mod_name == "tools":
                m.__path__ = []
            sys.modules[mod_name] = m

    tools_ep = sys.modules["tools.email_parser"]
    tools_ep._strip_quoted_text = lambda body: body
    tools_ep.strip_quoted_text = lambda body: body
    tools_ep.clean_email_body = lambda text: text
    tools_ep.try_parse_order = lambda text: None
    tools_ep._extract_base_flavor = lambda text: None
    tools_ep.REGION_SUFFIXES = ("EU", "ME", "made in Japan")

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


_MODULES_BEFORE_STUBS: dict | None = None
_MISSING = object()
_MUTATED_ATTRS: dict[tuple[str, str], object] = {}

_ATTR_LIST = [
    ("db.catalog", "get_display_name"), ("db.catalog", "get_base_display_name"),
    ("db.catalog", "get_equivalent_norms"), ("db.catalog", "_enrich_display_name_with_region"),
    ("db.catalog", "get_catalog_products"),
    ("db.region_family", "CATEGORY_REGION_SUFFIX"), ("db.region_family", "is_same_family"),
    ("db.region_preference", "apply_region_preference"), ("db.region_preference", "apply_thread_hint"),
    ("db.stock", "extract_variant_id"), ("db.stock", "has_ambiguous_variants"),
    ("db.email_history", "get_full_thread_history"),
    ("db.memory", "save_email"), ("db.memory", "save_order_items"),
    ("db.memory", "get_client"), ("db.memory", "get_stock_summary"),
    ("db.memory", "calculate_order_price"), ("db.memory", "check_stock_for_order"),
    ("db.memory", "resolve_order_items"), ("db.memory", "replace_order_items"),
    ("db.models", "get_session"), ("db.models", "ConversationState"), ("db.models", "Base"),
    ("db.conversation_state", "get_state"), ("db.conversation_state", "save_state"),
    ("db.conversation_state", "get_client_states"),
]
_PKG_ATTR_LIST = [
    ("db", "models"), ("db", "memory"), ("db", "conversation_state"),
    ("db", "catalog"), ("db", "region_family"), ("db", "fulfillment"),
    ("db", "region_preference"), ("db", "stock"), ("db", "email_history"),
]


def setup_module():
    """Install stubs before any test in this module runs."""
    global _MODULES_BEFORE_STUBS, _MUTATED_ATTRS
    _MODULES_BEFORE_STUBS = dict(sys.modules)
    _MUTATED_ATTRS = {}
    for mod_name, attr_name in _ATTR_LIST + _PKG_ATTR_LIST:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            _MUTATED_ATTRS[(mod_name, attr_name)] = getattr(mod, attr_name, _MISSING)
    _install_stubs()


def teardown_module():
    """Restore sys.modules and mutated attrs so stubs don't leak."""
    if _MODULES_BEFORE_STUBS is None:
        return
    # 1. Restore sys.modules
    added = set(sys.modules) - set(_MODULES_BEFORE_STUBS)
    for name in added:
        sys.modules.pop(name, None)
    for name, mod in _MODULES_BEFORE_STUBS.items():
        sys.modules[name] = mod
    # 2. Restore mutated attrs on real module objects
    for (mod_name, attr_name), original in _MUTATED_ATTRS.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        if original is _MISSING:
            if hasattr(mod, attr_name):
                delattr(mod, attr_name)
        else:
            setattr(mod, attr_name, original)


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


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7: Deterministic state builder
# ═══════════════════════════════════════════════════════════════════════════

class TestDeterministicStateBuilder(unittest.TestCase):
    """Tests for _build_deterministic_state and its helpers."""

    def test_new_thread_valid_shape(self):
        """New thread produces state with all required fields."""
        from agents.state_updater import _build_deterministic_state

        state = _build_deterministic_state(
            None, "Body: Hello", "new_order", "inbound",
            "ORD-1", "$110", None, None,
        )
        self.assertIn("status", state)
        self.assertIn("topic", state)
        self.assertIn("facts", state)
        self.assertIn("last_exchange", state)
        self.assertIn("summary", state)
        self.assertIn("promises", state)
        self.assertIn("open_questions", state)
        self.assertEqual(state["topic"], "new_order")
        self.assertEqual(state["facts"]["order_id"], "ORD-1")
        self.assertEqual(state["facts"]["price"], "$110")

    def test_preserves_existing_facts(self):
        """Existing facts (tracking, payment_method) are preserved."""
        from agents.state_updater import _build_deterministic_state

        current = {
            "status": "shipped",
            "topic": "tracking",
            "facts": {
                "order_id": "ORD-1",
                "tracking_number": "9400111",
                "payment_method": "Zelle",
                "ordered_items": ["Silver x3"],
            },
            "last_exchange": {"we_said": "Shipped!", "they_said": None},
            "promises": ["delivery in 3-5 days"],
            "open_questions": [],
            "summary": "Shipped",
        }
        state = _build_deterministic_state(
            current, "Body: Where is my order?", "tracking", "inbound",
            None, None, None, None,
        )
        self.assertEqual(state["facts"]["tracking_number"], "9400111")
        self.assertEqual(state["facts"]["payment_method"], "Zelle")
        self.assertEqual(state["facts"]["order_id"], "ORD-1")

    def test_never_strips_pending_oos(self):
        """pending_oos_resolution is always preserved from current state."""
        from agents.state_updater import _build_deterministic_state

        pending = _make_pending_oos()
        current = {
            "status": "awaiting_oos_decision",
            "facts": {"pending_oos_resolution": pending},
        }
        state = _build_deterministic_state(
            current, "Body: Yes I want the Turquoise", "oos_followup", "inbound",
            None, None, None, None,
        )
        self.assertEqual(
            state["facts"]["pending_oos_resolution"], pending,
        )

    def test_preserves_promises_and_open_questions(self):
        """promises and open_questions from current are preserved, not zeroed."""
        from agents.state_updater import _build_deterministic_state

        current = {
            "status": "new",
            "promises": ["ship today", "free shipping"],
            "open_questions": ["Which flavor?"],
            "facts": {},
        }
        state = _build_deterministic_state(
            current, "Body: ok", "other", "inbound",
            None, None, None, None,
        )
        self.assertEqual(state["promises"], ["ship today", "free shipping"])
        self.assertEqual(state["open_questions"], ["Which flavor?"])

    def test_ordered_items_from_classifier(self):
        """ordered_items filled from classification.order_items."""
        from agents.state_updater import _build_deterministic_state

        cls = _FakeClassification(
            order_items=[
                type("OI", (), {"product_name": "Terea Green EU", "base_flavor": "Green", "quantity": 2})(),
                type("OI", (), {"product_name": "Terea Silver ME", "base_flavor": "Silver", "quantity": 3})(),
            ]
        )
        state = _build_deterministic_state(
            None, "Body: order", "new_order", "inbound",
            None, None, cls, None,
        )
        self.assertEqual(state["facts"]["ordered_items"], ["Terea Green EU x2", "Terea Silver ME x3"])

    def test_order_items_dicts_populated(self):
        """facts.order_items filled as list[dict] for stock_question/price_question."""
        from agents.state_updater import _build_deterministic_state

        cls = _FakeClassification(
            order_items=[
                type("OI", (), {"product_name": "Green", "base_flavor": "Green", "quantity": 2})(),
            ]
        )
        state = _build_deterministic_state(
            None, "Body: order", "new_order", "inbound",
            None, None, cls, None,
        )
        self.assertIsInstance(state["facts"]["order_items"], list)
        self.assertEqual(len(state["facts"]["order_items"]), 1)
        self.assertEqual(state["facts"]["order_items"][0]["base_flavor"], "Green")
        self.assertEqual(state["facts"]["order_items"][0]["quantity"], 2)

    def test_topic_mapping(self):
        """Each situation maps to correct topic."""
        from agents.state_updater import _derive_topic

        self.assertEqual(_derive_topic("new_order"), "new_order")
        self.assertEqual(_derive_topic("oos_followup"), "new_order")
        self.assertEqual(_derive_topic("tracking"), "tracking")
        self.assertEqual(_derive_topic("payment_received"), "payment")
        self.assertEqual(_derive_topic("payment_question"), "payment")
        self.assertEqual(_derive_topic("discount_request"), "discount")
        self.assertEqual(_derive_topic("shipping_timeline"), "shipping")
        self.assertEqual(_derive_topic("other"), "general")
        self.assertEqual(_derive_topic("stock_question"), "general")

    def test_status_conservative(self):
        """payment_received preserves current status, does NOT set 'shipped'."""
        from agents.state_updater import _derive_status

        self.assertEqual(_derive_status("payment_received", "awaiting_payment"), "awaiting_payment")
        self.assertEqual(_derive_status("payment_received", "new"), "new")
        self.assertEqual(_derive_status("payment_received", None), "new")
        self.assertEqual(_derive_status("new_order", None), "new")
        self.assertEqual(_derive_status("oos_followup", None), "awaiting_oos_decision")
        self.assertEqual(_derive_status("tracking", "shipped"), "shipped")

    def test_last_exchange_inbound(self):
        """Inbound updates they_said, preserves we_said."""
        from agents.state_updater import _derive_last_exchange

        current = {"we_said": "Hello!", "they_said": "Old msg"}
        result = _derive_last_exchange(current, "Body: New question", "inbound")
        self.assertEqual(result["we_said"], "Hello!")
        self.assertIn("New question", result["they_said"])

    def test_summary_contains_key_facts(self):
        """Summary includes order_id and items."""
        from agents.state_updater import _derive_summary

        facts = {
            "order_id": "ORD-123",
            "ordered_items": ["Green x2", "Silver x3"],
            "pending_oos_resolution": {"items": []},
        }
        summary = _derive_summary(facts, "new_order")
        self.assertIn("ORD-123", summary)
        self.assertIn("Green x2", summary)
        self.assertIn("pending OOS", summary)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8: Phase 2 enrichment
# ═══════════════════════════════════════════════════════════════════════════

class TestPhase2Enrichment(unittest.TestCase):
    """Tests for _enrich_state_after_routing."""

    def test_oos_items_from_stock_issue(self):
        from agents.state_updater import _enrich_state_after_routing

        state = {"facts": {}}
        result = {
            "stock_issue": {
                "stock_check": {
                    "insufficient_items": [
                        {"product_name": "Terea Green EU", "base_flavor": "Green"},
                    ]
                },
                "best_alternatives": {},
            }
        }
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertEqual(state["facts"]["oos_items"], ["Terea Green EU"])

    def test_offered_alternatives_with_regions(self):
        from agents.state_updater import _enrich_state_after_routing

        # Stub CATEGORY_REGION_SUFFIX for this test
        db_region = sys.modules["db.region_family"]
        old = db_region.CATEGORY_REGION_SUFFIX
        db_region.CATEGORY_REGION_SUFFIX = {"ARMENIA": "ME", "TEREA_EUROPE": "EU"}

        state = {"facts": {}}
        result = {
            "stock_issue": {
                "stock_check": {"insufficient_items": []},
                "best_alternatives": {
                    "Green": {
                        "alternatives": [
                            {"alternative": {"product_name": "Turquoise", "category": "ARMENIA"}},
                        ]
                    }
                },
            }
        }
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertIn("Turquoise ME", state["facts"]["offered_alternatives"])

        db_region.CATEGORY_REGION_SUFFIX = old

    def test_final_price_formatted(self):
        from agents.state_updater import _enrich_state_after_routing

        state = {"facts": {}}
        result = {"calculated_price": 209.0}
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertEqual(state["facts"]["final_price"], "$209.00")

    def test_oos_resolve_clears_stale_fields(self):
        """After OOS resolve, oos_items/offered_alternatives/pending_order_items cleared."""
        from agents.state_updater import _enrich_state_after_routing

        state = {
            "facts": {
                "oos_items": ["Green"],
                "offered_alternatives": ["Turquoise ME"],
                "pending_order_items": [{"base_flavor": "Green"}],
            }
        }
        result = {
            "effective_situation": "new_order",
            "canonical_confirmed_items": [
                {"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 5}
            ],
        }
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertEqual(state["facts"]["oos_items"], [])
        self.assertEqual(state["facts"]["offered_alternatives"], [])
        self.assertEqual(state["facts"]["pending_order_items"], [])

    def test_oos_resolve_sets_confirmed_and_order_items(self):
        from agents.state_updater import _enrich_state_after_routing

        state = {"facts": {}}
        result = {
            "effective_situation": "new_order",
            "canonical_confirmed_items": [
                {"base_flavor": "Turquoise", "product_name": "Turquoise", "quantity": 5}
            ],
        }
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertEqual(len(state["facts"]["confirmed_order_items"]), 1)
        self.assertEqual(state["facts"]["confirmed_order_items"][0]["base_flavor"], "Turquoise")
        self.assertEqual(state["facts"]["order_items"], state["facts"]["confirmed_order_items"])

    def test_summary_recalculated(self):
        from agents.state_updater import _enrich_state_after_routing

        state = {"facts": {"order_id": "ORD-1"}, "summary": "old summary"}
        result = {"calculated_price": 220.0}
        _enrich_state_after_routing(
            state, result, _FakeClassification(situation="new_order"),
        )
        self.assertIn("ORD-1", state["summary"])
        self.assertNotEqual(state["summary"], "old summary")

    def test_no_stock_issue_preserves_current(self):
        from agents.state_updater import _enrich_state_after_routing

        state = {"facts": {"oos_items": ["Green"], "offered_alternatives": ["Silver"]}}
        result = {}  # no stock_issue, no effective_situation
        _enrich_state_after_routing(state, result, _FakeClassification())
        self.assertEqual(state["facts"]["oos_items"], ["Green"])
        self.assertEqual(state["facts"]["offered_alternatives"], ["Silver"])

    def test_none_state_no_crash(self):
        from agents.state_updater import _enrich_state_after_routing

        _enrich_state_after_routing(None, {}, _FakeClassification())


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9: Feature flag
# ═══════════════════════════════════════════════════════════════════════════

class TestFeatureFlag(unittest.TestCase):
    """Tests for USE_LLM_STATE_UPDATER feature flag."""

    def test_flag_read_at_runtime(self):
        from agents.state_updater import _use_llm

        with patch.dict(os.environ, {"USE_LLM_STATE_UPDATER": "false"}):
            self.assertEqual(_use_llm(), "false")
        with patch.dict(os.environ, {"USE_LLM_STATE_UPDATER": "shadow"}):
            self.assertEqual(_use_llm(), "shadow")
        with patch.dict(os.environ, {"USE_LLM_STATE_UPDATER": "true"}):
            self.assertEqual(_use_llm(), "true")

    def test_flag_false_no_llm(self):
        """When flag=false, LLM is never called."""
        from agents import state_updater

        with patch.dict(os.environ, {"USE_LLM_STATE_UPDATER": "false"}), \
             patch.object(state_updater, "_run_llm_state_updater") as mock_llm:
            result = state_updater.update_conversation_state(
                None, "Body: test", "new_order", "inbound",
            )
            mock_llm.assert_not_called()
            self.assertIn("facts", result)

    def test_flag_shadow_returns_deterministic(self):
        """Shadow mode returns deterministic result, calls LLM for comparison."""
        from agents import state_updater

        with patch.dict(os.environ, {"USE_LLM_STATE_UPDATER": "shadow"}), \
             patch.object(
                 state_updater, "_run_llm_state_updater",
                 return_value={"status": "new", "facts": {}},
             ) as mock_llm:
            result = state_updater.update_conversation_state(
                None, "Body: test", "new_order", "inbound",
            )
            mock_llm.assert_called_once()
            # Result should be deterministic (has topic from derive)
            self.assertEqual(result["topic"], "new_order")


import os  # noqa: E402 — needed for patch.dict


if __name__ == "__main__":
    unittest.main()
