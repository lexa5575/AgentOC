"""Tests for template-based handlers (tracking, payment, discount, shipping).

Covers:
- Template selection and placeholder filling
- Mixed-intent / keyword guards → general fallback
- Policy compliance (templates bypass checker)
- fill_template_reply extensions (any fallback, override, guards)
- _calc_recheck_date logic
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Module stubs
# ---------------------------------------------------------------------------
def _install_stubs():
    for name in list(sys.modules):
        if name.startswith("agents.handlers") and name != "agents.handlers":
            sys.modules.pop(name, None)
    if "agents" in sys.modules and hasattr(sys.modules["agents"], "handlers"):
        delattr(sys.modules["agents"], "handlers")

    # agno stubs
    if "agno" not in sys.modules:
        agno = types.ModuleType("agno")
        agno.__path__ = []
        sys.modules["agno"] = agno
    if "agno.agent" not in sys.modules:
        m = types.ModuleType("agno.agent")

        class FakeAgent:
            def __init__(self, *a, **kw):
                pass
            def run(self, prompt):
                resp = MagicMock()
                resp.content = "LLM fallback reply. Thank you!"
                return resp

        m.Agent = FakeAgent
        sys.modules["agno.agent"] = m
    if "agno.models" not in sys.modules:
        m = types.ModuleType("agno.models")
        m.__path__ = []
        sys.modules["agno.models"] = m
    if "agno.models.openai" not in sys.modules:
        m = types.ModuleType("agno.models.openai")
        m.OpenAIResponses = lambda *a, **kw: None
        sys.modules["agno.models.openai"] = m

    # DB stubs
    for mod_name in [
        "db", "db.models", "db.memory", "db.conversation_state",
        "db.catalog", "db.region_family", "db.fulfillment",
        "db.region_preference", "db.stock", "db.email_history",
        "db.shipping",
    ]:
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            if mod_name == "db":
                m.__path__ = []
            sys.modules[mod_name] = m

    db_memory = sys.modules["db.memory"]
    db_memory.decrement_discount = MagicMock()
    db_memory.save_email = MagicMock()
    db_memory.get_full_thread_history = MagicMock(return_value=[])
    db_memory.get_full_email_history = MagicMock(return_value=[])
    db_memory.get_client = MagicMock(return_value=None)
    db_memory.get_stock_summary = MagicMock(return_value="")

    db_eh = sys.modules["db.email_history"]
    db_eh.get_full_thread_history = MagicMock(return_value=[])

    db_cs = sys.modules["db.conversation_state"]
    db_cs.get_state = lambda *a, **kw: None
    db_cs.save_state = MagicMock()
    db_cs.get_client_states = lambda *a, **kw: []

    db_catalog = sys.modules["db.catalog"]
    db_catalog.get_display_name = lambda name, cat="": name
    db_catalog.get_base_display_name = lambda name: name
    db_catalog._enrich_display_name_with_region = lambda *args: args[-1] if args else ""

    db_region = sys.modules["db.region_family"]
    db_region.CATEGORY_REGION_SUFFIX = {}
    db_region.is_same_family = lambda a, b: False

    db_stock = sys.modules["db.stock"]
    db_stock.extract_variant_id = lambda *a, **kw: None

    # tools stubs
    for mod_name in ["tools", "tools.email_parser", "tools.web_search"]:
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

    tools_ws = sys.modules["tools.web_search"]
    tools_ws.get_search_tools = lambda: []

    # utils stubs
    for mod_name in ["utils", "utils.gmail", "utils.telegram"]:
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            if mod_name == "utils":
                m.__path__ = []
            sys.modules[mod_name] = m

    sys.modules["utils.gmail"].create_draft = MagicMock()
    sys.modules["utils.gmail"].get_full_thread_history = MagicMock(return_value=[])
    sys.modules["utils.telegram"].send_telegram = MagicMock()
    sys.modules["utils.telegram"].send_telegram_async = MagicMock()


_install_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeCls:
    """Minimal fake classification."""
    def __init__(self, **kw):
        defaults = {
            "situation": "tracking", "client_email": "test@example.com",
            "client_name": "Test", "needs_reply": True, "order_id": None,
            "price": None, "order_items": [], "dialog_intent": None,
            "followup_to": None, "customer_street": None,
            "customer_city_state_zip": None, "parser_used": False,
        }
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _base_result(**overrides):
    r = {
        "needs_reply": True, "situation": "tracking",
        "client_email": "test@example.com", "client_name": "Test",
        "client_found": True,
        "client_data": {
            "name": "Test", "payment_type": "postpay",
            "zelle_address": "pay@example.com",
            "discount_percent": 0, "discount_orders_left": 0,
            "street": "", "city_state_zip": "",
        },
        "template_used": False, "draft_reply": None,
        "needs_routing": True, "stock_issue": None,
        "conversation_state": {"status": "new", "facts": {}},
        "gmail_thread_id": "thread-1",
    }
    r.update(overrides)
    return r


# ═══════════════════════════════════════════════════════════════════════════
# Tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestTrackingTemplate(unittest.TestCase):

    def test_tracking_with_tracking_number(self):
        from agents.handlers.tracking import handle_tracking

        result = _base_result(
            conversation_state={"status": "shipped", "facts": {"tracking_number": "940011111"}},
        )
        out = handle_tracking(_FakeCls(), result, "Body: where is my order?")
        self.assertIn("940011111", out["draft_reply"])
        self.assertIn("usps.com", out["draft_reply"])
        self.assertTrue(out["template_used"])

    def test_tracking_not_shipped_goes_to_general(self):
        from agents.handlers.tracking import handle_tracking

        result = _base_result(
            conversation_state={"status": "new", "facts": {}},
        )
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            out = handle_tracking(_FakeCls(), result, "Body: where is my order?")
            mock.assert_called_once()

    def test_tracking_shipped_pending_template(self):
        """Shipped + no tracking + valid recheck date → pending template."""
        from agents.handlers.tracking import handle_tracking

        result = _base_result(
            conversation_state={"status": "shipped", "facts": {"shipped_at": "2026-03-10T12:00:00"}},
        )
        out = handle_tracking(_FakeCls(), result, "Body: where is my order?")
        self.assertTrue(out["template_used"])
        self.assertIn("shipped your order 100%", out["draft_reply"])
        self.assertIn("Thank you!", out["draft_reply"])

    def test_tracking_shipped_no_recheck_goes_to_general(self):
        """Shipped but _calc_recheck_date returns None → general."""
        from agents.handlers.tracking import handle_tracking

        result = _base_result(
            conversation_state={"status": "shipped", "facts": {}},
        )
        with patch("agents.handlers.template_utils._calc_recheck_date", return_value=None), \
             patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_tracking(_FakeCls(), result, "Body: tracking?")
            mock.assert_called_once()


class TestRecheckDate(unittest.TestCase):

    def test_recheck_skips_weekends(self):
        from agents.handlers.template_utils import _calc_recheck_date

        # Friday March 13 2026 + 5 business days = Friday March 20
        with patch("db.email_history.get_full_thread_history", return_value=[
            {"direction": "outbound", "situation": "new_order",
             "created_at": datetime(2026, 3, 13, 10, 0)}
        ]):
            result = _calc_recheck_date("thread-1", facts={}, payment_type="postpay")
        self.assertIsNotNone(result)
        self.assertIn("March 20", result)

    def test_recheck_none_when_no_ship_date(self):
        from agents.handlers.template_utils import _calc_recheck_date

        with patch("db.email_history.get_full_thread_history", return_value=[]):
            result = _calc_recheck_date("thread-1", facts={}, payment_type="postpay")
        self.assertIsNone(result)

    def test_recheck_uses_shipped_at(self):
        from agents.handlers.template_utils import _calc_recheck_date

        result = _calc_recheck_date(
            None,
            facts={"shipped_at": "2026-03-10T12:00:00"},
            payment_type="postpay",
        )
        self.assertIsNotNone(result)
        self.assertIn("March 17", result)


# ═══════════════════════════════════════════════════════════════════════════
# Payment
# ═══════════════════════════════════════════════════════════════════════════

class TestPaymentTemplate(unittest.TestCase):

    def test_prepay_simple_question_shows_zelle(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        result["client_data"]["payment_type"] = "prepay"
        out = handle_payment(_FakeCls(), result, "Body: How can I pay?")
        self.assertTrue(out["template_used"])
        self.assertIn("pay@example.com", out["draft_reply"])

    def test_postpay_simple_question(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        out = handle_payment(_FakeCls(), result, "Body: Where do I send payment?")
        self.assertTrue(out["template_used"])
        self.assertIn("Pay when received", out["draft_reply"])

    def test_complex_question_goes_to_general(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_payment(_FakeCls(), result, "Body: old email doesn't work on Zelle. Which one should I use?")
            mock.assert_called_once()

    def test_mixed_intent_goes_to_general(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_payment(_FakeCls(), result, "Body: same zelle account as before and what's the total?")
            mock.assert_called_once()

    def test_curly_apostrophe_normalized(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        # \u2019 = curly apostrophe
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_payment(_FakeCls(), result, "Body: Can I use Zelle, the old email doesn\u2019t work")
            mock.assert_called_once()

    def test_cash_app_goes_to_general(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_payment(_FakeCls(), result, "Body: Can I pay with cash app?")
            mock.assert_called_once()

    def test_empty_zelle_falls_to_general(self):
        from agents.handlers.payment import handle_payment

        result = _base_result()
        result["client_data"]["zelle_address"] = ""
        result["client_data"]["payment_type"] = "prepay"
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_payment(_FakeCls(), result, "Body: How can I pay?")
            mock.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Discount
# ═══════════════════════════════════════════════════════════════════════════

class TestDiscountTemplate(unittest.TestCase):

    def test_has_discount_shows_percent(self):
        from agents.handlers.discount import handle_discount

        result = _base_result()
        result["client_data"]["discount_percent"] = 5
        result["client_data"]["discount_orders_left"] = 3
        out = handle_discount(_FakeCls(situation="discount_request"), result, "Body: Any discount?")
        self.assertTrue(out["template_used"])
        self.assertIn("5%", out["draft_reply"])
        self.assertIn("3", out["draft_reply"])

    def test_no_discount_polite_decline(self):
        from agents.handlers.discount import handle_discount

        result = _base_result()
        out = handle_discount(_FakeCls(situation="discount_request"), result, "Body: Any discount?")
        self.assertTrue(out["template_used"])
        self.assertIn("don't have any active discounts", out["draft_reply"])

    def test_expired_discount(self):
        from agents.handlers.discount import handle_discount

        result = _base_result()
        result["client_data"]["discount_percent"] = 5
        result["client_data"]["discount_orders_left"] = 0  # expired
        out = handle_discount(_FakeCls(situation="discount_request"), result, "Body: discount?")
        self.assertIn("don't have any active discounts", out["draft_reply"])

    def test_bulk_goes_to_general(self):
        from agents.handlers.discount import handle_discount

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_discount(_FakeCls(situation="discount_request"), result, "Body: Can I get a bulk discount?")
            mock.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Shipping
# ═══════════════════════════════════════════════════════════════════════════

class TestShippingTemplate(unittest.TestCase):

    def test_prepay_standard(self):
        from agents.handlers.shipping import handle_shipping

        result = _base_result()
        result["client_data"]["payment_type"] = "prepay"
        out = handle_shipping(_FakeCls(situation="shipping_timeline"), result, "Body: How long does shipping take?")
        self.assertTrue(out["template_used"])
        self.assertIn("3 PM EST", out["draft_reply"])
        self.assertIn("2-4 business days", out["draft_reply"])

    def test_postpay_standard(self):
        from agents.handlers.shipping import handle_shipping

        result = _base_result()
        out = handle_shipping(_FakeCls(situation="shipping_timeline"), result, "Body: When will you ship?")
        self.assertTrue(out["template_used"])
        self.assertIn("ASAP", out["draft_reply"])

    def test_expedited_goes_to_general(self):
        from agents.handlers.shipping import handle_shipping

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_shipping(_FakeCls(situation="shipping_timeline"), result, "Body: Do you offer expedited shipping?")
            mock.assert_called_once()

    def test_by_friday_goes_to_general(self):
        from agents.handlers.shipping import handle_shipping

        result = _base_result()
        with patch("agents.handlers.general.handle_general", return_value={"draft_reply": "LLM"}) as mock:
            handle_shipping(_FakeCls(situation="shipping_timeline"), result, "Body: I need it by Friday please")
            mock.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# fill_template_reply extensions
# ═══════════════════════════════════════════════════════════════════════════

class TestFillTemplateReplyExtensions(unittest.TestCase):

    def test_any_fallback_works(self):
        from agents.handlers.template_utils import fill_template_reply

        result = _base_result()
        result["conversation_state"] = {"status": "shipped", "facts": {"shipped_at": "2026-03-10T12:00:00"}}
        _, found = fill_template_reply(_FakeCls(), result, "tracking")
        self.assertTrue(found)

    def test_override_payment_type(self):
        from agents.handlers.template_utils import fill_template_reply

        result = _base_result()
        result["client_data"]["discount_percent"] = 10
        result["client_data"]["discount_orders_left"] = 2
        _, found = fill_template_reply(
            _FakeCls(situation="discount_request"), result,
            "discount_request", override_payment_type="has_discount",
        )
        self.assertTrue(found)
        self.assertIn("10%", result["draft_reply"])

    def test_zelle_guard_skips(self):
        from agents.handlers.template_utils import fill_template_reply

        result = _base_result()
        result["client_data"]["zelle_address"] = ""
        result["client_data"]["payment_type"] = "prepay"
        _, found = fill_template_reply(_FakeCls(), result, "payment_question")
        self.assertFalse(found)

    def test_final_price_guard_skips(self):
        """Template with {FINAL_PRICE} but no price → skip."""
        from agents.handlers.template_utils import fill_template_reply
        from agents.reply_templates import REPLY_TEMPLATES

        # Temporarily add a template that requires FINAL_PRICE
        key = ("_test_final_price", "prepay")
        REPLY_TEMPLATES[key] = "Total: {FINAL_PRICE}. Thank you!"
        try:
            result = _base_result()
            result["client_data"]["payment_type"] = "prepay"
            _, found = fill_template_reply(
                _FakeCls(price=None, parser_used=False), result, "_test_final_price",
            )
            self.assertFalse(found)
        finally:
            del REPLY_TEMPLATES[key]

    def test_recheck_date_guard_skips(self):
        from agents.handlers.template_utils import fill_template_reply

        result = _base_result()
        result["conversation_state"] = {"status": "shipped", "facts": {}}
        with patch("agents.handlers.template_utils._calc_recheck_date", return_value=None):
            _, found = fill_template_reply(_FakeCls(), result, "tracking")
        self.assertFalse(found)


# ═══════════════════════════════════════════════════════════════════════════
# Policy compliance (templates bypass checker)
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyCompliance(unittest.TestCase):

    def test_discount_no_discount_mentions_promotions(self):
        from agents.reply_templates import REPLY_TEMPLATES
        template = REPLY_TEMPLATES[("discount_request", "no_discount")]
        self.assertIn("promotions", template.lower())

    def test_discount_no_discount_no_promises(self):
        from agents.reply_templates import REPLY_TEMPLATES
        template = REPLY_TEMPLATES[("discount_request", "no_discount")]
        self.assertNotIn("will get", template.lower())
        self.assertNotIn("guarantee", template.lower())

    def test_shipping_no_expedited_promise(self):
        from agents.reply_templates import REPLY_TEMPLATES
        for key in [("shipping_timeline", "prepay"), ("shipping_timeline", "postpay")]:
            template = REPLY_TEMPLATES[key]
            self.assertNotIn("expedited", template.lower())
            self.assertNotIn("express", template.lower())

    def test_payment_no_invented_zelle(self):
        from agents.reply_templates import REPLY_TEMPLATES
        for key in [("payment_question", "prepay"), ("payment_question", "postpay")]:
            template = REPLY_TEMPLATES[key]
            self.assertIn("{ZELLE_ADDRESS}", template)
            # No hardcoded email addresses
            self.assertNotIn("@gmail.com", template)

    def test_tracking_no_fake_number(self):
        from agents.reply_templates import REPLY_TEMPLATES
        template = REPLY_TEMPLATES[("tracking", "any")]
        self.assertNotIn("9400", template)


if __name__ == "__main__":
    unittest.main()
