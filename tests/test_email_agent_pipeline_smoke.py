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
    db_clients = types.ModuleType("db.clients")
    db_clients.get_client_profile = lambda *args, **kwargs: None
    db_clients.update_client_summary = lambda *args, **kwargs: True
    db_conversation_state = types.ModuleType("db.conversation_state")
    db_conversation_state.get_state = lambda *args, **kwargs: None
    db_conversation_state.save_state = lambda *args, **kwargs: None
    db_conversation_state.get_client_states = lambda *args, **kwargs: []

    sys.modules["db"] = db_mod
    sys.modules["db.memory"] = db_memory
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
        _install_import_stubs()
        cls.email_agent = importlib.import_module("agents.email_agent")
        cls.reply_templates = importlib.import_module("agents.reply_templates")
        cls.agents_pipeline = importlib.import_module("agents.pipeline")
        cls.agents_notifier = importlib.import_module("agents.notifier")
        cls.checker = importlib.import_module("agents.checker")
        cls.h_general = importlib.import_module("agents.handlers.general")
        cls.h_tracking = importlib.import_module("agents.handlers.tracking")
        cls.h_payment = importlib.import_module("agents.handlers.payment")
        cls.h_discount = importlib.import_module("agents.handlers.discount")
        cls.h_shipping = importlib.import_module("agents.handlers.shipping")
        cls.h_oos_followup = importlib.import_module("agents.handlers.oos_followup")

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
            patch.object(self.email_agent.classifier_agent, "run", side_effect=self._classifier_run),
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
                "order_items": None,
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
        self.assertIn("Unfortunately, we just ran out of Turquoise", out)
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

        with patch.object(self.email_agent.classifier_agent, "run", side_effect=_classify_paid):
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
        email = (
            "From: client1@example.com\n"
            "Subject: Re: Your order\n"
            "Body: Yes I'll take the green instead"
        )
        out = self.email_agent.classify_and_process(email)

        self.assertIn("Situation: oos_followup", out)
        # agrees_to_alternative + prepay + zelle → template, not LLM
        self.assertIn("[Template - exact copy]", out)
        self.assertIn("We will update your order with the alternative.", out)
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


if __name__ == "__main__":
    unittest.main()
