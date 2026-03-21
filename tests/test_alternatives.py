"""Tests for agents.alternatives.get_llm_alternatives inner behavior."""

import pytest

pytestmark = pytest.mark.domain_stock

import json
from unittest.mock import MagicMock, patch

from agents.alternatives import get_llm_alternatives

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(product_name: str, category: str, qty: int = 5) -> dict:
    return {"product_name": product_name, "category": category, "quantity": qty,
            "warehouse": "main", "is_fallback": False, "synced_at": None}


_AVAILABLE = [
    _item("Green", "TEREA_EUROPE", qty=10),
    _item("Silver", "TEREA_EUROPE", qty=5),
    _item("Turquoise", "ARMENIA", qty=3),
]

_PATCH_AGENT = "agents.alternatives.Agent"


def _mock_agent(response_content: str):
    """Return a mock Agent whose .run() returns content."""
    mock_resp = MagicMock()
    mock_resp.content = response_content
    mock_agent = MagicMock()
    mock_agent.run.return_value = mock_resp
    return mock_agent


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_returns_valid_items():
    """LLM returns valid CATEGORY|PRODUCT_NAME keys → correct item dicts."""
    llm_response = json.dumps(["TEREA_EUROPE|Green", "ARMENIA|Turquoise"])
    with patch(_PATCH_AGENT, return_value=_mock_agent(llm_response)):
        result = get_llm_alternatives("Silver", _AVAILABLE, [], "")
    assert len(result) == 2
    names = [it["product_name"] for it in result]
    assert "Green" in names
    assert "Turquoise" in names


def test_max_options_respected():
    """More keys returned than max_options → only max_options items."""
    llm_response = json.dumps([
        "TEREA_EUROPE|Green", "TEREA_EUROPE|Silver", "ARMENIA|Turquoise"
    ])
    with patch(_PATCH_AGENT, return_value=_mock_agent(llm_response)):
        result = get_llm_alternatives("Purple", _AVAILABLE, [], "", max_options=2)
    assert len(result) <= 2


# ---------------------------------------------------------------------------
# Hallucination guard
# ---------------------------------------------------------------------------

def test_hallucination_dropped():
    """LLM returns a key not in the stock list → it is silently dropped."""
    llm_response = json.dumps(["NONEXISTENT|FakeProduct", "TEREA_EUROPE|Green"])
    with patch(_PATCH_AGENT, return_value=_mock_agent(llm_response)):
        result = get_llm_alternatives("Silver", _AVAILABLE, [], "")
    names = [it["product_name"] for it in result]
    assert "FakeProduct" not in names
    assert "Green" in names


def test_all_hallucinated_returns_empty():
    """If all LLM keys are invalid → empty list (caller will fallback)."""
    llm_response = json.dumps(["FAKE|Foo", "FAKE|Bar"])
    with patch(_PATCH_AGENT, return_value=_mock_agent(llm_response)):
        result = get_llm_alternatives("Silver", _AVAILABLE, [], "")
    assert result == []


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------

def test_invalid_json_returns_empty():
    """LLM returns non-JSON text → empty list, no exception."""
    with patch(_PATCH_AGENT, return_value=_mock_agent("Sorry, I cannot help.")):
        result = get_llm_alternatives("Silver", _AVAILABLE, [], "")
    assert result == []


def test_agent_exception_returns_empty():
    """Agent.run() raises → empty list, no exception propagated."""
    mock_agent = MagicMock()
    mock_agent.run.side_effect = Exception("network timeout")
    with patch(_PATCH_AGENT, return_value=mock_agent):
        result = get_llm_alternatives("Silver", _AVAILABLE, [], "")
    assert result == []


def test_empty_available_returns_empty_without_calling_agent():
    """Empty available_items → empty list, Agent never instantiated."""
    with patch(_PATCH_AGENT) as mock_cls:
        result = get_llm_alternatives("Silver", [], [], "")
    assert result == []
    mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Excluded products
# ---------------------------------------------------------------------------

def test_excluded_products_not_returned():
    """LLM suggests an excluded product → filtered out."""
    llm_response = json.dumps(["TEREA_EUROPE|Green", "TEREA_EUROPE|Silver"])
    with patch(_PATCH_AGENT, return_value=_mock_agent(llm_response)):
        result = get_llm_alternatives("Purple", _AVAILABLE, [], "", excluded_products={"Green"})
    names = [it["product_name"] for it in result]
    assert "Green" not in names
    assert "Silver" in names
