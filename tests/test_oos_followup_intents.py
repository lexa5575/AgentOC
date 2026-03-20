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
        _STUBS_CREATED.append("agno")
    if "agno.agent" not in sys.modules:
        agno_agent = types.ModuleType("agno.agent")

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, prompt):
                raise RuntimeError("FakeAgent.run must be patched")

        agno_agent.Agent = FakeAgent
        sys.modules["agno.agent"] = agno_agent
        _STUBS_CREATED.append("agno.agent")
    if "agno.models" not in sys.modules:
        agno_models = types.ModuleType("agno.models")
        agno_models.__path__ = []
        sys.modules["agno.models"] = agno_models
        _STUBS_CREATED.append("agno.models")
    if "agno.models.openai" not in sys.modules:
        agno_models_openai = types.ModuleType("agno.models.openai")

        class FakeOpenAIResponses:
            def __init__(self, *args, **kwargs):
                pass

        agno_models_openai.OpenAIResponses = FakeOpenAIResponses
        sys.modules["agno.models.openai"] = agno_models_openai
        _STUBS_CREATED.append("agno.models.openai")

    # db
    if "db" not in sys.modules:
        db_mod = types.ModuleType("db")
        db_mod.__path__ = []
        db_mod.get_postgres_db = lambda *a, **kw: object()
        sys.modules["db"] = db_mod
        _STUBS_CREATED.append("db")
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
        _STUBS_CREATED.append("db.memory")
    if "db.clients" not in sys.modules:
        db_clients = types.ModuleType("db.clients")
        db_clients.get_client_profile = lambda *a, **kw: None
        db_clients.update_client_summary = lambda *a, **kw: True
        sys.modules["db.clients"] = db_clients
        _STUBS_CREATED.append("db.clients")
    if "db.conversation_state" not in sys.modules:
        db_cs = types.ModuleType("db.conversation_state")
        db_cs.get_state = lambda *a, **kw: None
        db_cs.save_state = lambda *a, **kw: None
        db_cs.get_client_states = lambda *a, **kw: []
        sys.modules["db.conversation_state"] = db_cs
        _STUBS_CREATED.append("db.conversation_state")

    # db.region_family — always reset CATEGORY_REGION_SUFFIX to real values.
    # test_handler_templates.py sets it to {} which breaks region suffix logic
    # in oos_agreement.py when tests run in combined mode.
    if "db.region_family" in sys.modules:
        sys.modules["db.region_family"].CATEGORY_REGION_SUFFIX = {
            "ARMENIA": "ME", "KZ_TEREA": "ME",
            "TEREA_EUROPE": "EU", "TEREA_JAPAN": "Japan",
        }

    # tools — only stub web_search
    if "tools" not in sys.modules:
        try:
            import tools
        except ImportError:
            tools_mod = types.ModuleType("tools")
            tools_mod.__path__ = []
            sys.modules["tools"] = tools_mod
            _STUBS_CREATED.append("tools")
    if "tools.web_search" not in sys.modules:
        tools_ws = types.ModuleType("tools.web_search")
        tools_ws.get_search_tools = lambda: []
        sys.modules["tools.web_search"] = tools_ws
        _STUBS_CREATED.append("tools.web_search")
    if "tools.email_parser" not in sys.modules:
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body  # passthrough in tests
            tools_ep.try_parse_order = lambda *a, **kw: None
            tools_ep.clean_email_body = lambda body: body
            sys.modules["tools.email_parser"] = tools_ep
            _STUBS_CREATED.append("tools.email_parser")

    # utils
    if "utils" not in sys.modules:
        utils_mod = types.ModuleType("utils")
        utils_mod.__path__ = []
        sys.modules["utils"] = utils_mod
        _STUBS_CREATED.append("utils")
    if "utils.telegram" not in sys.modules:
        utils_telegram = types.ModuleType("utils.telegram")
        utils_telegram.send_telegram = lambda *a, **kw: None
        sys.modules["utils.telegram"] = utils_telegram
        _STUBS_CREATED.append("utils.telegram")


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
        self.parser_used = kwargs.get("parser_used", False)


_MODULES_BEFORE_STUBS: dict | None = None
_STUBS_CREATED: list[str] = []


