"""Tests for OOS reply validation and fallback in stock_question handler.

Covers:
1. _extract_allowed_products — correct set from structured data
2. _validate_reply_products — word boundary matching, forbidden detection
3. _validate_reply_products — clean reply passes
4. _validate_reply_products — empty allowed → fail-closed
5. _handle_oos_reply — forbidden triggers fallback (integration, mock LLM)
6. _handle_mixed_reply — forbidden triggers mixed fallback (integration, mock LLM)
7. _validate_reply_products — cross-region rejection
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agents.handlers.stock_question import (
    _extract_allowed_products,
    _validate_reply_products,
    _build_oos_fallback,
    _build_mixed_fallback,
    _handle_oos_reply,
    _handle_mixed_reply,
    _oos_agent,
    EMPTY_ALLOWED_SENTINEL,
)
from db.catalog import get_display_name


# ---------------------------------------------------------------------------
# Test catalog data — seeded into get_catalog_products mock
# ---------------------------------------------------------------------------
_TEST_CATALOG = [
    {"stock_name": "Silver", "category": "ARMENIA", "name_norm": "silver"},
    {"stock_name": "Amber", "category": "ARMENIA", "name_norm": "amber"},
    {"stock_name": "Bronze", "category": "KZ_TEREA", "name_norm": "bronze"},
    {"stock_name": "Turquoise", "category": "ARMENIA", "name_norm": "turquoise"},
    {"stock_name": "Turquoise", "category": "TEREA_EUROPE", "name_norm": "turquoise"},
    {"stock_name": "Green", "category": "TEREA_EUROPE", "name_norm": "green"},
    {"stock_name": "Tropical", "category": "TEREA_JAPAN", "name_norm": "tropical"},
    {"stock_name": "Regular", "category": "TEREA_JAPAN", "name_norm": "regular"},
    {"stock_name": "Sienna", "category": "ARMENIA", "name_norm": "sienna"},
]

# Region suffix mapping for test display names
_TEST_REGION_SUFFIX = {
    "ARMENIA": "ME",
    "KZ_TEREA": "ME",
    "TEREA_EUROPE": "EU",
    "TEREA_JAPAN": "Made in Japan",
    "УНИКАЛЬНАЯ_ТЕРЕА": "Made in Japan",
}


def _test_get_display_name(stock_name: str, category: str) -> str:
    """Test-local get_display_name that always works regardless of stub state."""
    suffix = _TEST_REGION_SUFFIX.get(category, "")
    return f"Terea {stock_name} {suffix}".strip() if suffix else f"Terea {stock_name}"


def _mock_catalog():
    """Patch get_catalog_products and get_display_name in handler module.

    This ensures tests work even when other test modules (e.g. test_handler_templates)
    have replaced db.catalog with stubs that strip region suffixes.
    """
    from contextlib import contextmanager
    from unittest.mock import patch as _patch

    @contextmanager
    def _ctx():
        # Patch by import path + direct function globals.
        # The globals patch makes tests robust if another suite re-imported
        # agents.handlers.stock_question and our imported function objects
        # point to a different module instance.
        with _patch(
            "agents.handlers.stock_question.get_catalog_products",
            return_value=list(_TEST_CATALOG),
            create=True,
        ), _patch(
            "agents.handlers.stock_question.get_display_name",
            side_effect=_test_get_display_name,
            create=True,
        ), _patch(
            "db.catalog.get_catalog_products",
            return_value=list(_TEST_CATALOG),
            create=True,
        ), _patch(
            "db.catalog.get_display_name",
            side_effect=_test_get_display_name,
            create=True,
        ), _patch.dict(
            _extract_allowed_products.__globals__,
            {
                "get_catalog_products": lambda: list(_TEST_CATALOG),
                "get_display_name": _test_get_display_name,
            },
            clear=False,
        ), _patch.dict(
            _validate_reply_products.__globals__,
            {
                "get_catalog_products": lambda: list(_TEST_CATALOG),
                "get_display_name": _test_get_display_name,
            },
            clear=False,
        ), _patch.dict(
            _handle_oos_reply.__globals__,
            {
                "get_catalog_products": lambda: list(_TEST_CATALOG),
                "get_display_name": _test_get_display_name,
            },
            clear=False,
        ), _patch.dict(
            _handle_mixed_reply.__globals__,
            {
                "get_catalog_products": lambda: list(_TEST_CATALOG),
                "get_display_name": _test_get_display_name,
            },
            clear=False,
        ):
            yield

    # Return a decorator-compatible context manager
    class _Decorator:
        def __call__(self, func):
            from functools import wraps
            @wraps(func)
            def wrapper(self_arg, *args, **kwargs):
                with _ctx():
                    return func(self_arg, *args, **kwargs)
            return wrapper
    return _Decorator()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alt(product_name: str, category: str) -> dict:
    """Build a minimal alternative dict matching select_best_alternatives output."""
    return {
        "alternative": {
            "product_name": product_name,
            "category": category,
            "quantity": 10,
            "warehouse": "LA_MAKS",
        },
        "score": 0.9,
    }


def _stock_item(product_name: str, category: str, qty: int = 10) -> dict:
    return {
        "product_name": product_name,
        "category": category,
        "quantity": qty,
        "warehouse": "LA_MAKS",
    }


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestExtractAllowedFromStructured(unittest.TestCase):
    """test_extract_allowed_from_structured: correct set of display names."""

    @_mock_catalog()
    def test_extracts_from_alternatives_and_oos(self):
        oos_sections = [
            {
                "flavor": "Silver",
                "display_name": "Terea Silver ME",
                "_alternatives_raw": [
                    _alt("Amber", "ARMENIA"),
                    _alt("Bronze", "KZ_TEREA"),
                ],
            },
        ]
        allowed = _extract_allowed_products(oos_sections)
        self.assertIn("terea amber me", allowed)
        self.assertIn("terea bronze me", allowed)
        self.assertIn("terea silver me", allowed)  # OOS product itself
        self.assertNotIn("terea green eu", allowed)

    @_mock_catalog()
    def test_includes_in_stock_sections(self):
        oos_sections = [
            {
                "flavor": "Silver",
                "display_name": "Terea Silver ME",
                "_alternatives_raw": [_alt("Amber", "ARMENIA")],
            },
        ]
        in_stock_sections = [
            {
                "display_name": "Terea Green EU",
                "available": [_stock_item("Green", "TEREA_EUROPE")],
            },
        ]
        allowed = _extract_allowed_products(oos_sections, in_stock_sections)
        self.assertIn("terea green eu", allowed)
        self.assertIn("terea amber me", allowed)


class TestValidateCatchesForbiddenWordBoundary(unittest.TestCase):
    """test_validate_catches_forbidden_word_boundary."""

    @_mock_catalog()
    def test_catches_turquoise_me_not_in_allowed(self):
        """Turquoise ME in reply but only Turquoise EU allowed → detected."""
        allowed = {"terea turquoise eu", "terea amber me"}
        reply = "Hi, we have Terea Turquoise ME and Terea Amber ME available. Thank you!"
        is_valid, forbidden = _validate_reply_products(reply, allowed)
        self.assertFalse(is_valid)
        # Should catch turquoise me (the ME version)
        self.assertTrue(any("turquoise" in f and "me" in f for f in forbidden))

    @_mock_catalog()
    def test_no_false_positive_for_allowed_turquoise_eu(self):
        """Turquoise EU is allowed → no false positive."""
        allowed = {"terea turquoise eu", "terea amber me"}
        reply = "Hi, we have Terea Turquoise EU and Terea Amber ME. Thank you!"
        is_valid, forbidden = _validate_reply_products(reply, allowed)
        self.assertTrue(is_valid)
        self.assertEqual(forbidden, [])


class TestValidatePassesCleanReply(unittest.TestCase):
    """test_validate_passes_clean_reply: only allowed products → (True, [])."""

    @_mock_catalog()
    def test_clean_reply(self):
        allowed = {"terea amber me", "terea bronze me", "terea silver me"}
        reply = "Hi John, Silver ME is not available. We have Terea Amber ME and Terea Bronze ME as alternatives. Thank you!"
        is_valid, forbidden = _validate_reply_products(reply, allowed)
        self.assertTrue(is_valid, f"Expected valid but got forbidden: {forbidden}")
        self.assertEqual(forbidden, [])


class TestValidateEmptyAllowedFailsClosed(unittest.TestCase):
    """test_validate_empty_allowed_fails_closed."""

    def test_empty_allowed(self):
        is_valid, forbidden = _validate_reply_products(
            "Hi, we have Amber ME. Thank you!", set()
        )
        self.assertFalse(is_valid)
        self.assertEqual(forbidden, EMPTY_ALLOWED_SENTINEL)


class TestValidateCrossRegionRejected(unittest.TestCase):
    """test_validate_cross_region_rejected: allowed=Japan only, Silver ME → forbidden."""

    @_mock_catalog()
    def test_japan_only_rejects_me_product(self):
        allowed = {"terea tropical made in japan", "terea regular made in japan"}
        reply = "Hi, we have Terea Silver ME and Terea Tropical Japan as alternatives. Thank you!"
        is_valid, forbidden = _validate_reply_products(reply, allowed)
        self.assertFalse(is_valid)
        self.assertTrue(any("silver" in f for f in forbidden))


# ---------------------------------------------------------------------------
# Integration tests (mock LLM)
# ---------------------------------------------------------------------------


class TestOosHandlerForbiddenTriggersFallback(unittest.TestCase):
    """test_oos_handler_forbidden_triggers_fallback: mock LLM returns reply with
    forbidden product → fallback_triggered=True, draft_reply is deterministic."""

    @_mock_catalog()
    def test_fallback_on_hallucination(self):
        # Mock the LLM agent to return a reply with a hallucinated product
        hallucinated_reply = (
            "Hi John, Silver ME is not available. "
            "We have Terea Amber ME and Terea Green EU as alternatives. Thank you!"
        )

        # Prepare classification mock
        classification = MagicMock()
        classification.order_items = []

        result = {
            "client_email": "test@example.com",
            "client_data": {"llm_summary": ""},
        }

        oos_sections = [
            {
                "flavor": "Silver",
                "display_name": "Terea Silver ME",
                "available": [],
                "price": None,
                "is_region": False,
            },
        ]

        # Mock select_best_alternatives to return Amber ME only (not Green EU)
        mock_alts = {
            "alternatives": [
                _alt("Amber", "ARMENIA"),
                _alt("Bronze", "KZ_TEREA"),
            ]
        }

        # Patch function globals directly to avoid module re-import mismatch.
        original_sba = _handle_oos_reply.__globals__.get("select_best_alternatives")
        _handle_oos_reply.__globals__["select_best_alternatives"] = lambda **kw: mock_alts

        try:
            with patch.object(_oos_agent, "run") as mock_run:
                mock_response = MagicMock()
                mock_response.content = hallucinated_reply
                mock_run.return_value = mock_response

                result = _handle_oos_reply(
                    classification, result, "Body: Do you have Silver?",
                    oos_sections, "John", None,
                )
        finally:
            _handle_oos_reply.__globals__["select_best_alternatives"] = original_sba

        self.assertTrue(result["fallback_triggered"])
        # Fallback reply should NOT contain "Green EU"
        self.assertNotIn("Green EU", result["draft_reply"])
        # Should contain allowed alternatives
        self.assertIn("Amber", result["draft_reply"])
        self.assertIn("Thank you!", result["draft_reply"])


class TestMixedHandlerForbiddenTriggersMixedFallback(unittest.TestCase):
    """test_mixed_handler_forbidden_triggers_mixed_fallback: mock LLM returns reply
    with forbidden product → fallback preserves AVAILABLE section."""

    @_mock_catalog()
    def test_mixed_fallback_preserves_available(self):
        hallucinated_reply = (
            "Hi John, Tropical Japan is in stock. Silver ME is not available. "
            "We have Terea Sienna ME as an alternative. Thank you!"
        )

        classification = MagicMock()
        classification.order_items = []

        result = {
            "client_email": "test@example.com",
            "client_data": {"llm_summary": ""},
        }

        in_stock_sections = [
            {
                "flavor": "Tropical",
                "display_name": "Terea Tropical Made in Japan",
                "available": [_stock_item("Tropical", "TEREA_JAPAN")],
                "price": 115,
                "is_region": False,
            },
        ]

        oos_sections = [
            {
                "flavor": "Silver",
                "display_name": "Terea Silver ME",
                "available": [],
                "price": None,
                "is_region": False,
            },
        ]

        # Only Amber ME as alternative (Sienna ME is NOT in alternatives)
        mock_alts = {
            "alternatives": [_alt("Amber", "ARMENIA")]
        }

        original_sba = _handle_mixed_reply.__globals__.get("select_best_alternatives")
        _handle_mixed_reply.__globals__["select_best_alternatives"] = lambda **kw: mock_alts

        try:
            with patch.object(_oos_agent, "run") as mock_run:
                mock_response = MagicMock()
                mock_response.content = hallucinated_reply
                mock_run.return_value = mock_response

                result = _handle_mixed_reply(
                    classification, result, "Body: Do you have Tropical and Silver?",
                    in_stock_sections, oos_sections, "John", None,
                )
        finally:
            _handle_mixed_reply.__globals__["select_best_alternatives"] = original_sba

        self.assertTrue(result["fallback_triggered"])
        # Fallback should preserve available section
        self.assertIn("Tropical", result["draft_reply"])
        self.assertIn("in stock", result["draft_reply"])
        # Should contain allowed alternative
        self.assertIn("Amber", result["draft_reply"])
        # Should NOT contain hallucinated Sienna
        self.assertNotIn("Sienna", result["draft_reply"])
        self.assertIn("Thank you!", result["draft_reply"])


if __name__ == "__main__":
    unittest.main()
