"""Tests for agents.checker module."""

import pytest

pytestmark = pytest.mark.smoke

from agents.checker import (
    CheckResult,
    check_rules,
    format_check_result_for_telegram,
)


def test_check_result_defaults():
    """Test CheckResult default values."""
    result = CheckResult()
    assert result.is_ok is True
    assert result.warnings == []
    assert result.suggestions == []
    assert result.rule_violations == []
    assert result.llm_issues == []


def test_check_result_add_warning():
    """Test adding warnings marks result as not OK."""
    result = CheckResult()
    assert result.is_ok is True
    
    result.add_warning("Test warning", source="rule")
    assert result.is_ok is False
    assert "Test warning" in result.warnings
    assert "Test warning" in result.rule_violations


def test_check_result_add_suggestion():
    """Test adding suggestions doesn't affect is_ok."""
    result = CheckResult()
    assert result.is_ok is True
    
    result.add_suggestion("Test suggestion")
    assert result.is_ok is True
    assert "Test suggestion" in result.suggestions


def test_check_rules_ends_with_thank_you():
    """Test that replies ending with Thank you pass."""
    draft_ok = "Hi! Your order is on the way.\n\nThank you!"
    result_ok = check_rules(draft_ok, {})
    # Should only have a suggestion if missing, not a warning
    assert not any("Thank you" in w for w in result_ok.warnings)
    
    draft_missing = "Hi! Your order is on the way."
    result_missing = check_rules(draft_missing, {})
    assert any("Thank you" in s for s in result_missing.suggestions)


def test_check_rules_ai_self_reference():
    """Test that AI self-reference is flagged."""
    draft = "Hi! As an AI, I cannot help with that."
    result = check_rules(draft, {})
    assert result.is_ok is False
    assert any("AI self-reference" in w for w in result.warnings)


def test_check_rules_competitor_mention():
    """Test that competitor mentions are flagged."""
    draft = "Hi! You can find similar products on Amazon. Thank you!"
    result = check_rules(draft, {})
    assert result.is_ok is False
    assert any("Competitor" in w for w in result.warnings)


def test_check_rules_forbidden_phrases():
    """Test that forbidden phrases are flagged."""
    draft = "Hi! Please check the website for more info. Thank you!"
    result = check_rules(draft, {})
    assert result.is_ok is False
    assert any("Forbidden phrase" in w for w in result.warnings)


def test_check_rules_unauthorized_discount():
    """Test that unauthorized discounts are flagged."""
    draft = "Hi! I'll give you 20% off your order. Thank you!"
    result_data = {"client_data": {"discount_percent": 5}}
    result = check_rules(draft, result_data)
    assert result.is_ok is False
    assert any("Unauthorized discount" in w for w in result.warnings)


def test_check_rules_authorized_discount():
    """Test that authorized discounts pass."""
    draft = "Hi! Your 5% discount has been applied. Thank you!"
    result_data = {"client_data": {"discount_percent": 10}}
    result = check_rules(draft, result_data)
    # 5% is within 10% limit, should not flag
    assert not any("Unauthorized discount" in w for w in result.warnings)


def test_check_rules_clean_reply():
    """Test that a clean reply passes all rules."""
    draft = """Hi!

Your order has been shipped. Here is your tracking number:
9400111899562123456789

It should arrive in 3-5 business days.

Thank you!"""
    result = check_rules(draft, {"client_data": {"discount_percent": 0}})
    # Should have a suggestion about verifying tracking, but no rule violations
    assert len(result.rule_violations) == 0


def test_format_check_result_ok():
    """Test formatting when check passed."""
    result = CheckResult()
    formatted = format_check_result_for_telegram(result)
    assert "✅" in formatted


def test_format_check_result_with_issues():
    """Test formatting when check has issues."""
    result = CheckResult()
    result.add_warning("Rule violation 1", source="rule")
    result.add_warning("LLM issue 1", source="llm")
    result.add_suggestion("Suggestion 1")
    
    formatted = format_check_result_for_telegram(result)
    assert "⚠️" in formatted
    assert "Rule violation 1" in formatted
    assert "LLM issue 1" in formatted
    assert "Suggestion 1" in formatted