def setup_module():
    """Snapshot sys.modules at execution time (not collection time)."""
    global _MODULES_BEFORE_STUBS
    _MODULES_BEFORE_STUBS = dict(sys.modules)


def teardown_module():
    """Restore sys.modules — only remove stubs WE created, not others' modules."""
    # Remove only modules that _install_stubs() created
    for name in _STUBS_CREATED:
        sys.modules.pop(name, None)
    _STUBS_CREATED.clear()
    # Restore original entries from snapshot
    if _MODULES_BEFORE_STUBS is not None:
        for name, mod in _MODULES_BEFORE_STUBS.items():
            sys.modules[name] = mod


class TestOOSFollowupIntents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.handler_mod = importlib.import_module("agents.handlers.oos_followup")

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

        mock_extract.assert_called_once_with("thread_acct", "Ok thanks", "sales@example.com", result=result)


    # ---------------------------------------------------------------
    # Phase "Region Safety": region preservation + display_name
    # ---------------------------------------------------------------

    def test_extraction_region_preserved_in_resolve(self):
        """[RS.1] Extraction with region suffix → resolve_order_items receives region-aware names."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-RS1")
        result = self._make_result(payment_type="postpay", zelle_address="")
        result["gmail_thread_id"] = "thread_region"
        result["gmail_account"] = "default"

        # LLM extraction returns items with region suffix
        extracted_items = [
            {"base_flavor": "Bronze", "product_name": "Bronze EU", "quantity": 2},
            {"base_flavor": "Silver", "product_name": "Silver EU", "quantity": 3},
        ]

        resolved_call_args = []

        def capture_resolve(items):
            resolved_call_args.append(items)
            # Add display_name like real resolver does
            for item in items:
                item["display_name"] = f"Terea {item['base_flavor']} EU"
            return items, []

        with patch.object(self.handler_mod, "_extract_agreed_items_from_thread", return_value=extracted_items):
            with patch.object(self.handler_mod, "resolve_order_items", side_effect=capture_resolve):
                with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                    with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                        out = self.handler_mod.handle_oos_followup(cls, result, "Ok. Sounds good.")

        self.assertTrue(out["template_used"])
        # Verify resolve got items with region in product_name
        self.assertEqual(len(resolved_call_args), 1)
        pns = [i["product_name"] for i in resolved_call_args[0]]
        self.assertIn("Bronze EU", pns)
        self.assertIn("Silver EU", pns)

    def test_pending_path_category_to_region(self):
        """[RS.2] Pending path: alt category → region-aware product_name for resolver."""
        cls = _FakeClassification(dialog_intent="agrees_to_alternative", order_id="ORD-RS2")
        state = self._make_pending_oos(num_alternatives=1, alt_names=["Bronze"])
        # Set category on the alternative
        pending = state["facts"]["pending_oos_resolution"]
        pending["alternatives"]["Green"]["alternatives"] = [
            {"product_name": "Bronze", "category": "TEREA_EUROPE"},
        ]
        result = self._make_result(
            payment_type="postpay", zelle_address="",
            conversation_state=state,
        )

        resolved_call_args = []

        def capture_resolve(items):
            resolved_call_args.append(items)
            return items, []

        with patch.object(self.handler_mod, "resolve_order_items", side_effect=capture_resolve):
            with patch.object(self.handler_mod, "check_stock_for_order", return_value=self._mock_stock_all_ok()):
                with patch.object(self.handler_mod, "calculate_order_price", return_value=550.0):
                    out = self.handler_mod.handle_oos_followup(cls, result, "Ok")

        self.assertTrue(out["template_used"])
        # Verify the alt item got region suffix from category
        self.assertEqual(len(resolved_call_args), 1)
        alt_item = [i for i in resolved_call_args[0] if "Bronze" in i.get("product_name", "")]
        self.assertTrue(len(alt_item) > 0)
        self.assertIn("EU", alt_item[0]["product_name"])

    def test_order_summary_uses_display_name(self):
        """[RS.3] _build_order_summary prefers display_name over entries[0].category."""
        build_fn = self.handler_mod._build_order_summary
        stock_items = [
            {
                "ordered_qty": 2,
                "display_name": "Terea Bronze EU",  # resolver set this
                "product_name": "Bronze",
                "base_flavor": "Bronze",
                "stock_entries": [
                    {"category": "ARMENIA", "product_name": "Bronze", "quantity": 5},
                ],
            },
        ]
        summary = build_fn(stock_items)
        # Must use display_name ("EU"), not entries[0].category ("ARMENIA" → "ME")
        self.assertIn("Terea Bronze EU", summary)
        self.assertNotIn("ME", summary)


class TestApplyConfirmationFlagsAmbiguity(unittest.TestCase):
    """Phase 3: _apply_confirmation_flags blocks fulfillment on ambiguous variants."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.handler_mod = importlib.import_module("agents.handlers.oos_followup")

    def test_ambiguous_sets_blocked_no_effective_situation(self):
        """[P3] Ambiguous resolved items → fulfillment_blocked, no effective_situation."""
        result = {
            "client_email": "test@example.com",
            "needs_reply": True,
        }
        stock_result = {
            "items": [
                {"base_flavor": "Silver", "ordered_qty": 3, "is_sufficient": True},
                {"base_flavor": "Bronze", "ordered_qty": 2, "is_sufficient": True},
            ],
        }
        resolved_items = [
            {"base_flavor": "Silver", "product_ids": [10, 30, 54], "quantity": 3},  # AMBIGUOUS
            {"base_flavor": "Bronze", "product_ids": [52], "quantity": 2},  # single
        ]
        self.handler_mod._apply_confirmation_flags(
            result, stock_result, resolved_items,
            source="thread_extraction", order_id_norm="ORD-1",
        )

        # Ambiguity gate fired
        self.assertTrue(result.get("fulfillment_blocked"))
        self.assertIn("Silver", result.get("ambiguous_flavors", []))
        # effective_situation must NOT be set
        self.assertNotIn("effective_situation", result)
        # canonical_confirmed_items and _stock_check_items still set for display
        self.assertEqual(result["canonical_confirmed_items"], stock_result["items"])
        self.assertEqual(result["_stock_check_items"], resolved_items)

    def test_no_ambiguity_sets_effective_situation(self):
        """[P3] All single-variant + trusted source → effective_situation set normally."""
        result = {
            "client_email": "test@example.com",
            "needs_reply": True,
        }
        stock_result = {
            "items": [
                {"base_flavor": "Bronze", "ordered_qty": 2, "is_sufficient": True},
            ],
        }
        resolved_items = [
            {"base_flavor": "Bronze", "product_ids": [52], "quantity": 2},
        ]
        self.handler_mod._apply_confirmation_flags(
            result, stock_result, resolved_items,
            source="thread_extraction", order_id_norm="ORD-2",
        )

        self.assertNotIn("fulfillment_blocked", result)
        self.assertEqual(result.get("effective_situation"), "new_order")


