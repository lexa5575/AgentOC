"""Unit tests for _resolve_fallbacks() in agents.pipeline.

Tests the fallback/substitution resolution logic that handles patterns like
"3 Tropical please. If not, Black is fine too." — where the second item is
a substitute for the first, not an additional order.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Import stubs — minimal set to import agents.pipeline + agents.models
# Follows the same pattern as test_email_agent_pipeline_smoke.py
# ---------------------------------------------------------------------------
def _install_stubs():
    # Evict cached modules
    for name in list(sys.modules):
        if name in (
            "agents.pipeline", "agents.classifier", "agents.models",
            "agents.notifier", "agents.formatters", "agents.router",
            "agents.context", "agents.checker", "agents.state_updater",
        ) or name.startswith("agents.handlers"):
            sys.modules.pop(name, None)

    # agno stubs
    agno = types.ModuleType("agno"); agno.__path__ = []
    agno_agent = types.ModuleType("agno.agent")

    class FakeAgent:
        def __init__(self, *a, **kw): pass
        def run(self, prompt): raise RuntimeError("patched")

    agno_agent.Agent = FakeAgent
    agno_models = types.ModuleType("agno.models"); agno_models.__path__ = []
    agno_models_openai = types.ModuleType("agno.models.openai")
    agno_models_openai.OpenAIResponses = type("Fake", (), {"__init__": lambda *a, **kw: None})
    for n, m in [("agno", agno), ("agno.agent", agno_agent),
                 ("agno.models", agno_models), ("agno.models.openai", agno_models_openai)]:
        sys.modules[n] = m

    # db stubs
    db_mod = types.ModuleType("db"); db_mod.__path__ = []
    db_memory = types.ModuleType("db.memory")
    for fn in ("get_full_email_history", "save_email", "save_order_items",
               "get_client", "decrement_discount", "calculate_order_price",
               "get_full_thread_history", "update_client", "replace_order_items"):
        setattr(db_memory, fn, lambda *a, **kw: None)
    db_memory.check_stock_for_order = lambda *a, **kw: {"all_in_stock": True, "items": [], "insufficient_items": []}
    db_memory.resolve_order_items = lambda items, **kw: (items, [])
    db_memory.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
    db_memory.get_stock_summary = lambda *a, **kw: {"total": 10}

    db_clients = types.ModuleType("db.clients")
    db_clients.get_client_profile = lambda *a, **kw: None
    db_clients.update_client_summary = lambda *a, **kw: True

    db_cs = types.ModuleType("db.conversation_state")
    db_cs.get_state = db_cs.save_state = lambda *a, **kw: None
    db_cs.get_client_states = lambda *a, **kw: []

    db_stock = types.ModuleType("db.stock")
    db_stock.extract_variant_id = db_stock._extract_variant_id = lambda ids, **kw: ids[0] if ids and len(ids) == 1 else None
    db_stock.has_ambiguous_variants = db_stock._has_ambiguous_variants = lambda items, **kw: []
    db_stock.CATEGORY_PRICES = {"TEREA_JAPAN": 115, "TEREA_EUROPE": 110, "KZ_TEREA": 110, "ARMENIA": 110, "УНИКАЛЬНАЯ_ТЕРЕА": 115, "ONE": 99, "STND": 149, "PRIME": 245}
    db_stock.STICK_CATEGORIES = {"KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA", "УНИКАЛЬНАЯ_ТЕРЕА"}
    db_stock.DEVICE_CATEGORIES = {"ONE", "STND", "PRIME"}
    db_stock._REGION_CATEGORY_MAP = {}
    for fn in ("search_stock", "search_stock_by_ids", "get_client_flavor_history"):
        setattr(db_stock, fn, lambda *a, **kw: [])
    db_stock.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
    db_stock.get_product_type = lambda bf: "stick"
    db_stock.resolve_warehouse = lambda text: None
    db_stock.save_order_items = lambda *a, **kw: 0

    db_rp = types.ModuleType("db.region_preference")
    db_rp.apply_region_preference = lambda items: items
    db_rp.apply_thread_hint = lambda items, msgs, cat: items

    db_catalog = types.ModuleType("db.catalog")
    db_catalog._enrich_display_name_with_region = lambda vid, d: d
    db_catalog.get_catalog_products = lambda: []
    db_catalog.get_display_name = lambda name, cat=None: name
    db_catalog.get_base_display_name = lambda name, cat=None: f"Terea {name}"
    db_catalog.get_equivalent_norms = lambda *a, **kw: set()

    db_rf = types.ModuleType("db.region_family")
    db_rf.CATEGORY_REGION_SUFFIX = db_rf.REGION_FAMILIES = db_rf.PREFERRED_CATEGORY = {}
    db_rf.is_same_family = lambda a, b=None: a == b
    for fn in ("get_family", "get_region_suffix", "get_family_suffix", "get_preferred_product_id"):
        setattr(db_rf, fn, lambda *a, **kw: None)
    db_rf.expand_to_family_ids = lambda ids, cat: list(ids) if ids else []
    db_rf.extract_region_from_text = lambda text: None

    for n, m in [("db", db_mod), ("db.memory", db_memory), ("db.stock", db_stock),
                 ("db.clients", db_clients), ("db.conversation_state", db_cs),
                 ("db.region_preference", db_rp), ("db.catalog", db_catalog),
                 ("db.region_family", db_rf)]:
        sys.modules[n] = m

    # tools stubs
    if "tools" not in sys.modules:
        try:
            import tools  # noqa: F401
        except ImportError:
            t = types.ModuleType("tools"); t.__path__ = []
            sys.modules["tools"] = t

    tws = types.ModuleType("tools.web_search")
    tws.get_search_tools = lambda: []
    sys.modules["tools.web_search"] = tws

    sys.modules.pop("tools.email_parser", None)
    try:
        import tools.email_parser  # noqa: F401
    except ImportError:
        tep = types.ModuleType("tools.email_parser")
        tep.REGION_SUFFIXES = {}
        tep._extract_base_flavor = tep.strip_quoted_text = tep.clean_email_body = lambda x: x
        tep.try_parse_order = lambda *a, **kw: None
        sys.modules["tools.email_parser"] = tep

    utils = types.ModuleType("utils"); utils.__path__ = []
    ut = types.ModuleType("utils.telegram")
    ut.send_telegram = lambda *a, **kw: None
    sys.modules["utils"] = utils
    sys.modules["utils.telegram"] = ut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stock_response(all_in_stock):
    return {"all_in_stock": all_in_stock, "items": [], "insufficient_items": []}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestResolveFallbacks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._modules_snapshot = dict(sys.modules)
        _install_stubs()
        cls.pipeline = importlib.import_module("agents.pipeline")
        cls.models = importlib.import_module("agents.models")

    @classmethod
    def tearDownClass(cls):
        stubs_added = set(sys.modules) - set(cls._modules_snapshot)
        for name in stubs_added:
            del sys.modules[name]
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def _oi(self, name, flavor, qty=3, fallback_for=None, optional=False):
        return self.models.OrderItem(
            product_name=name, base_flavor=flavor, quantity=qty,
            fallback_for=fallback_for, optional=optional,
        )

    def _cls(self, order_items):
        return types.SimpleNamespace(
            order_items=list(order_items),
            items=self.pipeline._items_text(order_items),
            client_email="test@example.com",
            situation="new_order",
        )

    def _items_for_check(self, order_items):
        return [
            {
                "product_name": oi.product_name,
                "base_flavor": oi.base_flavor,
                "quantity": oi.quantity,
                "original_product_name": oi.product_name,
                "optional": getattr(oi, "optional", False),
            }
            for oi in order_items
        ]

    # --- Core scenarios ---

    def test_primary_in_stock_drops_fallback(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=0)]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(True)):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")
        self.assertEqual(len(cls.order_items), 1)
        self.assertEqual(cls.order_items[0].base_flavor, "Tropical")
        self.assertTrue(result["conditional_fallback"]["detected"])
        self.assertEqual(cls.items, "T Tropical x 3")

    def test_primary_oos_promotes_fallback(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=0)]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        def mock_stock(check_items):
            return _stock_response(check_items[0]["base_flavor"] != "Tropical")

        with patch.object(self.pipeline, "check_stock_for_order", side_effect=mock_stock):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Black")
        self.assertEqual(cls.items, "T Black x 3")

    def test_both_oos_keeps_primary(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=0)]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(False)):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")

    def test_no_fallbacks_passthrough(self):
        ois = [self._oi("T Green", "Green", qty=5), self._oi("T Blue", "Blue")]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        original_text = cls.items
        result = {}

        out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 2)
        self.assertNotIn("conditional_fallback", result)
        self.assertEqual(cls.items, original_text)

    def test_independent_plus_fallback_pair(self):
        ois = [
            self._oi("T Green", "Green", qty=5),
            self._oi("T Tropical", "Tropical"),
            self._oi("T Black", "Black", fallback_for=1),
        ]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(True)):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["base_flavor"], "Green")
        self.assertEqual(out[1]["base_flavor"], "Tropical")

    # --- Invalid fallback_for → DROP item ---

    def test_invalid_index_drops_item(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=99)]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")

    def test_self_reference_drops_item(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=1)]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")

    def test_chain_drops_second_level(self):
        ois = [
            self._oi("T Tropical", "Tropical"),
            self._oi("T Black", "Black", fallback_for=0),
            self._oi("T Green", "Green", fallback_for=1),  # chain → dropped
        ]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(True)):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        # Green dropped (chain), Black dropped (Tropical in stock)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")

    def test_duplicate_fallback_drops_later(self):
        ois = [
            self._oi("T Tropical", "Tropical"),
            self._oi("T Black", "Black", fallback_for=0),
            self._oi("T Green", "Green", fallback_for=0),  # duplicate → dropped
        ]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(True)):
            out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base_flavor"], "Tropical")

    # --- Edge cases ---

    def test_items_text_rebuilt(self):
        ois = [self._oi("T Tropical", "Tropical"), self._oi("T Black", "Black", fallback_for=0)]
        cls = self._cls(ois)
        self.assertIn("T Black", cls.items)

        items = self._items_for_check(ois)
        result = {}

        with patch.object(self.pipeline, "check_stock_for_order", return_value=_stock_response(True)):
            self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(cls.items, "T Tropical x 3")
        self.assertNotIn("Black", cls.items)

    def test_empty_order_items_passthrough(self):
        cls = types.SimpleNamespace(order_items=None, items=None)
        out = self.pipeline._resolve_fallbacks(cls, [], {})
        self.assertEqual(out, [])

    def test_all_items_dropped_returns_empty(self):
        """If all items have invalid fallback_for, all get dropped → empty list."""
        ois = [
            self._oi("T Black", "Black", fallback_for=99),   # invalid index
            self._oi("T Green", "Green", fallback_for=99),   # invalid index
        ]
        cls = self._cls(ois)
        items = self._items_for_check(ois)
        result = {}

        out = self.pipeline._resolve_fallbacks(cls, items, result)

        self.assertEqual(len(out), 0)
        self.assertEqual(len(cls.order_items), 0)

    def test_mismatched_lengths_passthrough(self):
        ois = [self._oi("T Tropical", "Tropical", fallback_for=0)]
        cls = self._cls(ois)
        out = self.pipeline._resolve_fallbacks(cls, [], {})
        self.assertEqual(out, [])


class TestItemsText(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.models = importlib.import_module("agents.models")
        cls.pipeline = importlib.import_module("agents.pipeline")

    def test_normal(self):
        items = [
            self.models.OrderItem(product_name="T Tropical", base_flavor="Tropical", quantity=3),
            self.models.OrderItem(product_name="T Black", base_flavor="Black", quantity=2),
        ]
        self.assertEqual(self.pipeline._items_text(items), "T Tropical x 3, T Black x 2")

    def test_none(self):
        self.assertIsNone(self.pipeline._items_text(None))

    def test_empty(self):
        self.assertIsNone(self.pipeline._items_text([]))


class TestOrderItemFallbackForValidator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.OI = importlib.import_module("agents.models").OrderItem

    def _oi(self, fb):
        return self.OI(product_name="X", base_flavor="X", fallback_for=fb)

    def test_none(self):
        self.assertIsNone(self._oi(None).fallback_for)

    def test_int_zero(self):
        self.assertEqual(self._oi(0).fallback_for, 0)

    def test_int_positive(self):
        self.assertEqual(self._oi(3).fallback_for, 3)

    def test_string_digit(self):
        self.assertEqual(self._oi("0").fallback_for, 0)

    def test_string_non_digit(self):
        self.assertIsNone(self._oi("abc").fallback_for)

    def test_negative(self):
        self.assertIsNone(self._oi(-1).fallback_for)

    def test_bool_false(self):
        self.assertIsNone(self._oi(False).fallback_for)

    def test_float_integer(self):
        self.assertEqual(self._oi(2.0).fallback_for, 2)

    def test_float_fractional_rejected(self):
        self.assertIsNone(self._oi(1.7).fallback_for)


class TestParseOptionalAndFallbackFor(unittest.TestCase):
    """Regression: verify optional and fallback_for are propagated from LLM JSON to OrderItem."""

    @classmethod
    def setUpClass(cls):
        cls.classifier = importlib.import_module("agents.classifier")

    def test_optional_parsed_from_json(self):
        """optional=true in LLM JSON → OrderItem.optional=True."""
        fake_json = '{"needs_reply":true,"situation":"new_order","client_email":"t@example.com",' \
                    '"order_items":[{"product_name":"T Green","base_flavor":"Green","quantity":5,' \
                    '"optional":true,"fallback_for":null}]}'

        fake_response = types.SimpleNamespace(content=fake_json)
        with patch.object(self.classifier, "classifier_agent") as mock_agent:
            mock_agent.run.return_value = fake_response
            result = self.classifier.run_classification(
                "From: t@example.com\nSubject: Order\n\n5 green if you have",
                context_str=None,
            )

        self.assertIsNotNone(result.order_items)
        self.assertEqual(len(result.order_items), 1)
        self.assertTrue(result.order_items[0].optional)
        self.assertIsNone(result.order_items[0].fallback_for)

    def test_fallback_for_parsed_from_json(self):
        """fallback_for=0 in LLM JSON → OrderItem.fallback_for=0."""
        fake_json = '{"needs_reply":true,"situation":"new_order","client_email":"t@example.com",' \
                    '"order_items":[' \
                    '{"product_name":"T Tropical","base_flavor":"Tropical","quantity":3,"optional":false,"fallback_for":null},' \
                    '{"product_name":"T Black","base_flavor":"Black","quantity":3,"optional":false,"fallback_for":0}' \
                    ']}'

        fake_response = types.SimpleNamespace(content=fake_json)
        with patch.object(self.classifier, "classifier_agent") as mock_agent:
            mock_agent.run.return_value = fake_response
            result = self.classifier.run_classification(
                "From: t@example.com\nSubject: Order\n\n3 tropical. if not, black.",
                context_str=None,
            )

        self.assertEqual(len(result.order_items), 2)
        self.assertIsNone(result.order_items[0].fallback_for)
        self.assertEqual(result.order_items[1].fallback_for, 0)

    def test_fallback_for_stripped_outside_new_order(self):
        """fallback_for set in payment_received → stripped to None."""
        fake_json = '{"needs_reply":true,"situation":"payment_received","client_email":"t@example.com",' \
                    '"order_items":[' \
                    '{"product_name":"T Tropical","base_flavor":"Tropical","quantity":3,"fallback_for":null},' \
                    '{"product_name":"T Black","base_flavor":"Black","quantity":3,"fallback_for":0}' \
                    ']}'

        fake_response = types.SimpleNamespace(content=fake_json)
        with patch.object(self.classifier, "classifier_agent") as mock_agent:
            mock_agent.run.return_value = fake_response
            result = self.classifier.run_classification(
                "From: t@example.com\nSubject: Payment\n\nI paid for tropical and black",
                context_str=None,
            )

        # fallback_for should be stripped for non-new_order
        for oi in result.order_items:
            self.assertIsNone(oi.fallback_for)

    def test_fallback_for_string_zero_parsed(self):
        """LLM returns fallback_for as string "0" → parsed to int 0."""
        fake_json = '{"needs_reply":true,"situation":"new_order","client_email":"t@example.com",' \
                    '"order_items":[' \
                    '{"product_name":"T Tropical","base_flavor":"Tropical","quantity":3},' \
                    '{"product_name":"T Black","base_flavor":"Black","quantity":3,"fallback_for":"0"}' \
                    ']}'

        fake_response = types.SimpleNamespace(content=fake_json)
        with patch.object(self.classifier, "classifier_agent") as mock_agent:
            mock_agent.run.return_value = fake_response
            result = self.classifier.run_classification(
                "From: t@example.com\nSubject: Order\n\n3 tropical. if not, black.",
                context_str=None,
            )

        self.assertEqual(result.order_items[1].fallback_for, 0)


class TestPipelineFallbackE2E(unittest.TestCase):
    """E2E pipeline tests: classification with fallback → resolve → stock check."""

    @classmethod
    def setUpClass(cls):
        cls.pipeline = importlib.import_module("agents.pipeline")
        cls.models = importlib.import_module("agents.models")

    def _make_classification(self, order_items, situation="new_order"):
        return types.SimpleNamespace(
            client_email="test@example.com",
            client_name="Test",
            situation=situation,
            needs_reply=True,
            order_id="AUTO-test",
            price=None,
            order_items=list(order_items),
            items=self.pipeline._items_text(order_items),
            dialog_intent=None,
            followup_to=None,
            customer_street=None,
            customer_city_state_zip=None,
            parser_used=False,
        )

    def test_fallback_e2e_primary_available(self):
        """Full pipeline path: fallback pair with primary in stock → only primary in result."""
        OI = self.models.OrderItem
        ois = [
            OI(product_name="T Tropical", base_flavor="Tropical", quantity=3,
               region_preference=["JAPAN"]),
            OI(product_name="T Black", base_flavor="Black", quantity=3,
               region_preference=["JAPAN"], fallback_for=0),
        ]
        cls = self._make_classification(ois)

        # Mock: get_client returns a postpay client, stock check returns all_in_stock
        client_data = {
            "email": "test@example.com", "name": "Test", "payment_type": "postpay",
        }
        stock_ok = {"all_in_stock": True, "items": [
            {"product_name": "T Tropical", "base_flavor": "Tropical",
             "ordered_qty": 3, "total_available": 10, "is_sufficient": True,
             "optional": False, "stock_entries": []},
        ], "insufficient_items": []}

        with patch.object(self.pipeline, "get_client", return_value=client_data), \
             patch.object(self.pipeline, "get_stock_summary", return_value={"total": 100}), \
             patch.object(self.pipeline, "resolve_order_items", side_effect=lambda items, **kw: (items, [])), \
             patch.object(self.pipeline, "apply_region_preference", side_effect=lambda items: items), \
             patch.object(self.pipeline, "check_stock_for_order", return_value=stock_ok), \
             patch.object(self.pipeline, "calculate_order_price", return_value={"total": 345.0, "per_item": []}), \
             patch.object(self.pipeline, "route_to_handler", return_value={"draft_reply": "ok", "template_used": True}), \
             patch.object(self.pipeline, "update_conversation_state", return_value=None), \
             patch.object(self.pipeline, "send_telegram", return_value=None):

            result = self.pipeline.process_classified_email(
                cls, gmail_thread_id="t1", gmail_message_id="m1",
            )

        # After fallback resolution: only Tropical should remain
        self.assertEqual(len(cls.order_items), 1)
        self.assertEqual(cls.order_items[0].base_flavor, "Tropical")
        self.assertIn("conditional_fallback", result)

    def test_fallback_all_dropped_routes_to_llm(self):
        """All items have invalid fallback_for → pipeline returns needs_routing."""
        OI = self.models.OrderItem
        ois = [
            OI(product_name="T Black", base_flavor="Black", quantity=3, fallback_for=99),
            OI(product_name="T Green", base_flavor="Green", quantity=3, fallback_for=99),
        ]
        cls = self._make_classification(ois)

        client_data = {
            "email": "test@example.com", "name": "Test", "payment_type": "postpay",
        }

        with patch.object(self.pipeline, "get_client", return_value=client_data), \
             patch.object(self.pipeline, "get_stock_summary", return_value={"total": 100}), \
             patch.object(self.pipeline, "resolve_order_items", side_effect=lambda items, **kw: (items, [])), \
             patch.object(self.pipeline, "apply_region_preference", side_effect=lambda items: items), \
             patch.object(self.pipeline, "update_conversation_state", return_value=None), \
             patch.object(self.pipeline, "send_telegram", return_value=None):

            result = self.pipeline.process_classified_email(
                cls, gmail_thread_id="t1", gmail_message_id="m1",
            )

        self.assertTrue(result.get("needs_routing"))
        self.assertIn("unresolved_context", result)

    def test_optional_oos_branch1_fires(self):
        """Pipeline Branch 1: required in stock + optional OOS → confirmed + P.S."""
        OI = self.models.OrderItem
        ois = [
            OI(product_name="T Green", base_flavor="Green", quantity=5, optional=False),
            OI(product_name="T Blue", base_flavor="Blue", quantity=3, optional=True),
        ]
        cls = self._make_classification(ois)

        client_data = {
            "email": "test@example.com", "name": "Test", "payment_type": "postpay",
        }
        stock_mixed = {"all_in_stock": False, "items": [
            {"product_name": "T Green", "base_flavor": "Green",
             "ordered_qty": 5, "total_available": 10, "is_sufficient": True,
             "optional": False, "stock_entries": [], "display_name": "Terea Green"},
            {"product_name": "T Blue", "base_flavor": "Blue",
             "ordered_qty": 3, "total_available": 0, "is_sufficient": False,
             "optional": True, "stock_entries": [], "display_name": "Terea Blue"},
        ], "insufficient_items": [
            {"product_name": "T Blue", "base_flavor": "Blue",
             "ordered_qty": 3, "total_available": 0, "is_sufficient": False,
             "optional": True, "stock_entries": [], "display_name": "Terea Blue"},
        ]}

        with patch.object(self.pipeline, "get_client", return_value=client_data), \
             patch.object(self.pipeline, "get_stock_summary", return_value={"total": 100}), \
             patch.object(self.pipeline, "resolve_order_items", side_effect=lambda items, **kw: (items, [])), \
             patch.object(self.pipeline, "apply_region_preference", side_effect=lambda items: items), \
             patch.object(self.pipeline, "check_stock_for_order", return_value=stock_mixed), \
             patch.object(self.pipeline, "calculate_order_price", return_value={"total": 550.0, "per_item": []}), \
             patch.object(self.pipeline, "select_best_alternatives", return_value={"alternatives": []}), \
             patch.object(self.pipeline, "route_to_handler", return_value={"draft_reply": "ok", "template_used": True}), \
             patch.object(self.pipeline, "update_conversation_state", return_value=None), \
             patch.object(self.pipeline, "send_telegram", return_value=None):

            result = self.pipeline.process_classified_email(
                cls, gmail_thread_id="t1", gmail_message_id="m1",
            )

        # Branch 1 should fire: optional_oos_items in result
        self.assertIn("optional_oos_items", result)


if __name__ == "__main__":
    unittest.main()
