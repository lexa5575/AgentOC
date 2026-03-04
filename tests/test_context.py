"""Tests for agents.context module (Context Builder + Policy YAML)."""

from unittest.mock import patch

from agents.context import (
    EmailContext,
    build_context,
    format_context_for_prompt,
    load_policy,
    _load_yaml,
)


# ---------------------------------------------------------------------------
# Policy YAML loading
# ---------------------------------------------------------------------------
def test_load_policy_payment():
    """Policy loads for payment_question situation."""
    text = load_policy("payment_question")
    assert "Payment Policy" in text
    assert "Zelle" in text
    assert "Tone" in text
    assert "Absolute Constraints" in text


def test_load_policy_tracking():
    """Policy loads for tracking situation."""
    text = load_policy("tracking")
    assert "Tracking Policy" in text
    assert "Shipping Policy" in text
    assert "Tone" in text


def test_load_policy_discount():
    """Policy loads for discount_request situation."""
    text = load_policy("discount_request")
    assert "Discount Policy" in text
    assert "negotiate" in text.lower()


def test_load_policy_shipping():
    """Policy loads for shipping_timeline situation."""
    text = load_policy("shipping_timeline")
    assert "Shipping Policy" in text
    assert "USPS" in text


def test_load_policy_other():
    """Fallback situation loads tone + hard_rules."""
    text = load_policy("other")
    assert "Tone" in text
    assert "Absolute Constraints" in text


def test_load_policy_unknown_situation():
    """Unknown situation gets default (tone + hard_rules)."""
    text = load_policy("some_unknown_thing")
    assert "Tone" in text
    assert "Absolute Constraints" in text


def test_load_yaml_caching():
    """YAML files are cached after first load."""
    from agents.context import _policy_cache
    _policy_cache.clear()

    data1 = _load_yaml("tone")
    data2 = _load_yaml("tone")
    assert data1 is data2  # Same object — cached


def test_load_yaml_missing_file():
    """Missing YAML file returns empty dict."""
    data = _load_yaml("nonexistent_policy_file")
    assert data == {}


# ---------------------------------------------------------------------------
# EmailContext + format_context_for_prompt
# ---------------------------------------------------------------------------
def test_format_context_known_client():
    """Format context for a known client with all data."""
    ctx = EmailContext(
        situation="payment_question",
        email_text="How do I pay?",
        client_name="John Doe",
        client_found=True,
        payment_type="prepay",
        zelle_address="john@zelle.com",
        discount_percent=5,
        discount_orders_left=3,
        conversation_state={"status": "new", "topic": "payment"},
        history_text="=== CONVERSATION HISTORY ===\n[WE SENT] Hi John",
        policy_rules="[Payment Policy]\n- We accept Zelle",
    )

    prompt = format_context_for_prompt(ctx)

    assert "=== CLIENT PROFILE ===" in prompt
    assert "John Doe" in prompt
    assert "prepay" in prompt
    assert "john@zelle.com" in prompt
    assert "Active discount: 5%" in prompt
    assert "=== CONVERSATION STATE ===" in prompt
    assert "=== CONVERSATION HISTORY ===" in prompt
    assert "=== POLICY RULES ===" in prompt
    assert "=== CUSTOMER'S EMAIL ===" in prompt
    assert "How do I pay?" in prompt


def test_format_context_new_client():
    """Format context for an unknown client."""
    ctx = EmailContext(
        situation="other",
        email_text="Hello, I want to order.",
        client_found=False,
    )

    prompt = format_context_for_prompt(ctx)

    assert "NEW CLIENT" in prompt
    assert "CUSTOMER'S EMAIL" in prompt
    assert "Hello, I want to order." in prompt


def test_format_context_no_discount():
    """No discount info when discount is 0."""
    ctx = EmailContext(
        situation="other",
        email_text="test",
        client_found=True,
        client_name="Alice",
        payment_type="postpay",
        discount_percent=0,
        discount_orders_left=0,
    )

    prompt = format_context_for_prompt(ctx)
    assert "Discount: none" in prompt
    assert "Active discount" not in prompt


def test_format_context_no_state():
    """No CONVERSATION STATE section when state is None."""
    ctx = EmailContext(
        situation="other",
        email_text="test",
        conversation_state=None,
    )

    prompt = format_context_for_prompt(ctx)
    assert "CONVERSATION STATE" not in prompt


def test_format_context_no_history():
    """No CONVERSATION HISTORY section when history is empty."""
    ctx = EmailContext(
        situation="other",
        email_text="test",
        history_text="",
    )

    prompt = format_context_for_prompt(ctx)
    assert "CONVERSATION HISTORY" not in prompt


# ---------------------------------------------------------------------------
# build_context (with mocked DB calls)
# ---------------------------------------------------------------------------
def test_build_context_known_client():
    """build_context assembles all data correctly for known client."""
    from agents.models import EmailClassification

    classification = EmailClassification(
        needs_reply=True,
        situation="tracking",
        client_email="test@example.com",
        client_name="Bob",
    )
    result = {
        "client_email": "test@example.com",
        "client_name": "Bob",
        "client_found": True,
        "client_data": {
            "name": "Bob Smith",
            "payment_type": "postpay",
            "zelle_address": "bob@zelle.com",
            "discount_percent": 10,
            "discount_orders_left": 2,
        },
        "situation": "tracking",
        "conversation_state": {"status": "shipped", "facts": {"tracking_number": "9400111"}},
    }

    with patch("agents.context.get_full_email_history", return_value=[]):
        ctx = build_context(classification, result, "Where is my order?")

    assert ctx.client_name == "Bob Smith"
    assert ctx.client_found is True
    assert ctx.payment_type == "postpay"
    assert ctx.zelle_address == "bob@zelle.com"
    assert ctx.discount_percent == 10
    assert ctx.discount_orders_left == 2
    assert ctx.conversation_state["status"] == "shipped"
    assert ctx.situation == "tracking"
    assert "Tracking Policy" in ctx.policy_rules