class TestNormalizeExtractedRegion(unittest.TestCase):
    """Test the deterministic post-normalization for extracted items."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.handler_mod = importlib.import_module("agents.handlers.oos_followup")

    def test_eu_suffix_preserved(self):
        items = [{"product_name": "Bronze EU", "base_flavor": "Bronze", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    def test_eu_prefix_normalized_to_suffix(self):
        items = [{"product_name": "EU Bronze", "base_flavor": "EU Bronze", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    def test_japan_suffix(self):
        items = [{"product_name": "Smooth Japan", "base_flavor": "Smooth", "quantity": 4}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Smooth Japan")
        self.assertEqual(result[0]["base_flavor"], "Smooth")

    def test_japanese_prefix_normalized(self):
        items = [{"product_name": "Japanese Smooth", "base_flavor": "Japanese Smooth", "quantity": 4}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Smooth Japan")
        self.assertEqual(result[0]["base_flavor"], "Smooth")

    def test_european_prefix_normalized(self):
        items = [{"product_name": "European Bronze", "base_flavor": "European Bronze", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    def test_no_region_passthrough(self):
        items = [{"product_name": "Purple", "base_flavor": "Purple", "quantity": 1}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Purple")
        self.assertEqual(result[0]["base_flavor"], "Purple")

    def test_brand_prefix_stripped(self):
        items = [{"product_name": "T Smooth Japan", "base_flavor": "Smooth", "quantity": 4}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Smooth Japan")
        self.assertEqual(result[0]["base_flavor"], "Smooth")

    def test_terea_brand_prefix_stripped(self):
        items = [{"product_name": "Terea Bronze EU", "base_flavor": "Bronze", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    # --- Stabilization: dual-source region + case-insensitive brand ---

    def test_region_from_base_flavor_when_product_name_bare(self):
        """product_name='Bronze', base_flavor='Bronze EU' → region from bf."""
        items = [{"product_name": "Bronze", "base_flavor": "Bronze EU", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    def test_region_from_base_flavor_japan_prefix(self):
        """product_name='Smooth', base_flavor='Japan Smooth' → region from bf."""
        items = [{"product_name": "Smooth", "base_flavor": "Japan Smooth", "quantity": 3}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Smooth Japan")
        self.assertEqual(result[0]["base_flavor"], "Smooth")

    def test_product_name_region_wins_over_base_flavor(self):
        """product_name='Silver EU', base_flavor='Silver Japan' → EU wins."""
        items = [{"product_name": "Silver EU", "base_flavor": "Silver Japan", "quantity": 1}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Silver EU")
        self.assertEqual(result[0]["base_flavor"], "Silver")

    def test_lowercase_brand_prefix_stripped(self):
        """Lowercase 'terea' brand prefix stripped correctly."""
        items = [{"product_name": "terea Bronze EU", "base_flavor": "Bronze", "quantity": 2}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")

    def test_mixed_case_brand_prefix_stripped(self):
        """Mixed case 'TERA' brand prefix stripped correctly."""
        items = [{"product_name": "TERA Silver Japan", "base_flavor": "Silver", "quantity": 1}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Silver Japan")
        self.assertEqual(result[0]["base_flavor"], "Silver")

    def test_heets_brand_prefix_case_insensitive(self):
        """'HEETS' brand prefix stripped case-insensitively."""
        items = [{"product_name": "HEETS Bronze EU", "base_flavor": "Bronze", "quantity": 1}]
        result = self.handler_mod._normalize_extracted_region(items)
        self.assertEqual(result[0]["product_name"], "Bronze EU")
        self.assertEqual(result[0]["base_flavor"], "Bronze")


# ---------------------------------------------------------------------------
# Tests for OOS template qty in alternatives (P1a)
# ---------------------------------------------------------------------------

class TestOosTemplateAlternativesQty(unittest.TestCase):
    """Tests that alternatives show correct qty."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.templates = importlib.import_module("agents.reply_templates")

    @patch("db.catalog.get_display_name", side_effect=lambda name, cat: f"Terea {name}")
    @patch("db.catalog.get_base_display_name", side_effect=lambda bf: f"Terea {bf}")
    def test_full_oos_shows_ordered_qty(self, mock_base, mock_display):
        """Full OOS ordered_qty=2 → '2 x Terea Amber ME ...'."""
        items = [{"base_flavor": "Amber", "ordered_qty": 2, "total_available": 0}]
        alts = {"Amber": {"alternatives": [
            {"alternative": {"product_name": "Amber ME", "category": "ARMENIA"}, "reason": "same_flavor"},
        ]}}
        text = self.templates.fill_out_of_stock_template(items, alts)
        self.assertIn("2 x Terea Amber ME", text)

    @patch("db.catalog.get_display_name", side_effect=lambda name, cat: f"Terea {name}")
    @patch("db.catalog.get_base_display_name", side_effect=lambda bf: f"Terea {bf}")
    def test_partial_oos_shows_missing_qty(self, mock_base, mock_display):
        """Partial OOS ordered=3, available=1 → '2 x ...' (missing=2)."""
        items = [{"base_flavor": "Bronze", "ordered_qty": 3, "total_available": 1}]
        alts = {"Bronze": {"alternatives": [
            {"alternative": {"product_name": "Bronze EU", "category": "TEREA_EUROPE"}, "reason": "fallback"},
        ]}}
        text = self.templates.fill_out_of_stock_template(items, alts)
        self.assertIn("2 x Terea Bronze EU", text)

    @patch("db.catalog.get_display_name", side_effect=lambda name, cat: f"Terea {name}")
    @patch("db.catalog.get_base_display_name", side_effect=lambda bf: f"Terea {bf}")
    def test_single_qty_no_prefix(self, mock_base, mock_display):
        """ordered_qty=1 → no 'x' prefix."""
        items = [{"base_flavor": "Amber", "ordered_qty": 1, "total_available": 0}]
        alts = {"Amber": {"alternatives": [
            {"alternative": {"product_name": "Amber ME", "category": "ARMENIA"}, "reason": "fallback"},
        ]}}
        text = self.templates.fill_out_of_stock_template(items, alts)
        self.assertIn("Terea Amber ME", text)
        self.assertNotIn("1 x", text)


