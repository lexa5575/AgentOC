"""Unit tests for OOS Followup handler intent-based branching.

Tests that:
- agrees_to_alternative + pending_oos → new_order template with price (0 tokens)
- agrees_to_alternative + no pending_oos → oos_agrees fallback (legacy)
- agrees_to_alternative + multiple alternatives + ambiguous → clarification reply
- agrees_to_alternative + multiple alternatives + explicit → new_order template
- agrees_to_alternative + prepay without zelle → LLM fallback
- declines_alternative → template (0 tokens)
- declines template has no website references
- asks_question / unknown → LLM
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _install_stubs() -> None:
    """Install stubs for modules not available in test environment."""
    for name in list(sys.modules):
        if (
            name.startswith("agents.handlers")
            or name == "agents.context"
            or name == "agents.reply_templates"
        ):
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

    # tools — only stub web_search
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
        tools_ep = types.ModuleType("tools.email_parser")
        tools_ep._strip_quoted_text = lambda body: body  # passthrough in tests
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


class _FakeClassification:
    """Minimal classification stub for handler tests."""

    def __init__(self, **kwargs):
        self.client_email = kwargs.get("client_email", "test@example.com")
        self.client_name = kwargs.get("client_name", "Test User")
        self.situation = kwargs.get("situation", "oos_followup")
        self.dialog_intent = kwargs.get("dialog_intent", None)
        self.followup_to = kwargs.get("followup_to", None)
        self.price = kwargs.get("price", None)
        self.order_id = kwargs.get("order_id", None)
        self.customer_street = kwargs.get("customer_street", None)
        self.customer_city_state_zip = kwargs.get("customer_city_state_zip", None)
        self.items = kwargs.get("items", None)
        self.order_items = kwargs.get("order_items", None)
        self.is_followup = kwargs.get("is_followup", True)
        self.parser_used = kwargs.get("parser_used", False)


class TestOOSFollowupIntents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.handler_mod = importlib.import_module("agents.handlers.oos_followup")

    def _make_result(self, *, client_found=True, payment_type="prepay",
                     zelle_address="pay@example.com", conversation_state=None):
        client_data = {
            "name": "Test User",
            "payment_type": payment_type,
            "zelle_address": zelle_address,
            "discount_percent": 0,
            "discount_orders_left": 0,
            "street": "",
            "city_state_zip": "",
        }
        return {
            "needs_reply": True,
            "situation": "oos_followup",
            "client_email": "test@example.com",
            "client_name": "Test User",
            "client_found": client_found,
            "client_data": client_data if client_found else None,
            "template_used": False,
            "draft_reply": None,
            "needs_routing": True,
            "stock_issue": None,
            "conversation_state": conversation_state,
        }

    def _make_pending_oos(self, *, num_alternatives=1, available_qty=0,
                          include_in_stock=True, alt_names=None):
        """Create a conversation_state with pending_oos_resolution."""
        if alt_names is None:
            alt_names = [f"Alt Product {i+1}" for i in range(num_alternatives)]
        alternatives = [
            {"product_name": name, "category": f"CAT_{i+1}"}
            for i, name in enumerate(alt_names)
        ]
        pending = {
            "order_id": "ORD-123",
            "items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "requested_qty": 5,
                    "available_qty": available_qty,
                }
            ],
            "alternatives": {
                "Green": {"alternatives": alternatives},
            },
            "in_stock_items": [],
        }
        if include_in_stock:
            pending["in_stock_items"] = [
                {
                    "base_flavor": "Silver",
                    "product_name": "Silver",
                    "ordered_qty": 3,
                }
            ]
        return {"facts": {"pending_oos_resolution": pending}, "status": "oos_followup"}

    def _mock_stock_all_ok(self):
        """Stock check result: all resolved items in stock."""
        return {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "Silver",
                    "product_name": "Silver",
                    "ordered_qty": 3,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Silver", "quantity": 50}],
                    "total_available": 50,
                    "is_sufficient": True,
                },
                {
                    "base_flavor": "Alt Product 1",
                    "product_name": "Alt Product 1",
                    "ordered_qty": 5,
                    "stock_entries": [{"category": "CAT_1", "product_name": "Alt Product 1", "quantity": 20}],
                    "total_available": 20,
                    "is_sufficient": True,
                },
            ],
            "insufficient_items": [],
        }

    # ---------------------------------------------------------------
    # agrees_to_alternative — templates
    # ---------------------------------------------------------------

    def test_agrees_prepay_uses_new_order_template(self):
        """agrees_to_alternative + prepay + pending_oos → new_order template with price."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("$550.00", out["draft_reply"])
        self.assertIn("pay@example.com", out["draft_reply"])
        self.assertIn("Thank you so much for placing an order", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_agrees_postpay_uses_new_order_template(self):
        """agrees_to_alternative + postpay + pending_oos → new_order template with price."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("$550.00", out["draft_reply"])
        self.assertIn("Thank you very much for placing an order", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_agrees_prepay_no_zelle_falls_to_llm(self):
        """agrees_to_alternative + prepay but no zelle_address → LLM fallback."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        result = self._make_result(payment_type="prepay", zelle_address="")

        with patch.object(
            self.handler_mod.oos_followup_agent,
            "run",
            return_value=types.SimpleNamespace(content="LLM generated reply"),
        ) as run_mock:
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM generated reply")

    # ---------------------------------------------------------------
    # declines_alternative — template
    # ---------------------------------------------------------------

    def test_declines_uses_template(self):
        """declines_alternative → decline template, no LLM."""
        cls = _FakeClassification(dialog_intent="declines_alternative")
        result = self._make_result()

        out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("No problem at all!", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_declines_no_website_reference(self):
        """Decline template must NOT contain website references (hard_rules.yaml)."""
        cls = _FakeClassification(dialog_intent="declines_alternative")
        result = self._make_result()

        out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        reply = out["draft_reply"].lower()
        self.assertNotIn("shipmecarton.com", reply)
        self.assertNotIn("website", reply)
        self.assertNotIn("visit", reply)

    # ---------------------------------------------------------------
    # LLM branches
    # ---------------------------------------------------------------

    def test_asks_question_uses_llm(self):
        """asks_question → LLM handler, not template."""
        cls = _FakeClassification(dialog_intent="asks_question")
        result = self._make_result()

        with patch.object(
            self.handler_mod.oos_followup_agent,
            "run",
            return_value=types.SimpleNamespace(content="LLM answer"),
        ) as run_mock:
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM answer")
        # Verify payment_type hint is in the prompt
        prompt = run_mock.call_args.args[0]
        self.assertIn("PREPAY", prompt)
        self.assertIn("IGNORE all postpay", prompt)

    def test_unknown_intent_uses_llm(self):
        """No dialog_intent → LLM handler."""
        cls = _FakeClassification(dialog_intent=None)
        result = self._make_result()

        with patch.object(
            self.handler_mod.oos_followup_agent,
            "run",
            return_value=types.SimpleNamespace(content="LLM fallback"),
        ) as run_mock:
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM fallback")

    # ---------------------------------------------------------------
    # agrees_to_alternative — new_order resolution tests
    # ---------------------------------------------------------------

    def test_agrees_no_pending_oos_uses_oos_agrees(self):
        """No pending_oos_resolution → falls back to oos_agrees template (legacy)."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        result = self._make_result(payment_type="prepay", zelle_address="pay@example.com")

        out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("We will update your order with the alternative.", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_agrees_partial_oos_reduces_qty(self):
        """Partial OOS (available_qty > 0) → qty reduced to available, new_order template."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        # available_qty=3 means partial OOS (requested was 5)
        state = self._make_pending_oos(num_alternatives=0, available_qty=3)
        # With available_qty > 0, no alternatives needed — partial item kept at reduced qty
        state["facts"]["pending_oos_resolution"]["alternatives"] = {}
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()) as mock_stock:
            with patch.object(self.handler_mod, "calculate_order_price", return_value=330.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        # check_stock_for_order should be called with qty=3 (not 5)
        called_items = mock_stock.call_args.args[0]
        green_item = [i for i in called_items if i["base_flavor"] == "Green"]
        self.assertEqual(green_item[0]["quantity"], 3)
        self.assertTrue(out["template_used"])
        self.assertIn("$330.00", out["draft_reply"])

    def test_agrees_no_alternatives_falls_to_oos_agrees(self):
        """Full OOS + empty alternatives → oos_agrees fallback."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=0)
        # Remove alternatives completely
        state["facts"]["pending_oos_resolution"]["alternatives"] = {"Green": {"alternatives": []}}
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("We will update your order with the alternative.", out["draft_reply"])

    def test_agrees_stock_changed_falls_to_oos_agrees(self):
        """Alternative sold out since OOS email → oos_agrees fallback."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )
        stock_not_ok = {
            "all_in_stock": False,
            "items": [],
            "insufficient_items": [{"base_flavor": "Alt Product 1"}],
        }

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=stock_not_ok):
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        self.assertIn("We will update your order with the alternative.", out["draft_reply"])

    def test_pending_oos_cleared_after_success(self):
        """After new_order template sent, pending_oos_resolution is cleared from state."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        # pending_oos_resolution should be cleared
        facts = out.get("conversation_state", {}).get("facts", {})
        self.assertNotIn("pending_oos_resolution", facts)

    def test_agrees_multiple_alternatives_sends_clarification(self):
        """agree + multiple alternatives + generic text → clarification reply."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(
            num_alternatives=2,
            alt_names=["Tera Green Armenia", "Terea Green KZ"],
        )
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        # Generic "agree" text — doesn't name any specific alternative
        out = self.handler_mod.handle_oos_followup(cls, result, "Yes that works for me")

        self.assertTrue(out["template_used"])
        self.assertIn("confirm which option", out["draft_reply"])
        self.assertIn("Tera Green Armenia", out["draft_reply"])
        self.assertIn("Terea Green KZ", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_agrees_explicit_alternative_uses_new_order(self):
        """agree + email names specific alternative → new_order template with price."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(
            num_alternatives=2,
            alt_names=["Tera Green Armenia", "Terea Green KZ"],
        )
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()) as mock_stock:
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(
                    cls, result,
                    "Yes, I'll take the Tera Green Armenia please",
                )

        self.assertTrue(out["template_used"])
        self.assertIn("$550.00", out["draft_reply"])
        # Verify the correct alternative was chosen (used as base_flavor)
        called_items = mock_stock.call_args.args[0]
        alt_item = [i for i in called_items if i["base_flavor"] == "Tera Green Armenia"]
        self.assertEqual(len(alt_item), 1)
        self.assertEqual(alt_item[0]["quantity"], 5)


if __name__ == "__main__":
    unittest.main()
