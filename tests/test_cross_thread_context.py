"""Tests for cross-thread context helpers.

Tests import production functions from agents.classifier and agents.context
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
    """Install lightweight stubs so agents.classifier can be imported."""
    if "agents.classifier" in sys.modules:
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
        db_memory.resolve_order_items = lambda items, **kw: (items, [])
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
        try:
            import tools.email_parser  # noqa: F401
        except ImportError:
            tools_ep = types.ModuleType("tools.email_parser")
            tools_ep._strip_quoted_text = lambda body: body
            tools_ep.strip_quoted_text = lambda body: body
            tools_ep.try_parse_order = lambda *a, **kw: None
            tools_ep.clean_email_body = lambda body: body
            sys.modules["tools.email_parser"] = tools_ep

    # agents package stub (allows real submodule imports)
    if "agents" not in sys.modules:
        agents_mod = types.ModuleType("agents")
        agents_mod.__path__ = []
        sys.modules["agents"] = agents_mod


_MODULES_BEFORE_STUBS: dict | None = None

# Module-level references filled by setup_module
_extract_sender_email = None
EmailContext = None
format_context_for_prompt = None
_format_other_threads = None


def setup_module():
    global _MODULES_BEFORE_STUBS, _extract_sender_email, EmailContext
    global format_context_for_prompt, _format_other_threads
    _MODULES_BEFORE_STUBS = dict(sys.modules)
    _ensure_stubs()
    from agents.classifier import _extract_sender_email as _ese
    from agents.context import EmailContext as _EC, format_context_for_prompt as _fcp
    from agents.formatters import format_other_threads as _fot
    _extract_sender_email = _ese
    EmailContext = _EC
    format_context_for_prompt = _fcp
    _format_other_threads = _fot


def teardown_module():
    """Restore sys.modules so stubs don't leak to other test files."""
    if _MODULES_BEFORE_STUBS is None:
        return
    added = set(sys.modules) - set(_MODULES_BEFORE_STUBS)
    for name in added:
        sys.modules.pop(name, None)
    for name, mod in _MODULES_BEFORE_STUBS.items():
        sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Unit tests for _extract_sender_email (production function)
# ---------------------------------------------------------------------------

def test_extract_sender_email_angle_brackets():
    text = "From: Lolita <loli_ondine@yahoo.com>\nSubject: Sticks\nBody: Hello"
    assert _extract_sender_email(text) == "loli_ondine@yahoo.com"


def test_extract_sender_email_reply_to_priority():
    text = "From: noreply@shipmecarton.com\nReply-To: customer@example.com\nSubject: Order\nBody: ..."
    assert _extract_sender_email(text) == "customer@example.com"


def test_extract_sender_email_system_sender_body_email():
    """System sender + body 'Email:' → extract real client from body."""
    text = "From: noreply@shipmecarton.com\nSubject: Order\nBody: Email: real@example.com"
    assert _extract_sender_email(text) == "real@example.com"


def test_extract_sender_email_system_sender_no_body_email():
    """System sender without body 'Email:' → None."""
    text = "From: noreply@shipmecarton.com\nSubject: Order\nBody: Some order text"
    assert _extract_sender_email(text) is None


def test_extract_sender_email_quoted_body_email_ignored():
    """Customer reply with quoted 'Email:' inside citation → NOT extracted from body."""
    text = (
        "From: real_customer@example.com\n"
        "Subject: Re: Order\n"
        "Body: Thanks for the info!\n"
        "\n"
        "On Mar 10, 2026, noreply@shipmecarton.com wrote:\n"
        "Email: old_customer@example.com\n"
        "Order ID: 12345"
    )
    # From is not system, so body Email: is not even checked.
    # Result should be the From: header customer.
    assert _extract_sender_email(text) == "real_customer@example.com"


def test_extract_sender_email_reply_to_over_body_email():
    """Reply-To takes priority over body 'Email:' field."""
    text = (
        "From: noreply@shipmecarton.com\n"
        "Reply-To: priority@example.com\n"
        "Subject: New Order\n"
        "Body: Email: body_email@example.com"
    )
    assert _extract_sender_email(text) == "priority@example.com"


def test_extract_sender_email_direct_from_is_source_of_truth():
    """Direct email (non-system From) → Python returns From email.

    Even if LLM would return a different email, Python's extraction
    from headers is the source of truth that overrides LLM.
    """
    text = "From: customer@example.com\nSubject: Hello\nBody: Some question"
    assert _extract_sender_email(text) == "customer@example.com"


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
