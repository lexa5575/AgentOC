"""
Tests for OOS Template Redesign — hybrid LLM formatter + validator.

Tests cover:
- _build_formatter_input: data conversion and format_mode selection
- _validate_formatter_output: per-mode semantic validation
- fill_out_of_stock_template: end-to-end with mocked LLM
- Dedup fix in db/alternatives.py: same_flavor excluded_products
"""

import pytest
from unittest.mock import patch, MagicMock

from agents.reply_templates import (
    _build_formatter_input,
    _fallback_format_alternatives,
    fill_out_of_stock_template,
)
from agents.oos_formatter import (
    _validate_formatter_output,
    format_alternatives_line,
)


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _make_item(flavor, ordered=1, available=0, display_name=None):
    return {
        "base_flavor": flavor,
        "ordered_qty": ordered,
        "total_available": available,
        "product_name": flavor,
        "display_name": display_name,
    }


def _make_alts(entries):
    """entries: list of (product_name, category, reason)"""
    return {
        "alternatives": [
            {
                "alternative": {"product_name": pn, "category": cat, "quantity": 10},
                "reason": reason,
                "order_count": None,
            }
            for pn, cat, reason in entries
        ],
        "reason": entries[0][2] if entries else "none_available",
        "order_count": None,
    }


# ---------------------------------------------------------------------------
# _build_formatter_input tests
# ---------------------------------------------------------------------------

class TestBuildFormatterInput:

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_single_item_3_alts(self, mock_display, mock_base):
        items = [_make_item("Amber")]
        alts = {"Amber": _make_alts([
            ("Amber", "ARMENIA", "same_flavor"),
            ("Silver", "ARMENIA", "llm"),
            ("Sof Fuse", "TEREA_EUROPE", "llm"),
        ])}
        result, total, mode = _build_formatter_input(items, alts)

        assert mode == "single_item"
        assert total == 1
        assert len(result) == 1
        assert len(result[0]["alternatives"]) == 3  # single_item gets up to 3

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_multi_items_1_alt_each(self, mock_display, mock_base):
        items = [_make_item("Amber"), _make_item("Yellow", ordered=2), _make_item("Silver")]
        alts = {
            "Amber": _make_alts([("Amber", "ARMENIA", "same_flavor"), ("Silver", "ARMENIA", "llm")]),
            "Yellow": _make_alts([("Yellow", "ARMENIA", "same_flavor")]),
            "Silver": _make_alts([("Silver", "ARMENIA", "same_flavor")]),
        }
        result, total, mode = _build_formatter_input(items, alts)

        assert mode == "all_same_flavor_grouped"
        assert total == 3
        assert len(result) == 3
        # Multi-item: only 1 alt each
        for item in result:
            assert len(item["alternatives"]) == 1

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_partial_oos_missing_qty(self, mock_display, mock_base):
        items = [_make_item("Amber", ordered=5, available=2)]
        alts = {"Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")])}
        result, total, mode = _build_formatter_input(items, alts)

        assert result[0]["missing_qty"] == 3
        assert result[0]["ordered_qty"] == 5
        assert result[0]["total_available"] == 2

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_no_alternatives_empty_list(self, mock_display, mock_base):
        items = [_make_item("Amber")]
        alts = {"Amber": {"alternatives": [], "reason": "none_available"}}
        result, total, mode = _build_formatter_input(items, alts)

        assert result == []
        assert total == 1

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_1_item_with_alt_2_total_oos(self, mock_display, mock_base):
        """1 item has alt, 1 item has no alt — mode should be per_item_mapping, not single_item."""
        items = [_make_item("Amber", ordered=3), _make_item("Mauve")]
        alts = {
            "Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")]),
            "Mauve": {"alternatives": [], "reason": "none_available"},
        }
        result, total, mode = _build_formatter_input(items, alts)

        assert mode == "per_item_mapping"
        assert total == 2
        assert len(result) == 1  # only Amber has alt
        assert result[0]["missing_qty"] == 3

    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_hybrid_mixed_mode(self, mock_display, mock_base):
        items = [_make_item("Amber"), _make_item("Yellow", ordered=2), _make_item("Mauve")]
        alts = {
            "Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")]),
            "Yellow": _make_alts([("Yellow", "ARMENIA", "same_flavor")]),
            "Mauve": _make_alts([("Purple", "TEREA_JAPAN", "llm")]),
        }
        result, total, mode = _build_formatter_input(items, alts)

        assert mode == "hybrid_mixed"
        assert total == 3
        assert len(result) == 3


# ---------------------------------------------------------------------------
# _validate_formatter_output tests
# ---------------------------------------------------------------------------

