"""Regression tests for email_agent orchestration after router integration.

These tests use unittest + import stubs so they can run in minimal CI/dev
environments where OpenAI/Agno runtime deps are unavailable.
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
    # Drop previously imported modules so re-import uses these stubs.
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

    # agno.*
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

    # db + db.memory
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
    sys.modules["db"] = db_mod
    sys.modules["db.memory"] = db_memory
    sys.modules["db.clients"] = db_clients
    sys.modules["db.conversation_state"] = db_conversation_state

    # db.region_preference — needed by agents/pipeline.py
    db_region_preference = types.ModuleType("db.region_preference")
    db_region_preference.apply_region_preference = lambda items, **kw: items
    db_region_preference.apply_thread_hint = lambda items, **kw: items
    sys.modules["db.region_preference"] = db_region_preference

    # db.region_family — needed by tools/stock_tools.py and agents/handlers/stock_question.py
    db_region_family = types.ModuleType("db.region_family")
    db_region_family.CATEGORY_REGION_SUFFIX = {
        "ARMENIA": "ME", "KZ_TEREA": "ME",
        "TEREA_EUROPE": "EU", "TEREA_JAPAN": "Japan",
    }
    db_region_family.REGION_FAMILIES = {
        "EU": frozenset({"TEREA_EUROPE"}),
        "ME": frozenset({"ARMENIA", "KZ_TEREA"}),
        "JAPAN": frozenset({"TEREA_JAPAN"}),
    }
    db_region_family.REGION_FAMILIES = {
        "ME": frozenset({"ARMENIA", "KZ_TEREA"}),
        "EU": frozenset({"TEREA_EUROPE"}),
        "JAPAN": frozenset({"TEREA_JAPAN"}),
    }
    db_region_family.PREFERRED_CATEGORY = {"ME": "ARMENIA", "EU": "TEREA_EUROPE", "JAPAN": "TEREA_JAPAN"}
    db_region_family.get_family = lambda cat: None
    db_region_family.get_region_suffix = lambda cat: None
    db_region_family.get_family_suffix = lambda fam: None
    db_region_family.get_preferred_product_id = lambda *a, **kw: None
    db_region_family.is_same_family = lambda cats: True
    db_region_family.expand_to_family_ids = lambda ids, catalog: list(ids)
    db_region_family.extract_region_from_text = lambda text: None
    sys.modules["db.region_family"] = db_region_family

    # db.stock — needed by tools/stock_tools.py and agents/handlers/stock_question.py
    db_stock = types.ModuleType("db.stock")
    db_stock.CATEGORY_PRICES = {}
    db_stock.search_stock = lambda *a, **kw: []
    db_stock.search_stock_by_ids = lambda *a, **kw: []
    db_stock.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
    db_stock.get_product_type = lambda *a, **kw: None
    db_stock.resolve_warehouse = lambda *a, **kw: None
    db_stock.extract_variant_id = lambda *a, **kw: None
    db_stock.has_ambiguous_variants = lambda *a, **kw: False
    sys.modules["db.stock"] = db_stock

    # db.catalog — needed by agents/handlers/stock_question.py
    db_catalog = types.ModuleType("db.catalog")
    db_catalog.get_catalog_products = lambda: []
    db_catalog.get_display_name = lambda *a, **kw: ""
    db_catalog.get_base_display_name = lambda *a, **kw: ""
    db_catalog._enrich_display_name_with_region = lambda *a, **kw: ""
    db_catalog.get_equivalent_norms = lambda *a, **kw: set()
    sys.modules["db.catalog"] = db_catalog

    # db.product_resolver — needed by agents/handlers/stock_question.py
    db_product_resolver = types.ModuleType("db.product_resolver")
    db_product_resolver.resolve_product_to_catalog = lambda *a, **kw: []
    db_product_resolver._normalize = lambda x: x
    db_product_resolver._extract_region_categories = lambda *a, **kw: set()
    sys.modules["db.product_resolver"] = db_product_resolver

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

    # tools.stock_tools — needed by agents/handlers/general.py and oos_followup.py
    tools_stock_tools = types.ModuleType("tools.stock_tools")
    tools_stock_tools.search_stock_tool = lambda *a, **kw: "No stock info available."
    sys.modules["tools.stock_tools"] = tools_stock_tools
    if "tools.email_parser" not in sys.modules:
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body
            tools_ep.try_parse_order = lambda *a, **kw: None
            tools_ep.clean_email_body = lambda body: body
            sys.modules["tools.email_parser"] = tools_ep

    # utils.telegram
    utils_mod = types.ModuleType("utils")
    utils_mod.__path__ = []
    utils_telegram = types.ModuleType("utils.telegram")
    utils_telegram.send_telegram = lambda *args, **kwargs: None
    sys.modules["utils"] = utils_mod
    sys.modules["utils.telegram"] = utils_telegram


class TestEmailAgentRouterRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._modules_snapshot = dict(sys.modules)
        _install_import_stubs()
        cls.email_agent = importlib.import_module("agents.email_agent")
        cls.agents_pipeline = importlib.import_module("agents.pipeline")
        cls.agents_notifier = importlib.import_module("agents.notifier")
        cls.agents_classifier = importlib.import_module("agents.classifier")

    @classmethod
    def tearDownClass(cls):
        added = set(sys.modules) - set(cls._modules_snapshot)
        for name in added:
            sys.modules.pop(name, None)
        for name, mod in cls._modules_snapshot.items():
            sys.modules[name] = mod

    def _classifier_payload(self, *, situation: str, needs_reply: bool = True) -> dict:
        return {
            "needs_reply": needs_reply,
            "situation": situation,
            "client_email": "client@example.com",
            "client_name": "Test Client",
            "order_id": "12345",
            "price": "$100.00",
            "customer_street": "123 Main",
            "customer_city_state_zip": "Austin, TX 78701",
            "items": "Tera Green x 1",
            "order_items": None,
        }

    def _base_result(self, **overrides) -> dict:
        base = {
            "needs_reply": True,
            "situation": "other",
            "client_email": "client@example.com",
            "client_name": "Test Client",
            "client_found": True,
            "client_data": {"payment_type": "prepay", "name": "Test Client"},
            "template_used": False,
            "draft_reply": None,
            "needs_routing": True,
            "stock_issue": None,
        }
        base.update(overrides)
        return base

    def _run(self, payload: dict) -> str:
        with patch.object(
            self.agents_classifier.classifier_agent,
            "run",
            return_value=types.SimpleNamespace(content=json.dumps(payload)),
        ):
            return self.email_agent.classify_and_process(
                "From: client@example.com\nSubject: Test\nBody: hello"
            )

    def test_template_path_routes_and_saves_outbound(self):
        processed = self._base_result(
            situation="new_order",
        )
        routed = self._base_result(
            situation="new_order",
            template_used=True,
            draft_reply="Template reply",
            needs_routing=False,
        )
        with (
            patch.object(self.agents_pipeline, "process_classified_email", return_value=processed),
            patch.object(self.agents_pipeline, "format_result", return_value="FORMATTED"),
            patch.object(self.agents_pipeline, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.agents_pipeline, "save_email") as save_email_mock,
            patch.object(self.agents_notifier, "send_telegram"),
        ):
            out = self._run(self._classifier_payload(situation="new_order"))

        self.assertEqual(out, "FORMATTED")
        route_mock.assert_called_once()
        self.assertEqual(save_email_mock.call_count, 2)
        self.assertEqual(save_email_mock.call_args_list[0].kwargs["direction"], "inbound")
        self.assertEqual(save_email_mock.call_args_list[1].kwargs["direction"], "outbound")
        self.assertEqual(save_email_mock.call_args_list[1].kwargs["body"], "Template reply")

    def test_no_reply_path_keeps_inbound_only(self):
        processed = self._base_result(
            needs_reply=False,
            template_used=False,
            draft_reply="(No reply needed)",
            needs_routing=False,
        )
        with (
            patch.object(self.agents_pipeline, "process_classified_email", return_value=processed),
            patch.object(self.agents_pipeline, "format_result", return_value="NO_REPLY"),
            patch.object(self.agents_pipeline, "route_to_handler") as route_mock,
            patch.object(self.agents_pipeline, "save_email") as save_email_mock,
            patch.object(self.agents_notifier, "send_telegram"),
        ):
            out = self._run(self._classifier_payload(situation="other", needs_reply=False))

        self.assertEqual(out, "NO_REPLY")
        route_mock.assert_not_called()
        self.assertEqual(save_email_mock.call_count, 1)
        self.assertEqual(save_email_mock.call_args.kwargs["direction"], "inbound")

    def test_router_reply_is_written_to_history(self):
        processed = self._base_result(
            situation="tracking",
            template_used=False,
            draft_reply=None,
            needs_routing=True,
        )
        routed = self._base_result(
            situation="tracking",
            template_used=False,
            draft_reply="Handler text",
            needs_routing=False,
        )
        with (
            patch.object(self.agents_pipeline, "process_classified_email", return_value=processed),
            patch.object(self.agents_pipeline, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.agents_pipeline, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.agents_pipeline, "save_email") as save_email_mock,
            patch.object(self.agents_notifier, "send_telegram"),
        ):
            out = self._run(self._classifier_payload(situation="tracking"))

        self.assertEqual(out, "Handler text")
        route_mock.assert_called_once()
        self.assertEqual(save_email_mock.call_count, 2)
        self.assertEqual(save_email_mock.call_args_list[1].kwargs["body"], "Handler text")

    def test_router_dict_reply_replaces_result_object(self):
        processed = self._base_result(
            situation="shipping_timeline",
            needs_routing=True,
        )
        routed = self._base_result(
            situation="shipping_timeline",
            template_used=True,
            draft_reply="Dict-based reply",
            needs_routing=False,
        )
        with (
            patch.object(self.agents_pipeline, "process_classified_email", return_value=processed),
            patch.object(self.agents_pipeline, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.agents_pipeline, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.agents_pipeline, "save_email") as save_email_mock,
            patch.object(self.agents_notifier, "send_telegram"),
        ):
            out = self._run(self._classifier_payload(situation="shipping_timeline"))

        self.assertEqual(out, "Dict-based reply")
        route_mock.assert_called_once()
        self.assertEqual(save_email_mock.call_count, 2)
        self.assertEqual(save_email_mock.call_args_list[1].kwargs["body"], "Dict-based reply")

    def test_oos_telegram_still_sent_with_draft_preview(self):
        processed = self._base_result(
            situation="new_order",
            needs_routing=True,
            stock_issue={"stock_check": {"insufficient_items": []}, "best_alternatives": {}},
        )
        routed = self._base_result(
            situation="new_order",
            template_used=True,
            draft_reply="OOS draft body",
            needs_routing=False,
            stock_issue={"stock_check": {"insufficient_items": []}, "best_alternatives": {}},
        )
        with (
            patch.object(self.agents_pipeline, "process_classified_email", return_value=processed),
            patch.object(self.agents_pipeline, "build_oos_message", return_value="OOS ALERT"),
            patch.object(self.agents_pipeline, "route_to_handler", return_value=routed),
            patch.object(self.agents_pipeline, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.agents_pipeline, "save_email"),
            patch.object(self.agents_notifier, "send_telegram") as send_telegram_mock,
        ):
            out = self._run(self._classifier_payload(situation="new_order"))

        self.assertEqual(out, "OOS draft body")
        send_telegram_mock.assert_called_once()
        tg_text = send_telegram_mock.call_args.args[0]
        self.assertIn("OOS ALERT", tg_text)
        self.assertIn("<pre>OOS draft body</pre>", tg_text)


if __name__ == "__main__":
    unittest.main()
