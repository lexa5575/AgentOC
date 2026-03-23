"""Unit tests for Price Question handler.

Tests that:
- Items + all in stock + price resolves → deterministic quote (0 tokens)
- Items + partial OOS → partial quote with OOS notice (0 tokens)
- Items + ambiguous categories → price_alert + LLM fallback
- No items → LLM fallback
- Items from conversation_state.facts.confirmed_order_items
- All items OOS → LLM fallback
- Empty stock table → LLM fallback
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.domain_fulfillment

import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _install_stubs():
    """Install minimal module stubs for import isolation."""
    # Clean any previously imported handler modules
    for name in list(sys.modules):
        if name.startswith("agents.handlers.price_question"):
            sys.modules.pop(name, None)

    # agno
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
                raise RuntimeError("FakeAgent.run must be patched")

        agno_agent.Agent = FakeAgent
        sys.modules["agno.agent"] = agno_agent
    if "agno.models" not in sys.modules:
        agno_models = types.ModuleType("agno.models")
        agno_models.__path__ = []
        sys.modules["agno.models"] = agno_models
    if "agno.models.openai" not in sys.modules:
        agno_models_openai = types.ModuleType("agno.models.openai")

        class FakeOpenAIResponses:
            def __init__(self, *args, **kwargs):
                pass

        agno_models_openai.OpenAIResponses = FakeOpenAIResponses
        sys.modules["agno.models.openai"] = agno_models_openai

    # db
    if "db" not in sys.modules:
        db_mod = types.ModuleType("db")
        db_mod.__path__ = []
        db_mod.get_postgres_db = lambda *a, **kw: object()
        sys.modules["db"] = db_mod
    if "db.memory" not in sys.modules:
        db_memory = types.ModuleType("db.memory")
        db_memory.get_client = lambda *a, **kw: None
        db_memory.decrement_discount = lambda *a, **kw: None
        db_memory.get_stock_summary = lambda *a, **kw: {"total": 0}
        db_memory.check_stock_for_order = lambda *a, **kw: {"all_in_stock": True, "items": [], "insufficient_items": []}
        db_memory.calculate_order_price = lambda *a, **kw: None
        db_memory.resolve_order_items = lambda items, **kw: (items, [])
        db_memory.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
        db_memory.get_full_email_history = lambda *a, **kw: []
        db_memory.get_full_thread_history = lambda *a, **kw: []
        db_memory.save_email = lambda *a, **kw: None
        db_memory.save_order_items = lambda *a, **kw: None
        db_memory.update_client = lambda *a, **kw: None
        sys.modules["db.memory"] = db_memory
    if "db.clients" not in sys.modules:
        db_clients = types.ModuleType("db.clients")
        db_clients.get_client_profile = lambda *a, **kw: None
        db_clients.update_client_summary = lambda *a, **kw: True
        sys.modules["db.clients"] = db_clients
    if "db.conversation_state" not in sys.modules:
        db_cs = types.ModuleType("db.conversation_state")
        db_cs.get_state = lambda *a, **kw: None
        db_cs.save_state = lambda *a, **kw: None
        db_cs.get_client_states = lambda *a, **kw: []
        sys.modules["db.conversation_state"] = db_cs

    # tools
    if "tools" not in sys.modules:
        try:
            import tools
        except ImportError:
            tools_mod = types.ModuleType("tools")
            tools_mod.__path__ = []
            sys.modules["tools"] = tools_mod
    if "tools.web_search" not in sys.modules:
        tools_ws = types.ModuleType("tools.web_search")
        tools_ws.get_search_tools = lambda: []
        sys.modules["tools.web_search"] = tools_ws
    if "tools.email_parser" not in sys.modules:
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body
            tools_ep.try_parse_order = lambda *a, **kw: None
            tools_ep.clean_email_body = lambda body: body
            sys.modules["tools.email_parser"] = tools_ep

    # utils
    if "utils" not in sys.modules:
        utils_mod = types.ModuleType("utils")
        utils_mod.__path__ = []
        sys.modules["utils"] = utils_mod
    if "utils.telegram" not in sys.modules:
        utils_telegram = types.ModuleType("utils.telegram")
        utils_telegram.send_telegram = lambda *a, **kw: None
        sys.modules["utils.telegram"] = utils_telegram


class _FakeOrderItem:
    def __init__(self, product_name, base_flavor, quantity):
        self.product_name = product_name
        self.base_flavor = base_flavor
        self.quantity = quantity


class _FakeClassification:
    def __init__(self, **kwargs):
        self.needs_reply = kwargs.get("needs_reply", True)
        self.situation = kwargs.get("situation", "price_question")
        self.client_email = kwargs.get("client_email", "test@example.com")
        self.client_name = kwargs.get("client_name", "Test User")
        self.dialog_intent = kwargs.get("dialog_intent", None)
        self.followup_to = kwargs.get("followup_to", None)
        self.price = kwargs.get("price", None)
        self.order_id = kwargs.get("order_id", None)
        self.customer_street = kwargs.get("customer_street", None)
        self.customer_city_state_zip = kwargs.get("customer_city_state_zip", None)
        self.items = kwargs.get("items", None)
        self.order_items = kwargs.get("order_items", None)
        self.parser_used = kwargs.get("parser_used", False)


class TestPriceQuestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._modules_snapshot = dict(sys.modules)
        _install_stubs()
        cls.handler_mod = importlib.import_module("agents.handlers.price_question")

    @classmethod
    def tearDownClass(cls):
        added = set(sys.modules) - set(cls._modules_snapshot)
        for name in added:
            sys.modules.pop(name, None)
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def setUp(self):
        # Patch Gmail-dependent functions to avoid googleapiclient import
        # in LLM-fallback paths (build_context → get_full_email_history → Gmail API)
        self._gmail_patches = [
            patch("agents.context.get_full_email_history", return_value=[]),
            patch("agents.context.get_full_thread_history", return_value=[]),
        ]
        for p in self._gmail_patches:
            p.start()

    def tearDown(self):
        for p in self._gmail_patches:
            p.stop()

    def _make_result(self, *, client_found=True, payment_type="prepay",
                     conversation_state=None):
        client_data = {
            "name": "Test User",
            "payment_type": payment_type,
            "zelle_address": "pay@example.com",
            "discount_percent": 0,
            "discount_orders_left": 0,
        }
        return {
            "client_email": "test@example.com",
            "client_name": "Test User",
            "client_found": client_found,
            "client_data": client_data if client_found else None,
            "template_used": False,
            "draft_reply": None,
            "needs_routing": True,
            "situation": "price_question",
            "stock_issue": None,
            "conversation_state": conversation_state,
        }

    def _stock_all_ok(self):
        """Stock check result: all items in stock, single category."""
        return {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 5,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 50}],
                    "total_available": 50,
                    "is_sufficient": True,
                },
                {
                    "base_flavor": "Silver",
                    "product_name": "Silver",
                    "ordered_qty": 3,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Silver", "quantity": 30}],
                    "total_available": 30,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [],
        }

    def _stock_partial_oos(self):
        """Stock check: Green in stock, Silver OOS."""
        return {
            "all_in_stock": False,
            "items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 5,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 50}],
                    "total_available": 50,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [
                {
                    "base_flavor": "Silver",
                    "ordered_qty": 3,
                    "total_available": 0,
                    "is_sufficient": False,
                },
            ],
        }

    def _stock_ambiguous(self):
        """Stock check: item matches multiple categories."""
        return {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "WeirdItem",
                    "product_name": "Weird",
                    "ordered_qty": 1,
                    "stock_entries": [
                        {"category": "KZ_TEREA", "product_name": "Weird", "quantity": 5},
                        {"category": "TEREA_JAPAN", "product_name": "Weird", "quantity": 3},
                    ],
                    "total_available": 8,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [],
        }

    # ---------------------------------------------------------------
    # Full quote — all items in stock, price resolves
    # ---------------------------------------------------------------

    def test_full_quote_all_in_stock(self):
        """Items + all in stock + price → deterministic quote."""
        cls = _FakeClassification(
            order_items=[
                _FakeOrderItem("Green", "Green", 5),
                _FakeOrderItem("Silver", "Silver", 3),
            ],
        )
        result = self._make_result()

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=880.0):
                    out = self.handler_mod.handle_price_question(cls, result, "how much for 5 Green and 3 Silver?")

        self.assertTrue(out["template_used"])
        self.assertIn("$880.00", out["draft_reply"])
        self.assertIn("free shipping", out["draft_reply"])
        self.assertIn("Would you like to go ahead", out["draft_reply"])
        self.assertFalse(out["needs_routing"])
        self.assertEqual(out["calculated_price"], 880.0)

    # ---------------------------------------------------------------
    # Partial OOS — some items out of stock
    # ---------------------------------------------------------------

    def test_partial_oos_quote(self):
        """Items + partial OOS → partial quote with OOS notice."""
        cls = _FakeClassification(
            order_items=[
                _FakeOrderItem("Green", "Green", 5),
                _FakeOrderItem("Silver", "Silver", 3),
            ],
        )
        result = self._make_result()

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._stock_partial_oos()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_price_question(cls, result, "price for Green and Silver?")

        self.assertTrue(out["template_used"])
        self.assertIn("$550.00", out["draft_reply"])
        self.assertIn("out of stock", out["draft_reply"])
        self.assertIn("Silver", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    # ---------------------------------------------------------------
    # Ambiguous categories → price_alert + LLM fallback
    # ---------------------------------------------------------------

    def test_ambiguous_categories_fallback(self):
        """Ambiguous categories → price_alert + LLM fallback."""
        cls = _FakeClassification(
            order_items=[_FakeOrderItem("Weird", "WeirdItem", 1)],
        )
        result = self._make_result()

        # Import general handler to patch its agent
        general_mod = importlib.import_module("agents.handlers.general")

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._stock_ambiguous()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=None):
                    with patch.object(
                        general_mod.general_agent, "run",
                        return_value=types.SimpleNamespace(content="LLM price reply"),
                    ):
                        out = self.handler_mod.handle_price_question(cls, result, "how much for Weird?")

        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM price reply")
        self.assertIsNotNone(out.get("price_alert"))
        self.assertEqual(out["price_alert"]["type"], "unmatched")
        self.assertIn("WeirdItem", out["price_alert"]["items"])

    # ---------------------------------------------------------------
    # No items → LLM fallback
    # ---------------------------------------------------------------

    def test_no_items_fallback(self):
        """No order_items → LLM fallback."""
        cls = _FakeClassification(order_items=None)
        result = self._make_result()

        general_mod = importlib.import_module("agents.handlers.general")

        with patch.object(
            general_mod.general_agent, "run",
            return_value=types.SimpleNamespace(content="LLM generic reply"),
        ):
            out = self.handler_mod.handle_price_question(cls, result, "how much does stuff cost?")

        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM generic reply")

    # ---------------------------------------------------------------
    # Items from conversation_state
    # ---------------------------------------------------------------

    def test_items_from_state(self):
        """No classification.order_items but confirmed_order_items in state → quote."""
        cls = _FakeClassification(order_items=None)
        state = {
            "facts": {
                "confirmed_order_items": [
                    {"base_flavor": "Green", "product_name": "Green", "quantity": 5},
                    {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
                ],
            },
        }
        result = self._make_result(conversation_state=state)

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=880.0):
                    out = self.handler_mod.handle_price_question(cls, result, "what's the total?")

        self.assertTrue(out["template_used"])
        self.assertIn("$880.00", out["draft_reply"])

    # ---------------------------------------------------------------
    # All items OOS → LLM fallback
    # ---------------------------------------------------------------

    def test_all_oos_fallback(self):
        """All items OOS → LLM fallback."""
        cls = _FakeClassification(
            order_items=[_FakeOrderItem("Green", "Green", 5)],
        )
        result = self._make_result()

        stock_all_oos = {
            "all_in_stock": False,
            "items": [],
            "insufficient_items": [{"base_flavor": "Green", "ordered_qty": 5}],
        }

        general_mod = importlib.import_module("agents.handlers.general")

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=stock_all_oos):
                with patch.object(
                    general_mod.general_agent, "run",
                    return_value=types.SimpleNamespace(content="LLM OOS reply"),
                ):
                    out = self.handler_mod.handle_price_question(cls, result, "how much for Green?")

        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM OOS reply")

    # ---------------------------------------------------------------
    # Empty stock table → LLM fallback
    # ---------------------------------------------------------------

    def test_empty_stock_table_fallback(self):
        """Stock table empty → LLM fallback."""
        cls = _FakeClassification(
            order_items=[_FakeOrderItem("Green", "Green", 5)],
        )
        result = self._make_result()

        general_mod = importlib.import_module("agents.handlers.general")

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 0}):
            with patch.object(
                general_mod.general_agent, "run",
                return_value=types.SimpleNamespace(content="LLM empty stock reply"),
            ):
                out = self.handler_mod.handle_price_question(cls, result, "how much for Green?")

        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM empty stock reply")

    # ---------------------------------------------------------------
    # No discount applied in quote
    # ---------------------------------------------------------------

    def test_no_discount_in_quote(self):
        """Quote shows raw price, no discount applied (discount only for new_order)."""
        cls = _FakeClassification(
            order_items=[_FakeOrderItem("Green", "Green", 5)],
        )
        result = self._make_result()
        # Client has discount — but it should NOT be applied
        result["client_data"]["discount_percent"] = 10
        result["client_data"]["discount_orders_left"] = 3

        stock_one_item = {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 5,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 50}],
                    "total_available": 50,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [],
        }

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=stock_one_item):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_price_question(cls, result, "how much for 5 Green?")

        self.assertTrue(out["template_used"])
        self.assertIn("$550.00", out["draft_reply"])
        # No discount text in reply
        self.assertNotIn("10%", out["draft_reply"])
        self.assertNotIn("$495", out["draft_reply"])


    # ---------------------------------------------------------------
    # Purple Wave regression: must NOT return $115 (Japan price)
    # ---------------------------------------------------------------

    def test_purple_wave_price_not_japan(self):
        """'Tera purple wave' must quote $110 (ME/EU), NOT $115 (Japan).

        Regression test for incident 2026-03-23 (khorolmaa_b@aol.com).
        The resolver must resolve 'Purple Wave' to non-Japan 'Purple' via alias,
        and calculate_order_price must use ME/EU category ($110).
        """
        cls = _FakeClassification(
            order_items=[_FakeOrderItem("Tera purple wave", "Purple", 1)],
        )
        result = self._make_result()

        stock_purple_me = {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "Purple",
                    "product_name": "Purple",
                    "ordered_qty": 1,
                    "stock_entries": [
                        {"category": "ARMENIA", "product_name": "Purple", "quantity": 10},
                    ],
                    "total_available": 10,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [],
        }

        with patch.object(self.handler_mod, "get_stock_summary", return_value={"total": 100}):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=stock_purple_me):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=110.0):
                    out = self.handler_mod.handle_price_question(
                        cls, result, "Tera purple wave. How much is the carton?",
                    )

        self.assertTrue(out["template_used"])
        self.assertIn("$110.00", out["draft_reply"])
        self.assertNotIn("$115", out["draft_reply"])
        self.assertEqual(out["calculated_price"], 110.0)


if __name__ == "__main__":
    unittest.main()