# ---------------------------------------------------------------------------
# Tests for qty enrichment (P0 fix)
# ---------------------------------------------------------------------------

class TestExtractClientQtyForFlavor(unittest.TestCase):
    """Tests for _extract_client_qty_for_flavor()."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.mod = importlib.import_module("agents.handlers.oos_followup")

    def test_qty_x_amber(self):
        self.assertEqual(self.mod._extract_client_qty_for_flavor("2 x Amber", "amber"), 2)

    def test_amber_3_boxes(self):
        self.assertEqual(self.mod._extract_client_qty_for_flavor("Amber 3 boxes", "amber"), 3)

    def test_just_1_amber(self):
        self.assertEqual(self.mod._extract_client_qty_for_flavor("just 1 Amber", "amber"), 1)

    def test_amber_x2(self):
        self.assertEqual(self.mod._extract_client_qty_for_flavor("Amber x2", "amber"), 2)

    def test_no_match_ok(self):
        self.assertIsNone(self.mod._extract_client_qty_for_flavor("ok sounds good", "amber"))

    def test_no_false_match_remember(self):
        """'remember' should NOT match 'amber' due to word boundaries."""
        self.assertIsNone(self.mod._extract_client_qty_for_flavor("2 remember me", "amber"))

    def test_no_false_match_chamber(self):
        self.assertIsNone(self.mod._extract_client_qty_for_flavor("3 chamber", "amber"))

    def test_no_false_match_amberton(self):
        self.assertIsNone(self.mod._extract_client_qty_for_flavor("2 amberton", "amber"))

    def test_multi_word_sun_pearl(self):
        self.assertEqual(
            self.mod._extract_client_qty_for_flavor("2 x Sun Pearl", "sun pearl"), 2,
        )

    def test_empty_flavor(self):
        self.assertIsNone(self.mod._extract_client_qty_for_flavor("2 boxes", ""))


class TestExtractStandaloneQty(unittest.TestCase):
    """Tests for _extract_standalone_qty()."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.mod = importlib.import_module("agents.handlers.oos_followup")

    def test_just_1_box(self):
        self.assertEqual(self.mod._extract_standalone_qty("just 1 box please"), 1)

    def test_2_cartons(self):
        self.assertEqual(self.mod._extract_standalone_qty("2 cartons"), 2)

    def test_only_3_no_unit(self):
        """'only 3' without unit → None (requires unit to avoid false positives like 'only 2 days ago')."""
        self.assertIsNone(self.mod._extract_standalone_qty("only 3"))

    def test_only_2_days_ago(self):
        """'only 2 days ago' → None (no false match on 'only N' without unit)."""
        self.assertIsNone(self.mod._extract_standalone_qty("only 2 days ago"))

    def test_no_match_sounds_good(self):
        self.assertIsNone(self.mod._extract_standalone_qty("sounds good"))

    def test_no_match_ok(self):
        self.assertIsNone(self.mod._extract_standalone_qty("ok let's go"))


