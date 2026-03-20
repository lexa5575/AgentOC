"""Unit tests for tools/email_parser.py — order parsing + body cleaning.

Tests that:
- Website order notifications are parsed deterministically (0 LLM tokens)
- Customer replies with quoted order text are NOT parsed (fall to LLM)
- Email body cleaning removes quotes, signatures, whitespace
- base_flavor extraction handles prefixes/suffixes correctly
"""

from __future__ import annotations

import sys
import types
import unittest


def _install_stubs() -> None:
    """Install minimal stubs so email_parser can import reply_templates."""
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
        agno_models_openai.OpenAIResponses = type(
            "OpenAIResponses", (), {"__init__": lambda *a, **kw: None}
        )
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
        db_memory.check_stock_for_order = lambda *a, **kw: {
            "all_in_stock": True,
            "items": [],
            "insufficient_items": [],
        }
        db_memory.calculate_order_price = lambda *a, **kw: None
        db_memory.resolve_order_items = lambda items, **kw: (items, [])
        db_memory.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
        db_memory.get_full_email_history = lambda *a, **kw: []
        db_memory.get_full_thread_history = lambda *a, **kw: []
        db_memory.save_email = lambda *a, **kw: None
        db_memory.save_order_items = lambda *a, **kw: None
        db_memory.update_client = lambda *a, **kw: None
        sys.modules["db.memory"] = db_memory


class TestTryParseOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._modules_snapshot = dict(sys.modules)
        _install_stubs()
        from tools.email_parser import try_parse_order, _extract_base_flavor

        cls.try_parse_order = staticmethod(try_parse_order)
        cls._extract_base_flavor = staticmethod(_extract_base_flavor)

    @classmethod
    def tearDownClass(cls):
        added = set(sys.modules) - set(cls._modules_snapshot)
        for name in added:
            sys.modules.pop(name, None)
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def test_clean_website_order(self):
        """Standard website order notification → parsed correctly."""
        email = (
            "From: Shipmecarton <order@shipmecarton.com>\n"
            "Reply-To: client@example.com\n"
            "Subject: Shipmecarton - Order 23573\n"
            "Body: \n"
            "Order ID: 23573\n"
            "Payment amount: $770.00\n"
            "Firstname: Test User\n"
            "Email: client@example.com\n"
            "Street address1: 212 Main Rd\n"
            "Town/City: El Paso\n"
            "State: Texas\n"
            "Postcode/Zip: 79912\n"
            "1 Tera Amber made in Middle East $110.00 2 $220.00\n"
            "2 Tera Bronze made in Middle East $110.00 5 $550.00"
        )
        result = self.try_parse_order(email)

        self.assertIsNotNone(result)
        self.assertEqual(result.situation, "new_order")
        self.assertEqual(result.client_email, "client@example.com")
        self.assertEqual(result.order_id, "23573")
        self.assertEqual(result.price, "$770.00")
        self.assertEqual(result.customer_street, "212 Main Rd")
        self.assertEqual(result.customer_city_state_zip, "El Paso, Texas 79912")
        self.assertTrue(result.needs_reply)

        # Order items
        self.assertEqual(len(result.order_items), 2)
        self.assertEqual(result.order_items[0].product_name, "Tera Amber made in Middle East")
        self.assertEqual(result.order_items[0].base_flavor, "Amber")
        self.assertEqual(result.order_items[0].quantity, 2)
        self.assertEqual(result.order_items[1].product_name, "Tera Bronze made in Middle East")
        self.assertEqual(result.order_items[1].base_flavor, "Bronze")
        self.assertEqual(result.order_items[1].quantity, 5)

        # Items text
        self.assertIn("Tera Amber", result.items)
        self.assertIn("Tera Bronze", result.items)

    def test_messy_html_order(self):
        """HTML-converted order with tabs and extra spaces → parsed."""
        email = (
            "From: Shipmecarton <order@shipmecarton.com>\n"
            "Reply-To: client@example.com\n"
            "Subject: Shipmecarton - Order 99999\n"
            "Body: \n"
            "Order ID:\t99999\n"
            "Payment amount:\t$330.00\n"
            "Firstname:\tJane Doe\n"
            "Email:\tclient@example.com\n"
            "Street address1:\t456 Oak Ave\n"
            "Town/City:\tChicago\n"
            "State:\tIllinois\n"
            "Postcode/Zip:\t60601\n"
            "\t\t\t1\t\tTera Green made in Middle East\t\t$110.00\t\t3\t\t$330.00"
        )
        result = self.try_parse_order(email)

        self.assertIsNotNone(result)
        self.assertEqual(result.order_id, "99999")
        self.assertEqual(result.price, "$330.00")
        self.assertEqual(result.client_email, "client@example.com")
        self.assertEqual(len(result.order_items), 1)
        self.assertEqual(result.order_items[0].base_flavor, "Green")
        self.assertEqual(result.order_items[0].quantity, 3)

    def test_customer_reply_not_parsed(self):
        """Regular customer email without order markers → None."""
        email = (
            "From: client@example.com\n"
            "Subject: Re: Order\n"
            "Body: Good evening, is bronze available?\n"
            "Thank you, Melissa"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_reply_with_quoted_order_not_parsed(self):
        """CRITICAL: Customer reply that quotes an order notification → None.

        The Order ID and Payment amount are in the quoted part,
        not in the customer's own message.
        """
        email = (
            "From: client@example.com\n"
            "Subject: Re: Shipmecarton - Order 23573\n"
            "Body: Yes, please ship it!\n"
            "\n"
            "On Mon, Mar 3, 2026 at 10:00 AM Shipmecarton wrote:\n"
            "> Order ID: 23573\n"
            "> Payment amount: $770.00\n"
            "> Firstname: Test User\n"
            "> Email: client@example.com\n"
            "> 1 Tera Amber $110.00 2 $220.00"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_reply_with_inline_quoted_order_not_parsed(self):
        """Customer reply with unformatted quoted order → None.

        Quoted text without > prefix but after 'On ... wrote:'.
        """
        email = (
            "From: client@example.com\n"
            "Subject: Re: Order 12345\n"
            "Body: Please replace with purple, thank you!\n"
            "\n"
            "On Feb 28, 2026 James wrote:\n"
            "Order ID: 12345\n"
            "Payment amount: $220.00\n"
            "Email: client@example.com\n"
            "1 Tera Green EU $110.00 2 $220.00"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_missing_order_id_not_parsed(self):
        """Payment amount without Order ID → None."""
        email = (
            "From: order@shipmecarton.com\n"
            "Body: Payment amount: $100.00\n"
            "Some other content"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_missing_email_not_parsed(self):
        """Order ID + Payment amount but no Email field → None (conservative)."""
        email = (
            "From: unknown@somewhere.com\n"
            "Body: Order ID: 55555\n"
            "Payment amount: $200.00\n"
            "1 Tera Silver $110.00 2 $220.00"
        )
        # No Email: field, no Reply-To → can't determine client → None
        self.assertIsNone(self.try_parse_order(email))

    def test_missing_items_not_parsed(self):
        """Order ID + Email but no parseable product lines → None (conservative)."""
        email = (
            "From: order@shipmecarton.com\n"
            "Reply-To: client@example.com\n"
            "Body: Order ID: 55555\n"
            "Payment amount: $200.00\n"
            "Email: client@example.com\n"
            "No product table here"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_manual_order_like_message_not_parsed(self):
        """Customer manually typing order-like markers → None (no Shipmecarton header)."""
        email = (
            "From: client@example.com\n"
            "Subject: My order details\n"
            "Body: \n"
            "Order ID: 99999\n"
            "Payment amount: $330.00\n"
            "Email: client@example.com\n"
            "1 Tera Green EU $110.00 3 $330.00"
        )
        self.assertIsNone(self.try_parse_order(email))

    def test_email_from_reply_to_header(self):
        """Client email extracted from Reply-To when no Email: field in body."""
        email = (
            "From: Shipmecarton <order@shipmecarton.com>\n"
            "Reply-To: buyer@example.com\n"
            "Subject: Shipmecarton - Order 11111\n"
            "Body: \n"
            "Order ID: 11111\n"
            "Payment amount: $110.00\n"
            "Firstname: Buyer\n"
            "1 Tera Silver $110.00 1 $110.00"
        )
        result = self.try_parse_order(email)

        self.assertIsNotNone(result)
        self.assertEqual(result.client_email, "buyer@example.com")

    def test_customer_reply_with_unquoted_order_template_not_parsed(self):
        """Customer replies 'Thank you' with inline (unquoted) order template → None.

        Subject has 'Shipmecarton' but From is the customer, not Shipmecarton.
        Parser must NOT trigger — only From: header matters.
        """
        email = (
            "From: client@example.com\n"
            "Subject: Re: Shipmecarton - Order 23573\n"
            "Body: Thank you James.\n"
            "\n"
            "Order ID: 23573\n"
            "Payment amount: $770.00\n"
            "Firstname: John Smith\n"
            "Email: client@example.com\n"
            "1 Tera Amber made in Middle East $110.00 2 $220.00"
        )
        self.assertIsNone(self.try_parse_order(email))

    # --- base_flavor extraction ---

    def test_base_flavor_tera_green_middle_east(self):
        self.assertEqual(self._extract_base_flavor("Tera Green made in Middle East"), "Green")

    def test_base_flavor_tera_turquoise_eu(self):
        self.assertEqual(self._extract_base_flavor("Tera Turquoise EU"), "Turquoise")

    def test_base_flavor_tera_silver(self):
        self.assertEqual(self._extract_base_flavor("Tera Silver"), "Silver")

    def test_base_flavor_one_green(self):
        self.assertEqual(self._extract_base_flavor("ONE Green"), "ONE Green")

    def test_base_flavor_prime_black(self):
        self.assertEqual(self._extract_base_flavor("PRIME Black"), "PRIME Black")

    def test_base_flavor_case_insensitive_suffix(self):
        """'Made in Middle East' with capital M → stripped."""
        self.assertEqual(self._extract_base_flavor("Tera Amber Made in Middle East"), "Amber")

    def test_base_flavor_terea_prefix(self):
        self.assertEqual(self._extract_base_flavor("Terea Purple EU"), "Purple")

    def test_base_flavor_made_in_europe(self):
        """'Tera AMBER made in Europe' → 'AMBER' (new suffix)."""
        self.assertEqual(self._extract_base_flavor("Tera AMBER made in Europe"), "AMBER")

    def test_base_flavor_made_in_europe_case_insensitive(self):
        self.assertEqual(self._extract_base_flavor("Tera Silver Made in Europe"), "Silver")


class TestCleanEmailBody(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._modules_snapshot = dict(sys.modules)
        _install_stubs()
        from tools.email_parser import clean_email_body

        cls.clean_email_body = staticmethod(clean_email_body)

    @classmethod
    def tearDownClass(cls):
        added = set(sys.modules) - set(cls._modules_snapshot)
        for name in added:
            sys.modules.pop(name, None)
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def test_strips_quoted_block(self):
        """'On ... wrote:' and everything after removed."""
        email = (
            "From: a@b.com\n"
            "Body: Yes please\n"
            "\n"
            "On Jan 1, 2026 John wrote:\n"
            "> old text\n"
            "> more old text"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("Yes please", cleaned)
        self.assertNotIn("old text", cleaned)

    def test_strips_iphone_signature(self):
        """'Sent from my iPhone' removed."""
        email = "From: a@b.com\nBody: Yes\n\nSent from my iPhone"
        cleaned = self.clean_email_body(email)
        self.assertIn("Yes", cleaned)
        self.assertNotIn("iPhone", cleaned)

    def test_preserves_headers(self):
        """Headers unchanged, only body cleaned."""
        email = (
            "From: a@b.com\n"
            "Reply-To: c@d.com\n"
            "Subject: Test\n"
            "Body: Hello\n"
            "\n"
            "Sent from my iPhone"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("From: a@b.com", cleaned)
        self.assertIn("Reply-To: c@d.com", cleaned)
        self.assertIn("Subject: Test", cleaned)
        self.assertIn("Hello", cleaned)

    def test_no_body_marker(self):
        """Email without 'Body:' returned as-is."""
        email = "From: a@b.com\nSubject: Test\nHello"
        self.assertEqual(self.clean_email_body(email), email)

    def test_strips_gt_quoted_lines(self):
        """Lines starting with '>' removed."""
        email = (
            "From: a@b.com\n"
            "Body: My reply here\n"
            "> previous message line 1\n"
            "> previous message line 2"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("My reply here", cleaned)
        self.assertNotIn("previous message", cleaned)

    def test_strips_inline_iphone_signature(self):
        """'Sent from my iPhone' without preceding newline → stripped."""
        email = "From: a@b.com\nBody: Great steakSent from my iPhone"
        cleaned = self.clean_email_body(email)
        self.assertIn("Great steak", cleaned)
        self.assertNotIn("iPhone", cleaned)

    def test_strips_wrapped_on_wrote(self):
        """'On ... wrote:' with line wrap before 'wrote:' → stripped."""
        email = (
            "From: a@b.com\n"
            "Body: Hey. I have paid. Thanks!\r\n"
            "\r\n"
            "On Fri, Feb 27, 2026 at 7:29\u202fAM James Harris <j@example.com>\r\n"
            "wrote:\r\n"
            "\r\n"
            "> Hello.\r\n"
            "> How are you?"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("I have paid", cleaned)
        self.assertNotIn("Hello", cleaned)
        self.assertNotIn("wrote", cleaned)

    def test_strips_inline_on_wrote_no_trailing_newline(self):
        """'On ... wrote:' at end of body (no trailing newline) → stripped."""
        email = (
            "From: a@b.com\n"
            "Body: Yes please\n"
            "On Jan 1, 2026 John wrote:"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("Yes please", cleaned)
        self.assertNotIn("John wrote", cleaned)

    def test_strips_russian_iphone_signature(self):
        """Russian 'Отправлено с iPhone' → stripped."""
        email = (
            "From: a@b.com\n"
            "Body: Can you send silver from Middle East please?\r\n"
            "Отправлено с iPhone"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("silver from Middle East", cleaned)
        self.assertNotIn("Отправлено", cleaned)
        self.assertNotIn("iPhone", cleaned)

    def test_strips_proton_mail_signature(self):
        """'Sent from Proton Mail' → stripped."""
        email = "From: a@b.com\nBody: Yes, please ship it\n\nSent from Proton Mail"
        cleaned = self.clean_email_body(email)
        self.assertIn("Yes, please ship it", cleaned)
        self.assertNotIn("Proton", cleaned)

    def test_strips_proton_mail_sent_with(self):
        """'Sent with Proton Mail' (alternative wording) → stripped."""
        email = "From: a@b.com\nBody: Sounds good\n\nSent with Proton Mail secure email"
        cleaned = self.clean_email_body(email)
        self.assertIn("Sounds good", cleaned)
        self.assertNotIn("Proton", cleaned)

    def test_strips_proton_mail_markdown_and_original_message(self):
        """Real Proton Mail format: markdown link + '-------- Original Message --------'."""
        email = (
            "From: a@b.com\n"
            "Body: That will be fine. Thank you\n"
            "\n"
            "Sent from [Proton Mail](https://proton.me/mail/home) for Android.\n"
            "\n"
            "-------- Original Message --------\n"
            "On Monday, 03/02/26 at 12:00 James wrote:\n"
            "\n"
            "> Hi!\n"
            "> Unfortunately, we just ran out of Terea Silver EU"
        )
        cleaned = self.clean_email_body(email)
        self.assertIn("That will be fine", cleaned)
        self.assertNotIn("Proton", cleaned)
        self.assertNotIn("Original Message", cleaned)
        self.assertNotIn("ran out", cleaned)

    def test_collapses_excessive_whitespace(self):
        """Multiple blank lines collapsed to double newline."""
        email = "From: a@b.com\nBody: Hello\n\n\n\n\nWorld"
        cleaned = self.clean_email_body(email)
        self.assertIn("Hello", cleaned)
        self.assertIn("World", cleaned)
        # No more than 2 consecutive newlines
        self.assertNotIn("\n\n\n", cleaned)


if __name__ == "__main__":
    unittest.main()
