"""Tests for cross-thread context helpers.

Tests import production functions from agents.email_agent and agents.context
via lightweight module stubs (same pattern as test_email_agent_pipeline_smoke).
"""

import json
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Stub setup — must run before importing agents.*
# ---------------------------------------------------------------------------

def _ensure_stubs():
    """Install lightweight stubs so agents.email_agent can be imported."""
    if "agents.email_agent" in sys.modules:
        return  # already importable

    # agno
    if "agno" not in sys.modules:
        agno = types.ModuleType("agno")
        agno.__path__ = []
        sys.modules["agno"] = agno
    if "agno.agent" not in sys.modules:
        agno_agent = types.ModuleType("agno.agent")
        class FakeAgent:
            def __init__(self, *a, **kw): pass
            def run(self, prompt): raise RuntimeError("stub")
        agno_agent.Agent = FakeAgent
        sys.modules["agno.agent"] = agno_agent
    if "agno.models" not in sys.modules:
        agno_models = types.ModuleType("agno.models")
        agno_models.__path__ = []
        sys.modules["agno.models"] = agno_models
    if "agno.models.openai" not in sys.modules:
        agno_models_openai = types.ModuleType("agno.models.openai")
        class FakeOpenAIResponses:
            def __init__(self, *a, **kw): pass
        agno_models_openai.OpenAIResponses = FakeOpenAIResponses
        sys.modules["agno.models.openai"] = agno_models_openai

    # db
    if "db" not in sys.modules:
        db_mod = types.ModuleType("db")
        db_mod.__path__ = []
        sys.modules["db"] = db_mod
    if "db.models" not in sys.modules:
        db_models = types.ModuleType("db.models")
        sys.modules["db.models"] = db_models
    if "db.url" not in sys.modules:
        db_url = types.ModuleType("db.url")
        db_url.db_url = "sqlite://"
        sys.modules["db.url"] = db_url
    if "db.memory" not in sys.modules:
        db_memory = types.ModuleType("db.memory")
        db_memory.get_full_email_history = lambda *a, **kw: []
        db_memory.get_full_thread_history = lambda *a, **kw: []
        db_memory.save_email = lambda *a, **kw: None
        db_memory.save_order_items = lambda *a, **kw: None
        db_memory.get_client = lambda *a, **kw: None
        db_memory.decrement_discount = lambda *a, **kw: None
        db_memory.get_stock_summary = lambda *a, **kw: {"total": 0}
        db_memory.check_stock_for_order = lambda *a, **kw: {
            "all_in_stock": True, "items": [], "insufficient_items": [],
        }
        db_memory.calculate_order_price = lambda *a, **kw: None
        db_memory.select_best_alternatives = lambda *a, **kw: {"alternatives": []}
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

    # tools — only stub web_search (the one email_agent imports),
    # do NOT stub the tools package itself to avoid breaking tools.stock_parser
    if "tools.web_search" not in sys.modules:
        if "tools" not in sys.modules:
            # Import real package so __path__ is correct for other tests
            try:
                import tools
            except ImportError:
                tools_mod = types.ModuleType("tools")
                tools_mod.__path__ = []
                sys.modules["tools"] = tools_mod
        tools_ws = types.ModuleType("tools.web_search")
        tools_ws.get_search_tools = lambda: []
        sys.modules["tools.web_search"] = tools_ws
    if "tools.email_parser" not in sys.modules:
        tools_ep = types.ModuleType("tools.email_parser")
        tools_ep._strip_quoted_text = lambda body: body
        sys.modules["tools.email_parser"] = tools_ep

    # agents stubs (reply_templates needed by context.py)
    if "agents" not in sys.modules:
        agents_mod = types.ModuleType("agents")
        agents_mod.__path__ = []
        sys.modules["agents"] = agents_mod
    if "agents.reply_templates" not in sys.modules:
        reply_mod = types.ModuleType("agents.reply_templates")
        reply_mod.format_email_history = lambda h: ""
        sys.modules["agents.reply_templates"] = reply_mod


_ensure_stubs()

# Now safe to import production code
from agents.email_agent import _extract_sender_email, _format_other_threads
from agents.context import EmailContext, format_context_for_prompt


# ---------------------------------------------------------------------------
# Unit tests for _extract_sender_email (production function)
# ---------------------------------------------------------------------------

def test_extract_sender_email_angle_brackets():
    text = "From: Lolita <loli_ondine@yahoo.com>\nSubject: Sticks\nBody: Hello"
    assert _extract_sender_email(text) == "loli_ondine@yahoo.com"


def test_extract_sender_email_reply_to_priority():
    text = "From: noreply@shipmecarton.com\nReply-To: customer@example.com\nSubject: Order\nBody: ..."
    assert _extract_sender_email(text) == "customer@example.com"


def test_extract_sender_email_skip_noreply():
    text = "From: noreply@shipmecarton.com\nSubject: Order\nBody: Email: real@example.com"
    assert _extract_sender_email(text) is None


def test_extract_sender_email_body_not_matched():
    """From: in quoted body should not be matched."""
    text = (
        "From: real_sender@example.com\n"
        "Subject: Re: Test\n"
        "Body: Some text\n"
        "> From: quoted_sender@other.com\n"
        "> Original message"
    )
    assert _extract_sender_email(text) == "real_sender@example.com"


# ---------------------------------------------------------------------------
# Unit tests for _format_other_threads (production function)
# ---------------------------------------------------------------------------

def _make_state(thread_id, situation="new_order", facts=None, summary=""):
    return {
        "gmail_thread_id": thread_id,
        "last_situation": situation,
        "state": {
            "facts": facts or {},
            "summary": summary,
        },
    }


def test_format_other_threads_excludes_current():
    states = [
        _make_state("thread_A", summary="Order for Bronze"),
        _make_state("thread_B", summary="Tracking info"),
    ]
    result = _format_other_threads(states, "thread_A")
    assert "Tracking info" in result
    assert "Order for Bronze" not in result


def test_format_other_threads_empty():
    # Only current thread — should return empty
    states = [_make_state("thread_A")]
    assert _format_other_threads(states, "thread_A") == ""
    # No states at all
    assert _format_other_threads([], "thread_A") == ""


def test_format_other_threads_max_3():
    states = [_make_state(f"thread_{i}", summary=f"Summary {i}") for i in range(6)]
    result = _format_other_threads(states, "other_thread")
    # All 6 are "other", but max 3 should be shown
    count = result.count("Thread (")
    assert count == 3


# ---------------------------------------------------------------------------
# Integration test: format_context_for_prompt includes cross-thread section
# ---------------------------------------------------------------------------

def test_format_context_for_prompt_with_cross_thread():
    """Verify format_context_for_prompt renders OTHER ACTIVE THREADS section."""
    ctx = EmailContext(
        situation="oos_followup",
        email_text="Do you have silver from eu?",
        client_found=True,
        client_name="Lolita",
        payment_type="prepay",
        other_thread_states=[
            {
                "gmail_thread_id": "thread_order",
                "last_situation": "new_order",
                "state": {
                    "facts": {"oos_items": ["BRONZE"], "offered_alternatives": ["Silver ME"]},
                    "summary": "Bronze OOS, offered Silver ME",
                },
            }
        ],
    )

    prompt = format_context_for_prompt(ctx)
    assert "OTHER ACTIVE THREADS" in prompt
    assert "Bronze OOS, offered Silver ME" in prompt
    assert "BRONZE" in prompt
