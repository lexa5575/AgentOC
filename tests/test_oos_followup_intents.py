"""Unit tests for OOS Followup handler intent-based branching.

Tests that:
- agrees_to_alternative + pending_oos → new_order template with price (0 tokens)
- agrees_to_alternative + no pending_oos + classifier items → new_order template (0 tokens)
- agrees_to_alternative + no pending_oos + no items → LLM fallback
- agrees_to_alternative + no alternatives → LLM fallback
- agrees_to_alternative + stock changed → LLM fallback
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
        if name.startswith("agents.handlers"):
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
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body  # passthrough in tests
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

    def test_agrees_no_pending_oos_with_classifier_items(self):
        """No pending_oos_resolution but classifier extracted items → new_order template."""
        cls = _FakeClassification(
            dialog_intent="agrees_to_alternative",
            order_items=[
                types.SimpleNamespace(base_flavor="Tropical", product_name="Japanese Tropical", quantity=2),
                types.SimpleNamespace(base_flavor="Black", product_name="Japanese Black", quantity=1),
            ],
        )
        result = self._make_result(payment_type="postpay", zelle_address="")

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=345.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "Yes pls. Ty")

        self.assertTrue(out["template_used"])
        self.assertIn("$345.00", out["draft_reply"])
        self.assertIn("Thank you very much for placing an order", out["draft_reply"])
        self.assertFalse(out["needs_routing"])

    def test_agrees_no_pending_oos_no_items_falls_to_llm(self):
        """No pending_oos_resolution AND no classifier items → LLM fallback."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        result = self._make_result(payment_type="prepay", zelle_address="pay@example.com")

        with patch.object(
            self.handler_mod.oos_followup_agent,
            "run",
            return_value=types.SimpleNamespace(content="LLM order confirmation"),
        ) as run_mock:
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])
        self.assertEqual(out["draft_reply"], "LLM order confirmation")

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

    def test_agrees_no_alternatives_falls_to_llm(self):
        """Full OOS + empty alternatives + no classifier items → LLM fallback."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative")
        state = self._make_pending_oos(num_alternatives=0)
        # Remove alternatives completely
        state["facts"]["pending_oos_resolution"]["alternatives"] = {"Green": {"alternatives": []}}
        result = self._make_result(
            payment_type="prepay", zelle_address="pay@example.com",
            conversation_state=state,
        )

        with patch.object(
            self.handler_mod.oos_followup_agent,
            "run",
            return_value=types.SimpleNamespace(content="LLM fallback reply"),
        ) as run_mock:
            out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])

    def test_agrees_stock_changed_falls_to_llm(self):
        """Alternative sold out since OOS email + no classifier items → LLM fallback."""
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
            with patch.object(
                self.handler_mod.oos_followup_agent,
                "run",
                return_value=types.SimpleNamespace(content="LLM stock changed reply"),
            ) as run_mock:
                out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        run_mock.assert_called_once()
        self.assertFalse(out["template_used"])

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


    # ---------------------------------------------------------------
    # Phase 2 (v3): extraction, source flags, resolve, order_id
    # ---------------------------------------------------------------

    def test_extraction_primary_success(self):
        """[8A.1] extraction PRIMARY: gmail_thread_id present + extraction returns items → template."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-100")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )
        result["gmail_thread_id"] = "thread_abc"
        result["gmail_account"] = "default"

        extracted_items = [
            {"base_flavor": "Smooth", "product_name": "T Smooth", "quantity": 4},
            {"base_flavor": "Bronze", "product_name": "Bronze", "quantity": 2},
        ]

        with patch.object(self.handler_mod, "_extract_agreed_items_from_thread", return_value=extracted_items):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=660.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "Ok. Sounds good.")

        self.assertTrue(out["template_used"])
        self.assertIn("$660.00", out["draft_reply"])
        self.assertEqual(out.get("confirmation_source"), "thread_extraction")
        self.assertEqual(out.get("effective_situation"), "new_order")
        self.assertIsNotNone(out.get("canonical_confirmed_items"))
        self.assertIsNotNone(out.get("_stock_check_items"))

    def test_extraction_overrides_stale_pending(self):
        """[8A.2] extraction returns items → pending path never reached."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-200")
        # Pending has DIFFERENT items (stale)
        state = self._make_pending_oos(num_alternatives=1, alt_names=["Stale Product"])
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )
        result["gmail_thread_id"] = "thread_xyz"

        fresh_items = [
            {"base_flavor": "Silver", "product_name": "Silver", "quantity": 3},
        ]

        with patch.object(self.handler_mod, "_extract_agreed_items_from_thread", return_value=fresh_items):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()) as mock_stock:
                with patch.object(self.handler_mod, "calculate_order_price", return_value=330.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "Yes please")

        self.assertTrue(out["template_used"])
        self.assertEqual(out.get("confirmation_source"), "thread_extraction")
        # check_stock was called with extraction items, NOT pending items
        called_items = mock_stock.call_args.args[0]
        flavors = [i["base_flavor"] for i in called_items]
        self.assertIn("Silver", flavors)
        self.assertNotIn("Stale Product", flavors)

    def test_extraction_fails_falls_to_pending(self):
        """[8A.3] extraction returns None → falls to pending (Outcome A)."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-300")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )
        result["gmail_thread_id"] = "thread_fail"

        with patch.object(self.handler_mod, "_extract_agreed_items_from_thread", return_value=None):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "Sounds good")

        self.assertTrue(out["template_used"])
        self.assertEqual(out.get("confirmation_source"), "pending_oos")
        self.assertEqual(out.get("effective_situation"), "new_order")

    def test_extraction_applies_inbound_qty_modification(self):
        """[8A.4] extraction with modified qty from inbound → template uses extraction qty."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-400")
        result = self._make_result(payment_type="postpay", zelle_address="")
        result["gmail_thread_id"] = "thread_mod"

        # Customer modified qty in reply
        extracted = [
            {"base_flavor": "Green", "product_name": "Green", "quantity": 2},  # was 5
        ]

        with patch.object(self.handler_mod, "_extract_agreed_items_from_thread", return_value=extracted):
            with patch.object(self.handler_mod, "resolve_order_items", return_value=(extracted, [])) as mock_resolve:
                with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                    with patch.object(self.handler_mod, "calculate_order_price", return_value=220.0):
                        out = self.handler_mod.handle_oos_followup(cls, result, "Just 2 please")

        self.assertTrue(out["template_used"])
        # resolve_order_items was called with extraction items
        mock_resolve.assert_called_once()
        called = mock_resolve.call_args.args[0]
        self.assertEqual(called[0]["quantity"], 2)

    def test_no_thread_id_uses_pending(self):
        """[8A.5] no gmail_thread_id → extraction skipped → pending path."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-500")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )
        # No gmail_thread_id in result

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "Ok thanks")

        self.assertTrue(out["template_used"])
        self.assertEqual(out.get("confirmation_source"), "pending_oos")

    def test_outcome_a_calls_resolve_order_items(self):
        """[8A.6] pending path (A) calls resolve_order_items before check_stock."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-600")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "resolve_order_items", return_value=(
            [{"base_flavor": "Silver", "quantity": 3, "product_ids": [10]},
             {"base_flavor": "Alt Product 1", "quantity": 5, "product_ids": [20]}], []
        )) as mock_resolve:
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        mock_resolve.assert_called_once()
        self.assertTrue(out["template_used"])

    def test_stock_check_items_have_product_ids(self):
        """[8A.7] _stock_check_items in result contain product_ids from resolve."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-700")
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )

        resolved_with_ids = [
            {"base_flavor": "Silver", "quantity": 3, "product_ids": [10]},
            {"base_flavor": "Alt Product 1", "quantity": 5, "product_ids": [20]},
        ]
        with patch.object(self.handler_mod, "resolve_order_items", return_value=(resolved_with_ids, [])):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "email text")

        self.assertTrue(out["template_used"])
        stock_items = out.get("_stock_check_items")
        self.assertIsNotNone(stock_items)
        ids = [i.get("product_ids") for i in stock_items]
        self.assertIn([10], ids)
        self.assertIn([20], ids)

    def test_no_effective_situation_without_order_id(self):
        """[8A.8] order_id missing → effective_situation NOT set."""
        cls = _FakeClassification(
            dialog_intent="agrees_to_alternative",
            order_id=None,  # missing
        )
        state = self._make_pending_oos(num_alternatives=1)
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "Ok")

        self.assertTrue(out["template_used"])
        self.assertEqual(out.get("confirmation_source"), "pending_oos")
        # effective_situation NOT set when order_id is None
        self.assertNotIn("effective_situation", out)

    def test_classifier_source_tagged_not_trusted(self):
        """[8A.9] classifier path sets source='classifier' but NOT effective_situation."""
        cls = _FakeClassification(
            dialog_intent="agrees_to_alternative",
            order_id="ORD-900",
            order_items=[
                types.SimpleNamespace(base_flavor="Tropical", product_name="T Tropical", quantity=2),
            ],
        )
        result = self._make_result(payment_type="postpay", zelle_address="")

        with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
            with patch.object(self.handler_mod, "calculate_order_price", return_value=230.0):
                out = self.handler_mod.handle_oos_followup(cls, result, "Yes pls")

        self.assertTrue(out["template_used"])
        self.assertEqual(out.get("confirmation_source"), "classifier")
        # classifier is NOT trusted → no effective_situation
        self.assertNotIn("effective_situation", out)

    def test_gmail_account_passed_to_extraction(self):
        """[8A.10] gmail_account from result is passed to extraction function."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-1000")
        result = self._make_result(payment_type="postpay", zelle_address="")
        result["gmail_thread_id"] = "thread_acct"
        result["gmail_account"] = "sales@example.com"

        with patch.object(
            self.handler_mod, "_extract_agreed_items_from_thread", return_value=None
        ) as mock_extract:
            with patch.object(
                self.handler_mod.oos_followup_agent,
                "run",
                return_value=types.SimpleNamespace(content="LLM fallback"),
            ):
                self.handler_mod.handle_oos_followup(cls, result, "Ok thanks")

        mock_extract.assert_called_once_with("thread_acct", "Ok thanks", "sales@example.com")


if __name__ == "__main__":
    unittest.main()
