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

    # tools.web_search
    tools_mod = types.ModuleType("tools")
    tools_mod.__path__ = []
    tools_web_search = types.ModuleType("tools.web_search")
    tools_web_search.get_search_tools = lambda: []
    sys.modules["tools"] = tools_mod
    sys.modules["tools.web_search"] = tools_web_search

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
        _install_import_stubs()
        cls.email_agent = importlib.import_module("agents.email_agent")

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
            self.email_agent.classifier_agent,
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
            patch.object(self.email_agent, "process_classified_email", return_value=processed),
            patch.object(self.email_agent, "format_result", return_value="FORMATTED"),
            patch.object(self.email_agent, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.email_agent, "save_email") as save_email_mock,
            patch.object(self.email_agent, "send_telegram"),
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
            patch.object(self.email_agent, "process_classified_email", return_value=processed),
            patch.object(self.email_agent, "format_result", return_value="NO_REPLY"),
            patch.object(self.email_agent, "route_to_handler") as route_mock,
            patch.object(self.email_agent, "save_email") as save_email_mock,
            patch.object(self.email_agent, "send_telegram"),
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
            patch.object(self.email_agent, "process_classified_email", return_value=processed),
            patch.object(self.email_agent, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.email_agent, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.email_agent, "save_email") as save_email_mock,
            patch.object(self.email_agent, "send_telegram"),
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
            patch.object(self.email_agent, "process_classified_email", return_value=processed),
            patch.object(self.email_agent, "route_to_handler", return_value=routed) as route_mock,
            patch.object(self.email_agent, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.email_agent, "save_email") as save_email_mock,
            patch.object(self.email_agent, "send_telegram"),
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
            patch.object(self.email_agent, "process_classified_email", return_value=processed),
            patch.object(self.email_agent, "_build_oos_telegram", return_value="OOS ALERT"),
            patch.object(self.email_agent, "route_to_handler", return_value=routed),
            patch.object(self.email_agent, "format_result", side_effect=lambda r: r["draft_reply"]),
            patch.object(self.email_agent, "save_email"),
            patch.object(self.email_agent, "send_telegram") as send_telegram_mock,
        ):
            out = self._run(self._classifier_payload(situation="new_order"))

        self.assertEqual(out, "OOS draft body")
        send_telegram_mock.assert_called_once()
        tg_text = send_telegram_mock.call_args.args[0]
        self.assertIn("OOS ALERT", tg_text)
        self.assertIn("<pre>OOS draft body</pre>", tg_text)


if __name__ == "__main__":
    unittest.main()