class TestBuildPendingQtyMap(unittest.TestCase):
    """Tests for _build_pending_qty_map()."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.mod = importlib.import_module("agents.handlers.oos_followup")

    def test_direct_oos(self):
        pending = {"items": [{"base_flavor": "Amber", "requested_qty": 2}]}
        result = self.mod._build_pending_qty_map(pending)
        self.assertEqual(result["amber"], 2)

    def test_in_stock(self):
        pending = {"in_stock_items": [{"base_flavor": "Silver", "ordered_qty": 3}]}
        result = self.mod._build_pending_qty_map(pending)
        self.assertEqual(result["silver"], 3)

    def test_reverse_map_alt(self):
        """Bronze is alternative for OOS Amber(qty=2) → bronze gets 2."""
        pending = {
            "items": [{"base_flavor": "Amber", "requested_qty": 2}],
            "alternatives": {
                "Amber": {"alternatives": [{"product_name": "Bronze EU", "category": "TEREA_EUROPE"}]}
            },
        }
        result = self.mod._build_pending_qty_map(pending)
        self.assertEqual(result["amber"], 2)
        self.assertEqual(result["bronze"], 2)

    def test_reverse_map_multi_word(self):
        """Purple Wave is alt for OOS Sun Pearl → purple wave gets parent qty."""
        pending = {
            "items": [{"base_flavor": "Sun Pearl", "requested_qty": 2}],
            "alternatives": {
                "Sun Pearl": {"alternatives": [
                    {"product_name": "Purple Wave EU", "category": "TEREA_EUROPE"},
                ]}
            },
        }
        result = self.mod._build_pending_qty_map(pending)
        self.assertEqual(result["sun pearl"], 2)
        self.assertEqual(result["purple wave"], 2)

    def test_reverse_map_oos_flavor_not_in_map(self):
        """OOS flavor key doesn't match any item → alts skipped (fail-closed)."""
        pending = {
            "items": [{"base_flavor": "Amber", "requested_qty": 2}],
            "alternatives": {
                # Key "Amberr" (typo) doesn't match "Amber" in items
                "Amberr": {"alternatives": [
                    {"product_name": "Bronze EU", "category": "TEREA_EUROPE"},
                ]}
            },
        }
        result = self.mod._build_pending_qty_map(pending)
        self.assertEqual(result["amber"], 2)
        self.assertNotIn("bronze", result)  # not mapped because parent not found

    def test_reverse_map_conflict(self):
        """Bronze is alt for Amber(qty=2) AND Silver(qty=3) → conflict, not in map."""
        pending = {
            "items": [
                {"base_flavor": "Amber", "requested_qty": 2},
                {"base_flavor": "Silver", "requested_qty": 3},
            ],
            "alternatives": {
                "Amber": {"alternatives": [{"product_name": "Bronze EU", "category": "TEREA_EUROPE"}]},
                "Silver": {"alternatives": [{"product_name": "Bronze ME", "category": "ARMENIA"}]},
            },
        }
        result = self.mod._build_pending_qty_map(pending)
        self.assertNotIn("bronze", result)