def test_build_context_unknown_client():
    """build_context handles unknown client gracefully."""
    from agents.models import EmailClassification

    classification = EmailClassification(
        needs_reply=True,
        situation="other",
        client_email="new@example.com",
    )
    result = {
        "client_email": "new@example.com",
        "client_name": None,
        "client_found": False,
        "client_data": None,
        "situation": "other",
        "conversation_state": None,
    }

    with patch("agents.context.get_full_email_history", return_value=[]):
        ctx = build_context(classification, result, "Hi there")

    assert ctx.client_found is False
    assert ctx.client_name == "unknown"
    assert ctx.payment_type == "unknown"
    assert ctx.conversation_state is None


# ---------------------------------------------------------------------------
# Phase 4: Profile data in format_context_for_prompt
# ---------------------------------------------------------------------------
def test_format_context_with_profile_stats():
    """Profile stats (orders, flavors, status) appear in prompt."""
    ctx = EmailContext(
        situation="new_order",
        email_text="I want Green x3",
        client_name="Alice",
        client_found=True,
        payment_type="postpay",
        total_orders=12,
        favorite_flavors=["Green (8x)", "Silver (4x)"],
        is_active=True,
        notes="VIP client",
        llm_summary="Frequent buyer, prefers Green flavors",
    )

    prompt = format_context_for_prompt(ctx)

    assert "Total orders: 12" in prompt
    assert "Favorite flavors: Green (8x), Silver (4x)" in prompt
    assert "Status: active" in prompt
    assert "Operator notes: VIP client" in prompt
    assert "Summary: Frequent buyer, prefers Green flavors" in prompt


def test_format_context_inactive_client():
    """Inactive client shows correct status."""
    ctx = EmailContext(
        situation="other",
        email_text="test",
        client_name="Ghost",
        client_found=True,
        payment_type="prepay",
        is_active=False,
    )

    prompt = format_context_for_prompt(ctx)
    assert "Status: inactive" in prompt


def test_format_context_no_notes_no_summary():
    """Empty notes/summary are not shown in prompt."""
    ctx = EmailContext(
        situation="other",
        email_text="test",
        client_name="Simple",
        client_found=True,
        payment_type="prepay",
        notes="",
        llm_summary="",
    )

    prompt = format_context_for_prompt(ctx)
    assert "Operator notes" not in prompt
    assert "Summary" not in prompt


def test_build_context_with_thread_id():
    """When gmail_thread_id is in result, build_context uses thread history."""
    from agents.models import EmailClassification

    classification = EmailClassification(
        needs_reply=True,
        situation="tracking",
        client_email="thread@example.com",
        client_name="Thread User",
    )
    result = {
        "client_email": "thread@example.com",
        "client_name": "Thread User",
        "client_found": False,
        "situation": "tracking",
        "conversation_state": None,
        "gmail_thread_id": "thread_abc123",
    }

    with (
        patch("agents.context.get_full_thread_history", return_value=[]) as mock_thread,
        patch("agents.context.get_full_email_history") as mock_email,
    ):
        ctx = build_context(classification, result, "Where is my package?")

    mock_thread.assert_called_once_with("thread_abc123", max_results=10)
    mock_email.assert_not_called()
    assert ctx.email_text == "Where is my package?"


def test_build_context_without_thread_id():
    """Without gmail_thread_id, build_context falls back to client email history."""
    from agents.models import EmailClassification

    classification = EmailClassification(
        needs_reply=True,
        situation="other",
        client_email="nothrd@example.com",
    )
    result = {
        "client_email": "nothrd@example.com",
        "client_name": None,
        "client_found": False,
        "situation": "other",
        "conversation_state": None,
        # no gmail_thread_id key
    }

    with (
        patch("agents.context.get_full_thread_history") as mock_thread,
        patch("agents.context.get_full_email_history", return_value=[]) as mock_email,
    ):
        ctx = build_context(classification, result, "Hello")

    mock_email.assert_called_once_with("nothrd@example.com", max_results=10)
    mock_thread.assert_not_called()


def test_build_context_with_profile(db_session):
    """build_context uses get_client_profile for enriched data."""
    from agents.models import EmailClassification
    from db.clients import add_client, update_client_notes, update_client_summary

    # Seed client
    add_client("rich@example.com", "Rich Client", "postpay", zelle_address="r@z.com")
    update_client_notes("rich@example.com", "Premium customer")
    update_client_summary("rich@example.com", "Orders weekly")

    classification = EmailClassification(
        needs_reply=True,
        situation="new_order",
        client_email="rich@example.com",
        client_name="Rich Client",
    )
    result = {
        "client_email": "rich@example.com",
        "client_name": "Rich Client",
        "client_found": True,
        "situation": "new_order",
        "conversation_state": None,
    }

    with patch("agents.context.get_full_email_history", return_value=[]):
        ctx = build_context(classification, result, "I want to order")

    assert ctx.client_name == "Rich Client"
    assert ctx.payment_type == "postpay"
    assert ctx.notes == "Premium customer"
    assert ctx.llm_summary == "Orders weekly"
    assert ctx.total_orders == 0  # No order items seeded
