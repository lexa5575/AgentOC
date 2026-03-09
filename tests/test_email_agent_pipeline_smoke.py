"""End-to-end smoke tests for email pipeline with router architecture.

The tests use realistic email texts and run through:
classifier -> process_classified_email -> router -> handler -> formatting/saving.

External runtime dependencies are stubbed so this suite runs locally without
OpenAI/Gmail/Postgres services.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from unittest.mock import patch


def _install_import_stubs() -> None:
    """Install lightweight module stubs required to import agents.email_agent."""
    for name in list(sys.modules):
        if (
            name == "agents.email_agent"
            or name == "agents.router"
            or name == "agents.context"
            or name == "agents.checker"
            or name == "agents.state_updater"
            or name.startswith("agents.handlers")
            or name == "db.conversation_state"
            or name in ("agents.pipeline", "agents.notifier", "agents.classifier",
                        "agents.formatters", "agents.models")
        ):
            sys.modules.pop(name, None)

    agno = types.ModuleType("agno")
    agno.__path__ = []
    agno_agent = types.ModuleType("agno.agent")

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, prompt):
            raise RuntimeError("FakeAgent.run must be patched in tests")

    agno_agent.Agent = FakeAgent

    agno_models = types.ModuleType("agno.models")
    agno_models.__path__ = []
    agno_models_openai = types.ModuleType("agno.models.openai")

    class FakeOpenAIResponses:
        def __init__(self, *args, **kwargs):
            pass

    agno_models_openai.OpenAIResponses = FakeOpenAIResponses

    sys.modules["agno"] = agno
    sys.modules["agno.agent"] = agno_agent
    sys.modules["agno.models"] = agno_models
    sys.modules["agno.models.openai"] = agno_models_openai

    db_mod = types.ModuleType("db")
    db_mod.__path__ = []
    db_mod.get_postgres_db = lambda *args, **kwargs: object()

    db_memory = types.ModuleType("db.memory")
    db_memory.get_full_email_history = lambda *args, **kwargs: []
    db_memory.save_email = lambda *args, **kwargs: None
    db_memory.save_order_items = lambda *args, **kwargs: None
    db_memory.get_client = lambda *args, **kwargs: None
    db_memory.decrement_discount = lambda *args, **kwargs: None
    db_memory.get_stock_summary = lambda *args, **kwargs: {"total": 0}
    db_memory.check_stock_for_order = lambda *args, **kwargs: {
        "all_in_stock": True,
        "items": [],
        "insufficient_items": [],
    }
    db_memory.calculate_order_price = lambda *args, **kwargs: None
    db_memory.resolve_order_items = lambda items, **kw: (items, [])
    db_memory.select_best_alternatives = lambda *args, **kwargs: {"alternatives": []}
    db_memory.get_full_thread_history = lambda *args, **kwargs: []
    db_memory.update_client = lambda *args, **kwargs: None
    db_memory.replace_order_items = lambda *args, **kwargs: 0
    db_clients = types.ModuleType("db.clients")
    db_clients.get_client_profile = lambda *args, **kwargs: None
    db_clients.update_client_summary = lambda *args, **kwargs: True
    db_conversation_state = types.ModuleType("db.conversation_state")
    db_conversation_state.get_state = lambda *args, **kwargs: None
    db_conversation_state.save_state = lambda *args, **kwargs: None
    db_conversation_state.get_client_states = lambda *args, **kwargs: []

    # Stub db.stock so pipeline + downstream can import extract_variant_id, CATEGORY_PRICES, etc.
    db_stock = types.ModuleType("db.stock")

    def _stub_extract_variant_id(product_ids, catalog_entries=None, client_email=None):
        if not product_ids:
            return None
        if len(product_ids) == 1:
            return product_ids[0]
        return None

    db_stock.extract_variant_id = _stub_extract_variant_id
    db_stock._extract_variant_id = _stub_extract_variant_id

    def _stub_has_ambiguous_variants(items, catalog_entries=None, client_email=None):
        return [
            item.get("base_flavor", "?")
            for item in items
            if len(item.get("product_ids") or []) > 1
        ]

    db_stock.has_ambiguous_variants = _stub_has_ambiguous_variants
    db_stock._has_ambiguous_variants = _stub_has_ambiguous_variants
    db_stock.CATEGORY_PRICES = {
        "TEREA_EUROPE": 110, "KZ_TEREA": 110, "ARMENIA": 110,
        "TEREA_JAPAN": 115, "УНИКАЛЬНАЯ_ТЕРЕА": 115,
        "ONE": 99, "STND": 149, "PRIME": 245,
    }
    db_stock.STICK_CATEGORIES = {"KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA", "УНИКАЛЬНАЯ_ТЕРЕА"}
    db_stock.DEVICE_CATEGORIES = {"ONE", "STND", "PRIME"}
    db_stock._REGION_CATEGORY_MAP = {}
    db_stock.search_stock = lambda *args, **kwargs: []
    db_stock.search_stock_by_ids = lambda *args, **kwargs: []
    db_stock.select_best_alternatives = lambda *args, **kwargs: {"alternatives": []}
    db_stock.get_product_type = lambda bf: "stick"
    db_stock.resolve_warehouse = lambda text: None
    db_stock.get_client_flavor_history = lambda *args, **kwargs: []
    db_stock.save_order_items = lambda *args, **kwargs: 0

    sys.modules["db"] = db_mod
    sys.modules["db.memory"] = db_memory
    sys.modules["db.stock"] = db_stock
    sys.modules["db.clients"] = db_clients
    sys.modules["db.conversation_state"] = db_conversation_state

    # Only stub tools.web_search; preserve real tools package for stock_parser
    if "tools" not in sys.modules:
        try:
            import tools
        except ImportError:
            tools_mod = types.ModuleType("tools")
            tools_mod.__path__ = []
            sys.modules["tools"] = tools_mod
    tools_web_search = types.ModuleType("tools.web_search")
    tools_web_search.get_search_tools = lambda: []
    sys.modules["tools.web_search"] = tools_web_search
    if "tools.email_parser" not in sys.modules:
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body
            tools_ep.try_parse_order = lambda *a, **kw: None
            tools_ep.clean_email_body = lambda body: body
            sys.modules["tools.email_parser"] = tools_ep

    utils_mod = types.ModuleType("utils")
    utils_mod.__path__ = []
    utils_telegram = types.ModuleType("utils.telegram")
    utils_telegram.send_telegram = lambda *args, **kwargs: None
    sys.modules["utils"] = utils_mod
    sys.modules["utils.telegram"] = utils_telegram


class TestEmailPipelineSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Snapshot sys.modules so we can restore after the test class runs.
        # This prevents stub modules (especially db.stock) from leaking into
        # subsequent test files in the same pytest process.
        cls._modules_snapshot = dict(sys.modules)
        _install_import_stubs()
        cls.email_agent = importlib.import_module("agents.email_agent")
        cls.agents_pipeline = importlib.import_module("agents.pipeline")
        cls.agents_notifier = importlib.import_module("agents.notifier")
        cls.agents_classifier = importlib.import_module("agents.classifier")
        cls.checker = importlib.import_module("agents.checker")
        cls.h_general = importlib.import_module("agents.handlers.general")
        cls.h_tracking = importlib.import_module("agents.handlers.tracking")
        cls.h_payment = importlib.import_module("agents.handlers.payment")
        cls.h_discount = importlib.import_module("agents.handlers.discount")
        cls.h_shipping = importlib.import_module("agents.handlers.shipping")
        cls.h_oos_followup = importlib.import_module("agents.handlers.oos_followup")

    @classmethod
    def tearDownClass(cls):
        # Restore sys.modules to pre-stub state so subsequent test files
        # in the same pytest process get the real modules (especially db.stock).
        stubs_added = set(sys.modules) - set(cls._modules_snapshot)
        for name in stubs_added:
            del sys.modules[name]
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def setUp(self):
        self.saved = []
        self.telegrams = []
        self.classifier_calls = 0

        self.clients = {
            "client1@example.com": {
                "email": "client1@example.com",
                "name": "Test Client One",
                "payment_type": "prepay",
                "zelle_address": "pay@example.com",
                "discount_percent": 0,
                "discount_orders_left": 0,
            },
            "client2@example.com": {
                "email": "client2@example.com",
                "name": "Test Client Two",
                "payment_type": "postpay",
                "zelle_address": "",
                "discount_percent": 0,
                "discount_orders_left": 0,
            },
        }

        # Fake CheckResult for checker stub
        fake_check = self.checker.CheckResult()  # is_ok=True by default

        self.patchers = [
            patch.object(self.agents_classifier.classifier_agent, "run", side_effect=self._classifier_run),
            patch.object(self.agents_pipeline, "get_client", side_effect=self._get_client),
            patch.object(self.agents_pipeline, "get_stock_summary", side_effect=self._get_stock_summary),
            patch.object(self.agents_pipeline, "resolve_order_items", side_effect=lambda items, **kw: (items, [])),
            patch.object(self.agents_pipeline, "check_stock_for_order", side_effect=self._check_stock_for_order),
            patch.object(
                self.agents_pipeline,
                "select_best_alternatives",
                side_effect=self._select_best_alternatives,
            ),
            patch.object(self.agents_pipeline, "save_email", side_effect=self._save_email),
            patch.object(self.agents_pipeline, "save_order_items", return_value=None),
            patch.object(self.agents_pipeline, "replace_order_items", return_value=0),
            patch.object(self.agents_notifier, "send_telegram", side_effect=self._send_telegram),
            # Checker: return clean result (no LLM call)
            patch.object(self.agents_pipeline, "check_reply", return_value=fake_check),
            # State updater: return empty state (no LLM call)
            patch.object(self.agents_pipeline, "update_conversation_state", return_value={}),
            # Handler agents
            patch.object(self.h_tracking.tracking_agent, "run", return_value=types.SimpleNamespace(
                content="Your tracking number is AB123. Thank you!"
            )),
            patch.object(self.h_payment.payment_agent, "run", return_value=types.SimpleNamespace(
                content="Please send payment via Zelle to pay@example.com. Thank you!"
            )),
            patch.object(self.h_discount.discount_agent, "run", return_value=types.SimpleNamespace(
                content="Unfortunately, no active discounts right now. Thank you!"
            )),
            patch.object(self.h_shipping.shipping_agent, "run", return_value=types.SimpleNamespace(
                content="We ship via USPS and delivery takes 2-4 business days. Thank you!"
            )),
            patch.object(self.h_general.general_agent, "run", return_value=types.SimpleNamespace(
                content="We'll check and get back to you. Thank you!"
            )),
            patch.object(self.h_oos_followup.oos_followup_agent, "run", return_value=types.SimpleNamespace(
                content="Hi! We'll update your order with the alternative. Thank you!"
            )),
        ]

        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()

    # ----- fake infra -----
    def _save_email(self, **kwargs):
        self.saved.append(kwargs)

    def _send_telegram(self, text: str):
        self.telegrams.append(text)

    def _history(self, client_email: str, max_results: int = 10):
        return [
            {
                "direction": "outbound",
                "subject": "Previous",
                "body": "Old message",
                "created_at": None,
            }
        ]

    def _get_client(self, email: str):
        return self.clients.get(email)

    def _get_stock_summary(self):
        return {"total": 10}

    def _check_stock_for_order(self, items: list[dict]):
        has_oos = any(i.get("base_flavor") == "Turquoise" for i in items)
        if not has_oos:
            return {
                "all_in_stock": True,
                "items": [
                    {
                        "base_flavor": i["base_flavor"],
                        "ordered_qty": i["quantity"],
                        "total_available": 10,
                        "is_sufficient": True,
                    }
                    for i in items
                ],
                "insufficient_items": [],
            }
        return {
            "all_in_stock": False,
            "items": [
                {
                    "base_flavor": "Turquoise",
                    "ordered_qty": 3,
                    "total_available": 0,
                    "is_sufficient": False,
                }
            ],
            "insufficient_items": [
                {
                    "base_flavor": "Turquoise",
                    "ordered_qty": 3,
                    "total_available": 0,
                    "product_name": "Tera Turquoise EU",
                }
            ],
        }

    def _select_best_alternatives(self, **kwargs):
        return {
            "alternatives": [
                {
                    "alternative": {
                        "product_name": "Tera Green EU",
                        "category": "TEREA_EUROPE",
                        "quantity": 5,
                    },
                    "reason": "fallback",
                }
            ]
        }

    def _classifier_run(self, email_text: str):
        self.classifier_calls += 1
        text = email_text.lower()
        if "where is my order" in text:
            payload = {
                "needs_reply": True,
                "situation": "tracking",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": None,
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": None,
            }
        elif "how can i pay" in text:
            payload = {
                "needs_reply": True,
                "situation": "payment_question",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": None,
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": None,
            }
        elif "thank you so much!" in text:
            payload = {
                "needs_reply": False,
                "situation": "other",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": None,
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": None,
            }
        elif "order 77777" in text:
            payload = {
                "needs_reply": True,
                "situation": "new_order",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": "77777",
                "price": "$285.00",
                "customer_street": "123 Main St",
                "customer_city_state_zip": "Springfield, Illinois 62701",
                "items": "Tera Turquoise EU x 3",
                "order_items": [
                    {"product_name": "Tera Turquoise EU", "base_flavor": "Turquoise", "quantity": 3}
                ],
            }
        elif "order 23432" in text:
            payload = {
                "needs_reply": True,
                "situation": "new_order",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": "23432",
                "price": "$220.00",
                "customer_street": "123 Main St",
                "customer_city_state_zip": "Springfield, Illinois 62701",
                "items": "Tera Green x 2",
                "order_items": [
                    {"product_name": "Tera Green EU", "base_flavor": "Green", "quantity": 2}
                ],
            }
        elif "yes i'll take the green" in text:
            payload = {
                "needs_reply": True,
                "situation": "oos_followup",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": "77777",
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": [
                    {"product_name": "Tera Green EU", "base_flavor": "Green", "quantity": 3}
                ],
                "is_followup": True,
                "followup_to": "oos_notification",
                "dialog_intent": "agrees_to_alternative",
            }
        else:
            payload = {
                "needs_reply": True,
                "situation": "other",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": None,
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": None,
            }
        return types.SimpleNamespace(content=json.dumps(payload))

    # ----- smoke cases -----
    def test_new_order_template_flow(self):
        email = (
            "From: noreply@shipmecarton.com\n"
            "Reply-To: client1@example.com\n"
            "Subject: Shipmecarton - Order 23432\n"
            "Body: \n"
            "1 Tera Green EU $110.00 2 $220.00\n"
            "Payment amount: $220.00\n"
            "Order ID: 23432\n"
            "Firstname: Test Client One\n"
            "Street address1: 123 Main St\n"
            "Town/City: Springfield\n"
            "State: Illinois\n"
            "Postcode/Zip: 62701\n"
            "Email: client1@example.com"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: new_order", out)
        self.assertIn("Thank you so much for placing an order", out)
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(self.saved[1]["direction"], "outbound")
        self.assertIn("Thank you so much for placing an order", self.saved[1]["body"])
        # Parser handled this — LLM classifier must NOT have been called
        self.assertEqual(self.classifier_calls, 0, "Parser should handle order, not LLM")

    def test_tracking_flow(self):
        email = (
            "From: client1@example.com\n"
            "Subject: Re: Order 23432\n"
            "Body: where is my order?"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: tracking", out)
        self.assertIn("Your tracking number is AB123. Thank you!", out)
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(self.saved[1]["body"], "Your tracking number is AB123. Thank you!")

    def test_payment_question_flow(self):
        email = (
            "From: client1@example.com\n"
            "Subject: Payment\n"
            "Body: How can I pay?"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: payment_question", out)
        self.assertIn("Please send payment via Zelle to pay@example.com. Thank you!", out)
        self.assertEqual(len(self.saved), 2)

    def test_oos_new_order_flow(self):
        email = (
            "From: noreply@shipmecarton.com\n"
            "Reply-To: client1@example.com\n"
            "Subject: Shipmecarton - Order 77777\n"
            "Body: \n"
            "1 Tera Turquoise EU $95.00 3 $285.00\n"
            "Payment amount: $285.00\n"
            "Order ID: 77777\n"
            "Email: client1@example.com"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: new_order", out)
        self.assertIn("STOCK CHECK", out)
        self.assertIn("Unfortunately, we just ran out of Terea Turquoise", out)
        self.assertEqual(len(self.saved), 2)
        self.assertTrue(any("Нет на складе" in t for t in self.telegrams))

    def test_no_reply_flow(self):
        email = (
            "From: client1@example.com\n"
            "Subject: Re: thanks\n"
            "Body: Thank you so much!"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Needs Reply: False", out)
        self.assertIn("(No reply needed)", out)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["direction"], "inbound")

    def test_template_address_fallback_from_db(self):
        """When classification has no address, template uses client DB address.

        payment_received + prepay triggers a template with {CUSTOMER_STREET} and
        {CUSTOMER_CITY_STATE_ZIP}. Classifier returns null for both fields →
        template should fall back to client DB values.
        """
        # Add address to client data
        self.clients["client1@example.com"]["street"] = "99 Pine Rd"
        self.clients["client1@example.com"]["city_state_zip"] = "Austin, TX 73301"

        # Override classifier to return payment_received with no address
        def _classify_paid(email_text):
            payload = {
                "needs_reply": True,
                "situation": "payment_received",
                "client_email": "client1@example.com",
                "client_name": "Test Client One",
                "order_id": None,
                "price": None,
                "customer_street": None,
                "customer_city_state_zip": None,
                "items": None,
                "order_items": None,
            }
            return types.SimpleNamespace(content=json.dumps(payload))

        with patch.object(self.agents_classifier.classifier_agent, "run", side_effect=_classify_paid):
            email = (
                "From: client1@example.com\n"
                "Subject: Re: Order\n"
                "Body: Paid. Thanks!"
            )
            out = self.email_agent.classify_and_process(email)

        # Template should have picked up address from client DB via fallback
        outbound = self.saved[1]["body"]
        self.assertIn("99 Pine Rd", outbound)
        self.assertIn("Austin, TX 73301", outbound)

    def test_auto_save_address_from_classification(self):
        """Step 6.5: address extracted by Classifier is saved to client DB."""
        update_calls = []

        def _track_update(email, **fields):
            update_calls.append({"email": email, "fields": fields})
            return None

        with patch.object(self.agents_pipeline, "update_client", side_effect=_track_update):
            email = (
                "From: noreply@shipmecarton.com\n"
                "Reply-To: client1@example.com\n"
                "Subject: Shipmecarton - Order 23432\n"
                "Body: \n"
                "1 Tera Green EU $110.00 2 $220.00\n"
                "Payment amount: $220.00\n"
                "Order ID: 23432\n"
                "Firstname: Test Client One\n"
                "Street address1: 123 Main St\n"
                "Town/City: Springfield\n"
                "State: Illinois\n"
                "Postcode/Zip: 62701\n"
                "Email: client1@example.com"
            )
            self.email_agent.classify_and_process(email)

        # Parser extracts street="123 Main St", city_state_zip="Springfield, Illinois 62701"
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0]["email"], "client1@example.com")
        self.assertEqual(update_calls[0]["fields"]["street"], "123 Main St")
        self.assertEqual(update_calls[0]["fields"]["city_state_zip"], "Springfield, Illinois 62701")

    def test_oos_followup_flow(self):
        """OOS followup — customer agrees to alternative, routed to oos_followup handler (template)."""
        stock_ok = {
            "all_in_stock": True,
            "items": [
                {
                    "base_flavor": "Green",
                    "product_name": "Green",
                    "ordered_qty": 3,
                    "stock_entries": [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 10}],
                    "total_available": 10,
                    "is_sufficient": True,
                }
            ],
            "insufficient_items": [],
        }
        with patch.object(self.h_oos_followup, "check_stock_for_order", return_value=stock_ok):
            with patch.object(self.h_oos_followup, "calculate_order_price", return_value=330.0):
                email = (
                    "From: client1@example.com\n"
                    "Subject: Re: Your order\n"
                    "Body: Yes I'll take the green instead"
                )
                out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: oos_followup", out)
        # agrees_to_alternative + classifier items → new_order template with price
        self.assertIn("[Template - exact copy]", out)
        self.assertIn("$330.00", out)
        self.assertIn("pay@example.com", out)  # zelle_address filled
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(self.saved[1]["direction"], "outbound")


    def test_reply_with_quoted_order_uses_llm(self):
        """Customer reply quoting an order notification → parser skips, LLM classifies."""
        email = (
            "From: client1@example.com\n"
            "Subject: Re: Shipmecarton - Order 23432\n"
            "Body: Yes, please ship it!\n"
            "\n"
            "On Mon, Mar 3, 2026 at 10:00 AM Shipmecarton wrote:\n"
            "> Order ID: 23432\n"
            "> Payment amount: $220.00\n"
            "> Email: client1@example.com\n"
            "> 1 Tera Green EU $110.00 2 $220.00"
        )
        out = self.email_agent.classify_and_process(email)

        # Parser should NOT have handled this (no Shipmecarton in From header)
        # LLM classifier MUST have been called
        self.assertGreater(self.classifier_calls, 0, "LLM should classify quoted-order reply")

    # ---------------------------------------------------------------
    # Phase 3: gmail_account propagation + persistence gate (plan §7.3)
    # ---------------------------------------------------------------

    def _make_fake_classification(self, **overrides):
        """Build a fake classification SimpleNamespace with defaults."""
        defaults = {
            "client_email": "client1@example.com",
            "client_name": "Test Client One",
            "situation": "oos_followup",
            "needs_reply": True,
            "order_id": "ORD-P3",
            "price": None,
            "customer_street": None,
            "customer_city_state_zip": None,
            "order_items": None,
            "parser_used": False,
        }
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    def _make_persist_result(self, **overrides):
        """Build a minimal result dict suitable for _persist_results."""
        defaults = {
            "needs_reply": True,
            "draft_reply": "Test reply",
            "client_found": True,
            "client_data": self.clients["client1@example.com"],
            "template_used": True,
        }
        defaults.update(overrides)
        return defaults

    def test_gmail_account_propagated_to_result(self):
        """[§7.3.1] gmail_account is set on result dict before handler routing."""
        email = (
            "From: client1@example.com\n"
            "Subject: Re: Order\n"
            "Body: where is my order?"
        )
        # Intercept result dict after routing
        original_route = self.agents_pipeline.route_to_handler

        captured = {}

        def spy_route(cls, result, email_text):
            captured["gmail_account"] = result.get("gmail_account")
            return original_route(cls, result, email_text)

        with patch.object(self.agents_pipeline, "route_to_handler", side_effect=spy_route):
            self.email_agent.classify_and_process(email, gmail_account="sales@test.com")

        self.assertEqual(captured.get("gmail_account"), "sales@test.com")

    def test_oos_replace_called_for_trusted_source(self):
        """[§7.3.2] trusted source + effective_situation + order_id + canonical → replace called."""
        cls = self._make_fake_classification(order_id="ORD-T1")
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="thread_extraction",
            canonical_confirmed_items=[
                {"base_flavor": "Silver", "product_name": "Tera Silver EU", "ordered_qty": 3},
                {"base_flavor": "Bronze", "product_name": "Tera Bronze EU", "ordered_qty": 2},
            ],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=2) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_1", "msg_1", "email text",
            )

        mock_replace.assert_called_once()
        call_kw = mock_replace.call_args
        self.assertEqual(call_kw.kwargs.get("client_email") or call_kw[1].get("client_email", call_kw[0][0] if call_kw[0] else None),
                         "client1@example.com")

    def test_oos_replace_pending_oos_source_trusted(self):
        """[§7.3.2b] pending_oos source is also trusted for replace."""
        cls = self._make_fake_classification(order_id="ORD-T2")
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="pending_oos",
            canonical_confirmed_items=[
                {"base_flavor": "Green", "product_name": "Tera Green", "ordered_qty": 5},
            ],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=1) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_2", "msg_2", "email text",
            )

        mock_replace.assert_called_once()

    def test_oos_replace_skipped_for_classifier_source(self):
        """[§7.3.3] classifier source → replace NOT called."""
        cls = self._make_fake_classification(order_id="ORD-C1")
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="classifier",
            canonical_confirmed_items=[
                {"base_flavor": "Tropical", "product_name": "Tropical", "ordered_qty": 2},
            ],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=0) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_c", "msg_c", "email text",
            )

        mock_replace.assert_not_called()

    def test_oos_replace_skipped_for_no_order_id(self):
        """[§7.3.4] order_id=None → replace NOT called even with trusted source."""
        cls = self._make_fake_classification(order_id=None)
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="thread_extraction",
            canonical_confirmed_items=[
                {"base_flavor": "Silver", "product_name": "Tera Silver", "ordered_qty": 3},
            ],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=0) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_n", "msg_n", "email text",
            )

        mock_replace.assert_not_called()

    def test_oos_replace_skipped_for_empty_canonical(self):
        """[§7.3.4b] empty canonical_confirmed_items → replace NOT called."""
        cls = self._make_fake_classification(order_id="ORD-E1")
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="thread_extraction",
            canonical_confirmed_items=[],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=0) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_e", "msg_e", "email text",
            )

        mock_replace.assert_not_called()

    def test_native_new_order_still_uses_save_order_items(self):
        """[§7.3.5] native new_order path (no effective_situation) → save_order_items, NOT replace."""
        cls = self._make_fake_classification(
            situation="new_order",
            order_id="ORD-N1",
            order_items=[
                types.SimpleNamespace(product_name="Tera Green EU", base_flavor="Green", quantity=2),
            ],
        )
        result = self._make_persist_result()
        # No effective_situation or confirmation_source

        with patch.object(self.agents_pipeline, "save_order_items", return_value=None) as mock_save:
            with patch.object(self.agents_pipeline, "replace_order_items", return_value=0) as mock_replace:
                self.agents_pipeline._persist_results(
                    cls, result, "thread_nat", "msg_nat", "email text",
                )

        mock_save.assert_called_once()
        mock_replace.assert_not_called()

    def test_oos_replace_maps_ordered_qty_to_quantity(self):
        """[§7.3.6] canonical items with ordered_qty are mapped to quantity for replace."""
        cls = self._make_fake_classification(order_id="ORD-M1")
        result = self._make_persist_result(
            effective_situation="new_order",
            confirmation_source="thread_extraction",
            canonical_confirmed_items=[
                {"base_flavor": "Silver", "product_name": "Tera Silver EU", "ordered_qty": 4},
            ],
        )

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=1) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_m", "msg_m", "email text",
            )

        mock_replace.assert_called_once()
        items_arg = mock_replace.call_args.kwargs.get("order_items",
                     mock_replace.call_args[0][2] if len(mock_replace.call_args[0]) > 2 else None)
        self.assertEqual(items_arg[0]["quantity"], 4)
        self.assertEqual(items_arg[0]["base_flavor"], "Silver")

    def test_no_effective_situation_no_replace(self):
        """[§7.3.7] no effective_situation at all → replace NOT called."""
        cls = self._make_fake_classification(situation="oos_followup", order_id="ORD-X1")
        result = self._make_persist_result()
        # No effective_situation key

        with patch.object(self.agents_pipeline, "replace_order_items", return_value=0) as mock_replace:
            self.agents_pipeline._persist_results(
                cls, result, "thread_x", "msg_x", "email text",
            )

        mock_replace.assert_not_called()

    # ---------------------------------------------------------------
    # Phase 2 patch: variant_id / resolved source-of-truth tests
    # ---------------------------------------------------------------

    def test_new_order_save_uses_resolved_fields_from_stock_check(self):
        """[P2.a] save_order_items receives resolved product_name/base_flavor
        from _stock_check_items, NOT raw classifier fields."""
        cls = self._make_fake_classification(
            situation="new_order",
            order_id="ORD-RES1",
            order_items=[
                types.SimpleNamespace(
                    product_name="Tera Green",  # raw classifier name
                    base_flavor="Green",        # raw classifier flavor
                    quantity=2,
                ),
            ],
        )
        result = self._make_persist_result(
            _stock_check_items=[
                {
                    "product_name": "Green EU",        # resolved name
                    "base_flavor": "Green",             # resolved flavor
                    "quantity": 2,
                    "product_ids": [42],
                    "display_name": "Terea Green EU",
                },
            ],
        )

        with patch.object(self.agents_pipeline, "save_order_items", return_value=1) as mock_save:
            self.agents_pipeline._persist_results(
                cls, result, "thread_res", "msg_res", "email text",
            )

        mock_save.assert_called_once()
        items_arg = mock_save.call_args.kwargs.get(
            "order_items",
            mock_save.call_args[0][2] if len(mock_save.call_args[0]) > 2 else None,
        )
        self.assertIsNotNone(items_arg)
        saved_item = items_arg[0]
        # Must use resolved values from _stock_check_items
        self.assertEqual(saved_item["product_name"], "Green EU")
        self.assertEqual(saved_item["base_flavor"], "Green")
        self.assertEqual(saved_item["quantity"], 2)
        self.assertEqual(saved_item["variant_id"], 42)
        self.assertEqual(saved_item["display_name_snapshot"], "Terea Green EU")

    def test_new_order_save_passes_variant_fields(self):
        """[P2.b] variant_id and display_name_snapshot are passed to save_order_items
        when _stock_check_items has product_ids."""
        cls = self._make_fake_classification(
            situation="new_order",
            order_id="ORD-VF1",
            order_items=[
                types.SimpleNamespace(product_name="Silver EU", base_flavor="Silver", quantity=3),
                types.SimpleNamespace(product_name="Bronze EU", base_flavor="Bronze", quantity=1),
            ],
        )
        result = self._make_persist_result(
            _stock_check_items=[
                {
                    "product_name": "Silver EU",
                    "base_flavor": "Silver",
                    "quantity": 3,
                    "product_ids": [10],
                    "display_name": "Terea Silver EU",
                },
                {
                    "product_name": "Bronze EU",
                    "base_flavor": "Bronze",
                    "quantity": 1,
                    "product_ids": [52, 53],  # ambiguous -> variant_id=None
                    "display_name": "Terea Bronze",
                },
            ],
        )

        with patch.object(self.agents_pipeline, "save_order_items", return_value=2) as mock_save:
            self.agents_pipeline._persist_results(
                cls, result, "thread_vf", "msg_vf", "email text",
            )

        mock_save.assert_called_once()
        items = mock_save.call_args.kwargs.get(
            "order_items",
            mock_save.call_args[0][2] if len(mock_save.call_args[0]) > 2 else None,
        )
        # First item: single product_id → variant_id=10
        self.assertEqual(items[0]["variant_id"], 10)
        self.assertEqual(items[0]["display_name_snapshot"], "Terea Silver EU")
        # Second item: ambiguous → variant_id=None
        self.assertIsNone(items[1]["variant_id"])
        self.assertEqual(items[1]["display_name_snapshot"], "Terea Bronze")

    def test_new_order_save_fallback_when_stock_check_missing(self):
        """[P2.c] When _stock_check_items is absent, save uses raw classifier
        fields with no variant_id."""
        cls = self._make_fake_classification(
            situation="new_order",
            order_id="ORD-FB1",
            order_items=[
                types.SimpleNamespace(product_name="Tera Green Raw", base_flavor="Green", quantity=2),
            ],
        )
        result = self._make_persist_result()
        # No _stock_check_items in result

        with patch.object(self.agents_pipeline, "save_order_items", return_value=1) as mock_save:
            self.agents_pipeline._persist_results(
                cls, result, "thread_fb", "msg_fb", "email text",
            )

        mock_save.assert_called_once()
        items = mock_save.call_args.kwargs.get(
            "order_items",
            mock_save.call_args[0][2] if len(mock_save.call_args[0]) > 2 else None,
        )
        saved_item = items[0]
        # Falls back to raw classifier values
        self.assertEqual(saved_item["product_name"], "Tera Green Raw")
        self.assertEqual(saved_item["base_flavor"], "Green")
        self.assertEqual(saved_item["quantity"], 2)
        # No variant data
        self.assertNotIn("variant_id", saved_item)
        self.assertNotIn("display_name_snapshot", saved_item)


    def test_new_order_ambiguous_sets_fulfillment_blocked(self):
        """[P3] Ambiguous items in _stock_check_items → fulfillment_blocked + ambiguous_flavors."""
        email = (
            "From: client1@example.com\n"
            "Subject: New Order\n"
            "Body: I want 3 Silver and 2 Bronze EU"
        )

        def fake_classify(text, ctx):
            return types.SimpleNamespace(
                client_email="client1@example.com",
                client_name="Test Client One",
                situation="new_order",
                needs_reply=True,
                order_id="ORD-AMB1",
                price="$550",
                order_items=[
                    types.SimpleNamespace(product_name="Silver", base_flavor="Silver", quantity=3),
                    types.SimpleNamespace(product_name="Bronze EU", base_flavor="Bronze", quantity=2),
                ],
                dialog_intent=None,
                followup_to=None,
                customer_street=None,
                customer_city_state_zip=None,
                parser_used=False,
                is_followup=False,
            )

        with (
            patch.object(self.agents_classifier, "run_classification", side_effect=fake_classify),
            patch.object(self.agents_pipeline, "get_stock_summary", return_value={"total": 10}),
            patch.object(
                self.agents_pipeline,
                "resolve_order_items",
                return_value=(
                    [
                        {"product_name": "Silver", "base_flavor": "Silver", "quantity": 3,
                         "product_ids": [10, 30, 54]},  # AMBIGUOUS
                        {"product_name": "Bronze EU", "base_flavor": "Bronze", "quantity": 2,
                         "product_ids": [52]},  # single
                    ],
                    [],
                ),
            ),
            patch.object(
                self.agents_pipeline,
                "check_stock_for_order",
                return_value={
                    "all_in_stock": True,
                    "items": [
                        {"base_flavor": "Silver", "ordered_qty": 3, "total_available": 10,
                         "is_sufficient": True, "stock_entries": []},
                        {"base_flavor": "Bronze", "ordered_qty": 2, "total_available": 5,
                         "is_sufficient": True, "stock_entries": [], "display_name": "Terea Bronze EU"},
                    ],
                    "insufficient_items": [],
                },
            ),
            patch.object(self.agents_pipeline, "calculate_order_price", return_value=550.0),
        ):
            result = self.agents_pipeline.process_classified_email(fake_classify("", ""))

        # Ambiguity gate must fire
        self.assertTrue(result.get("fulfillment_blocked"))
        self.assertIn("Silver", result.get("ambiguous_flavors", []))
        # Bronze is NOT ambiguous
        self.assertNotIn("Bronze", result.get("ambiguous_flavors", []))


if __name__ == "__main__":
    unittest.main()