class TestEnrichQtyFromPending(unittest.TestCase):
    """Tests for _enrich_qty_from_pending()."""

    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.mod = importlib.import_module("agents.handlers.oos_followup")

    def _make_result(self, pending=None):
        """Build a result dict with optional pending_oos_resolution."""
        state = {"facts": {}}
        if pending is not None:
            state["facts"]["pending_oos_resolution"] = pending
        return {"conversation_state": state}

    def test_default_1_to_2(self):
        """LLM returned qty=1, pending says 2, text='ok' → enriched to 2."""
        extracted = [{"base_flavor": "Amber", "quantity": 1}]
        result = self._make_result({"items": [{"base_flavor": "Amber", "requested_qty": 2}]})
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "ok let's go")
        self.assertEqual(enriched[0]["quantity"], 2)

    def test_preserves_explicit_gt1(self):
        """LLM returned qty=3 (client increased) → stays 3."""
        extracted = [{"base_flavor": "Amber", "quantity": 3}]
        result = self._make_result({"items": [{"base_flavor": "Amber", "requested_qty": 2}]})
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "make it 3")
        self.assertEqual(enriched[0]["quantity"], 3)

    def test_client_specifies_for_flavor(self):
        """'just 1 Amber' → Amber stays 1 despite pending=2."""
        extracted = [{"base_flavor": "Amber", "quantity": 1}]
        result = self._make_result({"items": [{"base_flavor": "Amber", "requested_qty": 2}]})
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "just 1 Amber please")
        self.assertEqual(enriched[0]["quantity"], 1)

    def test_client_specifies_qty_with_brand_prefix(self):
        """'1 Terea Bronze and 1 Terea Sun Pearl' → both stay 1 despite pending=2.

        Regression: brand name (Terea/IQOS/Heets) between qty and flavor
        was not matched, causing enrichment to override client's explicit qty.
        """
        extracted = [
            {"base_flavor": "Bronze", "quantity": 1},
            {"base_flavor": "Sun Pearl", "quantity": 1},
        ]
        pending = {
            "items": [{"base_flavor": "KONA", "requested_qty": 2}],
            "in_stock_items": [{"base_flavor": "Oasis Pearl", "ordered_qty": 1}],
            "alternatives": {
                "KONA": {"alternatives": [
                    {"product_name": "Bronze EU", "category": "TEREA_EUROPE"},
                ]},
                "Oasis Pearl": {"alternatives": [
                    {"product_name": "Sun Pearl ME", "category": "ARMENIA"},
                ]},
            },
        }
        result = self._make_result(pending)
        text = "Can you please send 1 Terea Bronze and 1 Terea Sun Pearl"
        enriched = self.mod._enrich_qty_from_pending(extracted, result, text)
        self.assertEqual(enriched[0]["quantity"], 1, "Bronze should stay 1")
        self.assertEqual(enriched[1]["quantity"], 1, "Sun Pearl should stay 1")

    def test_no_pending(self):
        """No pending → no change."""
        extracted = [{"base_flavor": "Amber", "quantity": 1}]
        result = self._make_result(None)
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "ok")
        self.assertEqual(enriched[0]["quantity"], 1)

    def test_multi_item_partial_explicit(self):
        """2 items, qty near only 1 flavor → only that keeps client qty, other enriched."""
        extracted = [
            {"base_flavor": "Amber", "quantity": 1},
            {"base_flavor": "Bronze", "quantity": 1},
        ]
        result = self._make_result({
            "items": [
                {"base_flavor": "Amber", "requested_qty": 2},
                {"base_flavor": "Bronze", "requested_qty": 3},
            ],
        })
        enriched = self.mod._enrich_qty_from_pending(
            extracted, result, "1 x Amber and Bronze",
        )
        self.assertEqual(enriched[0]["quantity"], 1)   # client said "1 x Amber"
        self.assertEqual(enriched[1]["quantity"], 3)   # enriched from pending

    def test_reverse_map_alt_flavor(self):
        """OOS Amber(qty=2), client chose Bronze (alt) → Bronze gets qty=2."""
        extracted = [{"base_flavor": "Bronze", "quantity": 1}]
        result = self._make_result({
            "items": [{"base_flavor": "Amber", "requested_qty": 2}],
            "alternatives": {
                "Amber": {"alternatives": [
                    {"product_name": "Bronze EU", "category": "TEREA_EUROPE"},
                ]},
            },
        })
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "I'll take Bronze EU")
        self.assertEqual(enriched[0]["quantity"], 2)

    def test_single_item_standalone_qty(self):
        """Single item, 'just 1 box please' (no flavor) → qty=1 (standalone)."""
        extracted = [{"base_flavor": "Bronze", "quantity": 1}]
        result = self._make_result({
            "items": [{"base_flavor": "Amber", "requested_qty": 2}],
            "alternatives": {
                "Amber": {"alternatives": [
                    {"product_name": "Bronze EU", "category": "TEREA_EUROPE"},
                ]},
            },
        })
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "just 1 box please")
        self.assertEqual(enriched[0]["quantity"], 1)

    def test_reverse_map_conflict_no_enrich(self):
        """Bronze is alt for both Amber(qty=2) AND Silver(qty=3) → no enrichment."""
        extracted = [{"base_flavor": "Bronze", "quantity": 1}]
        result = self._make_result({
            "items": [
                {"base_flavor": "Amber", "requested_qty": 2},
                {"base_flavor": "Silver", "requested_qty": 3},
            ],
            "alternatives": {
                "Amber": {"alternatives": [{"product_name": "Bronze EU", "category": "TEREA_EUROPE"}]},
                "Silver": {"alternatives": [{"product_name": "Bronze ME", "category": "ARMENIA"}]},
            },
        })
        enriched = self.mod._enrich_qty_from_pending(extracted, result, "ok Bronze")
        # Conflict: Bronze maps to both 2 and 3 → stays at extracted qty=1
        self.assertEqual(enriched[0]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