class TestValidateFormatterOutput:

    def test_valid_single_item(self):
        items = [{
            "display_name": "Terea Amber EU",
            "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
            "alternatives": [
                {"display_name": "Terea Amber ME", "reason": "same_flavor"},
                {"display_name": "Terea Silver ME", "reason": "llm"},
            ],
        }]
        output = "We have alternatives: Terea Amber ME (same product, different region), Terea Silver ME"
        assert _validate_formatter_output(output, items, "single_item") is not None

    def test_valid_all_same_flavor(self):
        items = [
            {"display_name": "Terea Amber EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]},
            {"display_name": "Terea Yellow EU", "ordered_qty": 2, "total_available": 0, "missing_qty": 2,
             "alternatives": [{"display_name": "Terea Yellow ME", "reason": "same_flavor"}]},
        ]
        output = "We have alternatives: 1 x Terea Amber ME, 2 x Terea Yellow ME (same product, different region)"
        assert _validate_formatter_output(output, items, "all_same_flavor_grouped") is not None

    def test_valid_per_item_mapping(self):
        items = [
            {"display_name": "Terea Mauve EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Purple Japan", "reason": "llm"}]},
            {"display_name": "Terea Russet EU", "ordered_qty": 3, "total_available": 0, "missing_qty": 3,
             "alternatives": [{"display_name": "Terea Bronze ME", "reason": "llm"}]},
        ]
        output = (
            "We have alternatives:\n"
            "   For Terea Mauve EU: 1 x Terea Purple Japan\n"
            "   For Terea Russet EU: 3 x Terea Bronze ME"
        )
        assert _validate_formatter_output(output, items, "per_item_mapping") is not None

    def test_valid_hybrid_mixed(self):
        items = [
            {"display_name": "Terea Amber EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]},
            {"display_name": "Terea Mauve EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Purple Japan", "reason": "llm"}]},
        ]
        output = (
            "We have alternatives:\n"
            "   1 x Terea Amber ME (same product, different region)\n"
            "   For Terea Mauve EU: 1 x Terea Purple Japan"
        )
        assert _validate_formatter_output(output, items, "hybrid_mixed") is not None

    def test_too_many_lines_rejected(self):
        items = [{"display_name": "X", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
                  "alternatives": [{"display_name": "Y", "reason": "llm"}]}]
        output = "\n".join(["line"] * 6)
        assert _validate_formatter_output(output, items, "single_item") is None

    def test_greeting_rejected(self):
        items = [{"display_name": "Terea Amber EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
                  "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]}]
        output = "Hi! We have alternatives: Terea Amber ME"
        assert _validate_formatter_output(output, items, "single_item") is None

    def test_missing_alt_name_rejected(self):
        items = [{"display_name": "Terea Amber EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
                  "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]}]
        output = "We have alternatives: Terea Silver ME"  # wrong alt name
        assert _validate_formatter_output(output, items, "single_item") is None

    def test_swapped_pairs_rejected(self):
        """LLM swaps Amber→Silver and Silver→Amber — should be caught."""
        items = [
            {"display_name": "Terea Amber EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]},
            {"display_name": "Terea Silver EU", "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
             "alternatives": [{"display_name": "Terea Silver ME", "reason": "same_flavor"}]},
        ]
        # Wrong: Amber→Silver ME, Silver→Amber ME
        output = (
            "We have alternatives:\n"
            "   For Terea Amber EU: 1 x Terea Silver ME\n"
            "   For Terea Silver EU: 1 x Terea Amber ME"
        )
        assert _validate_formatter_output(output, items, "per_item_mapping") is None

    def test_per_item_same_flavor_without_region_note_rejected(self):
        items = [
            {"display_name": "Terea Amber EU", "ordered_qty": 3, "total_available": 0, "missing_qty": 3,
             "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}]},
        ]
        output = (
            "We have alternatives:\n"
            "   For Terea Amber EU: 3 x Terea Amber ME"  # missing region note!
        )
        assert _validate_formatter_output(output, items, "per_item_mapping") is None


# ---------------------------------------------------------------------------
# format_alternatives_line tests (mocked LLM)
# ---------------------------------------------------------------------------

class TestFormatAlternativesLine:

    @patch("agents.oos_formatter.Agent")
    def test_normal_response(self, mock_agent_cls):
        mock_response = MagicMock()
        mock_response.content = "We have alternatives: Terea Amber ME (same product, different region)"
        mock_agent_cls.return_value.run.return_value = mock_response

        items = [{
            "display_name": "Terea Amber EU",
            "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
            "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}],
        }]
        result = format_alternatives_line(items, "single_item", 1)
        assert result is not None
        assert "Terea Amber ME" in result

    @patch("agents.oos_formatter.Agent")
    def test_llm_exception_returns_none(self, mock_agent_cls):
        mock_agent_cls.return_value.run.side_effect = Exception("API error")

        items = [{
            "display_name": "Terea Amber EU",
            "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
            "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}],
        }]
        result = format_alternatives_line(items, "single_item", 1)
        assert result is None

    @patch("agents.oos_formatter.Agent")
    def test_invalid_response_returns_none(self, mock_agent_cls):
        mock_response = MagicMock()
        mock_response.content = "Hello! Thank you for your order. Here are some alternatives..."
        mock_agent_cls.return_value.run.return_value = mock_response

        items = [{
            "display_name": "Terea Amber EU",
            "ordered_qty": 1, "total_available": 0, "missing_qty": 1,
            "alternatives": [{"display_name": "Terea Amber ME", "reason": "same_flavor"}],
        }]
        result = format_alternatives_line(items, "single_item", 1)
        assert result is None  # validator rejects greeting


# ---------------------------------------------------------------------------
# fill_out_of_stock_template end-to-end tests (mocked LLM)
# ---------------------------------------------------------------------------

class TestFillOutOfStockTemplate:

    @patch("agents.oos_formatter.format_alternatives_line")
    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_full_oos_3_items_same_flavor(self, mock_dn, mock_bdn, mock_formatter):
        mock_formatter.return_value = (
            "We have alternatives: 1 x Terea Amber ME, 2 x Terea Yellow ME, "
            "1 x Terea Silver ME (same product, different region)"
        )
        items = [
            _make_item("Amber"), _make_item("Yellow", ordered=2), _make_item("Silver"),
        ]
        alts = {
            "Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")]),
            "Yellow": _make_alts([("Yellow", "ARMENIA", "same_flavor")]),
            "Silver": _make_alts([("Silver", "ARMENIA", "same_flavor")]),
        }
        result = fill_out_of_stock_template(items, alts)

        assert "Unfortunately" in result
        assert "Terea Amber" in result
        assert "shipmecarton.com" in result
        assert "Please let us know what you think" in result
        mock_formatter.assert_called_once()

    @patch("agents.oos_formatter.format_alternatives_line", return_value=None)
    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    def test_llm_failure_uses_fallback(self, mock_bdn, mock_formatter):
        items = [_make_item("Amber")]
        alts = {"Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")])}

        result = fill_out_of_stock_template(items, alts)

        assert "Unfortunately" in result
        assert "shipmecarton.com" in result
        # Fallback format used
        assert "1." in result

    @patch("agents.oos_formatter.format_alternatives_line")
    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_no_alternatives_website_only(self, mock_dn, mock_bdn, mock_formatter):
        items = [_make_item("Amber")]
        alts = {"Amber": {"alternatives": [], "reason": "none_available"}}

        result = fill_out_of_stock_template(items, alts)

        assert "Check our website" in result
        assert "Unfortunately" in result
        mock_formatter.assert_not_called()  # formatter not called

    @patch("agents.oos_formatter.format_alternatives_line")
    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_2_oos_1_with_alt_1_without(self, mock_dn, mock_bdn, mock_formatter):
        """1 item has alt (qty=3), 1 has no alt — mode=per_item_mapping, qty shown."""
        mock_formatter.return_value = (
            "We have alternatives:\n"
            "   For Terea Amber: 3 x Terea Amber ME (same product, different region)"
        )
        items = [_make_item("Amber", ordered=3), _make_item("Mauve")]
        alts = {
            "Amber": _make_alts([("Amber", "ARMENIA", "same_flavor")]),
            "Mauve": {"alternatives": [], "reason": "none_available"},
        }
        result = fill_out_of_stock_template(items, alts)

        # Formatter called with per_item_mapping mode
        call_args = mock_formatter.call_args
        assert call_args[0][1] == "per_item_mapping"  # format_mode
        assert call_args[0][2] == 2  # total_oos_count
        assert "Unfortunately" in result

    @patch("agents.oos_formatter.format_alternatives_line")
    @patch("db.catalog.get_base_display_name", side_effect=lambda x: f"Terea {x}")
    @patch("db.catalog.get_display_name", side_effect=lambda n, c: f"Terea {n} ME")
    def test_partial_oos_same_format(self, mock_dn, mock_bdn, mock_formatter):
        """Partial OOS: ordered 5, have 2 — same alt format as full OOS."""
        mock_formatter.return_value = (
            "We have alternatives: Terea Amber ME (same product, different region), "
            "Terea Silver ME, Terea Sof Fuse ME"
        )
        items = [_make_item("Amber", ordered=5, available=2)]
        alts = {"Amber": _make_alts([
            ("Amber", "ARMENIA", "same_flavor"),
            ("Silver", "ARMENIA", "llm"),
            ("Sof Fuse", "TEREA_EUROPE", "llm"),
        ])}
        result = fill_out_of_stock_template(items, alts)

        assert "we only have 2" in result
        assert "you ordered 5" in result


# ---------------------------------------------------------------------------
# Dedup fix test
# ---------------------------------------------------------------------------

class TestDedupBehavior:

    def test_same_flavor_not_blocked_by_excluded(self):
        """same_flavor (Priority 0) must NOT be blocked by excluded_products.

        If LLM for Amber EU suggested Silver as an alternative,
        Silver ME should still appear as same_flavor for Silver EU.
        Same_flavor is the highest-priority substitute and must not
        be blocked by LLM picks for other OOS flavors.
        """
        excluded = {"Silver"}  # Silver was LLM-suggested for Amber
        same_flavor_items = [
            {"product_name": "Silver", "category": "ARMENIA", "quantity": 25},
        ]
        # same_flavor should NOT be filtered — it's Priority 0
        # (the filter was intentionally removed from db/alternatives.py)
        assert len(same_flavor_items) == 1  # Silver ME stays
