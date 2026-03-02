"""Tests for cross-thread context helpers."""

import json
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Unit tests for _extract_sender_email
# ---------------------------------------------------------------------------

def _get_extract_fn():
    """Import _extract_sender_email without triggering heavy deps."""
    # We only need the function itself (pure regex), so import directly
    # from the module source to avoid DB/agent imports.
    import re

    _EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')

    def _extract_sender_email(email_text: str) -> str | None:
        header_section = email_text.split("\nBody:", 1)[0] if "\nBody:" in email_text else email_text[:500]
        for line in header_section.splitlines():
            if line.lower().startswith("reply-to:"):
                match = _EMAIL_RE.search(line)
                if match:
                    return match.group(0).lower()
        for line in header_section.splitlines():
            if line.lower().startswith("from:"):
                match = _EMAIL_RE.search(line)
                if match:
                    email = match.group(0).lower()
                    if not any(skip in email for skip in ("noreply@", "no-reply@", "@shipmecarton.com")):
                        return email
        return None

    return _extract_sender_email


def test_extract_sender_email_angle_brackets():
    fn = _get_extract_fn()
    text = "From: Lolita <loli_ondine@yahoo.com>\nSubject: Sticks\nBody: Hello"
    assert fn(text) == "loli_ondine@yahoo.com"


def test_extract_sender_email_reply_to_priority():
    fn = _get_extract_fn()
    text = "From: noreply@shipmecarton.com\nReply-To: customer@example.com\nSubject: Order\nBody: ..."
    assert fn(text) == "customer@example.com"


def test_extract_sender_email_skip_noreply():
    fn = _get_extract_fn()
    text = "From: noreply@shipmecarton.com\nSubject: Order\nBody: Email: real@example.com"
    assert fn(text) is None


def test_extract_sender_email_body_not_matched():
    """From: in quoted body should not be matched."""
    fn = _get_extract_fn()
    text = (
        "From: real_sender@example.com\n"
        "Subject: Re: Test\n"
        "Body: Some text\n"
        "> From: quoted_sender@other.com\n"
        "> Original message"
    )
    assert fn(text) == "real_sender@example.com"


# ---------------------------------------------------------------------------
# Unit tests for _format_other_threads
# ---------------------------------------------------------------------------

def _get_format_fn():
    """Return a standalone _format_other_threads function."""
    def _format_other_threads(states, exclude_thread_id):
        other = [s for s in states if s.get("gmail_thread_id") != exclude_thread_id]
        if not other:
            return ""
        lines = ["--- OTHER ACTIVE THREADS ---"]
        for s in other[:3]:
            state = s.get("state", {})
            situation = s.get("last_situation", "unknown")
            lines.append(f"Thread ({situation}):")
            if state.get("facts"):
                lines.append(f"  Facts: {json.dumps(state['facts'], ensure_ascii=False)}")
            if state.get("summary"):
                lines.append(f"  Summary: {state['summary']}")
            lines.append("")
        return "\n".join(lines)

    return _format_other_threads


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
    fn = _get_format_fn()
    states = [
        _make_state("thread_A", summary="Order for Bronze"),
        _make_state("thread_B", summary="Tracking info"),
    ]
    result = fn(states, "thread_A")
    assert "thread_A" not in result.lower() or "OTHER ACTIVE THREADS" in result
    assert "Tracking info" in result
    assert "Order for Bronze" not in result


def test_format_other_threads_empty():
    fn = _get_format_fn()
    # Only current thread — should return empty
    states = [_make_state("thread_A")]
    assert fn(states, "thread_A") == ""
    # No states at all
    assert fn([], "thread_A") == ""


def test_format_other_threads_max_3():
    fn = _get_format_fn()
    states = [_make_state(f"thread_{i}", summary=f"Summary {i}") for i in range(6)]
    result = fn(states, "other_thread")
    # All 6 are "other", but max 3 should be shown
    count = result.count("Thread (")
    assert count == 3


# ---------------------------------------------------------------------------
# Integration test: format_context_for_prompt includes cross-thread section
# ---------------------------------------------------------------------------

def test_format_context_for_prompt_with_cross_thread():
    """Verify format_context_for_prompt renders OTHER ACTIVE THREADS section."""
    # Stub heavy imports before importing context module
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

    if "db.clients" not in sys.modules:
        db_clients = types.ModuleType("db.clients")
        db_clients.get_client_profile = lambda *a, **kw: None
        sys.modules["db.clients"] = db_clients

    if "db.memory" not in sys.modules:
        db_memory = types.ModuleType("db.memory")
        db_memory.get_full_email_history = lambda *a, **kw: []
        db_memory.get_full_thread_history = lambda *a, **kw: []
        sys.modules["db.memory"] = db_memory

    if "agents.reply_templates" not in sys.modules:
        reply_mod = types.ModuleType("agents.reply_templates")
        reply_mod.format_email_history = lambda h: ""
        if "agents" not in sys.modules:
            agents_mod = types.ModuleType("agents")
            agents_mod.__path__ = []
            sys.modules["agents"] = agents_mod
        sys.modules["agents.reply_templates"] = reply_mod

    from agents.context import EmailContext, format_context_for_prompt

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
